from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import cast

from app.core.database import get_db, set_tenant_context
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
    create_mfa_challenge_token,
    PURPOSE_ACCESS,
    PURPOSE_MFA_CHALLENGE,
)
from app.services.mfa_service import MfaService
from app.schemas.auth import (
    LoginIn, Token, TokenWithUser, PasswordChange, ProfileUpdate,
    LoginResult, MfaVerifyIn, MfaCodeIn, MfaDisableIn,
    MfaEnrolmentOut, MfaStatusOut, MfaRecoveryCodesOut,
)
from app.schemas.user import RegistrationRequest, UserOut
from app.models.user import User
from app.models.tenant import Tenant
from app.core.config import settings
from app.core.roles import DEFAULT_ROLE

router = APIRouter(prefix="/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def _token_claims(user: User) -> dict:
    """Claims embedded in every issued token. token_version is what makes
    logout/password-change able to revoke previously issued tokens."""
    return {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "email": user.email,
        "role": getattr(user.role, "value", user.role),
        "token_version": user.token_version or 0,
    }


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    user_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not user_id or not tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    # users is RLS-protected; bind tenant before reading it.
    set_tenant_context(db, str(tenant_id))
    # A half-finished login is not a login. The challenge token issued after a
    # correct password proves one factor and must never authenticate a request
    # — if it did, MFA would be a screen somebody could skip by keeping the
    # token the password already earned them.
    if payload.get("purpose") not in (None, PURPOSE_ACCESS):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This token cannot be used to authenticate a request",
        )

    user = db.query(User).filter(User.id == user_id, User.tenant_id == tenant_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # Reject tokens minted before the user's current token_version (revoked on
    # logout / password change). Defaults to 0 for tokens predating the feature.
    if payload.get("token_version", 0) != (user.token_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked")
    return user


@router.post("/register", response_model=TokenWithUser, status_code=status.HTTP_201_CREATED)
def register(
    user_in: RegistrationRequest,
    db: Session = Depends(get_db),
    tenant: str = Query("demo", description="Tenant slug; default 'demo'"),
):
    """Self-service signup into an existing tenant.

    Two things this endpoint must not do, both of which it used to.

    It must not let the caller choose a role. `UserCreate` inherits `role`, so
    an unauthenticated `{"role": "admin"}` against any tenant slug returned 201
    with an administrator's token — remote takeover of any tenant whose slug
    could be guessed, needing no isolation bypass at all. The body no longer
    carries a role and the assignment below is unconditional.

    It must not be open by default. Even at the default clerk role a stranger
    can create vendors, raise invoices, prepare payment runs and import bank
    statements — an accounts-payable system enrols staff, it does not accept
    walk-ins. Deployments turn it on deliberately; administrators otherwise
    create accounts through POST /users, where the act is permissioned and
    audited.
    """
    if not settings.ALLOW_SELF_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Self-registration is disabled. An administrator creates "
                "accounts for this organisation."
            ),
        )

    # Resolve tenant
    tenant_obj = db.query(Tenant).filter(Tenant.slug == tenant).first()
    if not tenant_obj:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant not found")

    # Bind RLS tenant before touching the users table (read + insert below).
    set_tenant_context(db, str(tenant_obj.id))

    # Check existing user for tenant
    existing = db.query(User).filter(User.tenant_id == tenant_obj.id, User.email == user_in.email).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User already exists for tenant")

    hashed = get_password_hash(user_in.password)
    # Not negotiable by the caller. Anything above this is granted by someone
    # who already holds users.manage.
    role = DEFAULT_ROLE
    user = User(
        tenant_id=tenant_obj.id,
        email=user_in.email,
        full_name=user_in.full_name,
        password=hashed,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token(_token_claims(user))

    return {"access_token": access_token, "token_type": "bearer", "user": user}


@router.post("/token", response_model=Token)
def token_login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
    tenant: str = Query("demo", description="Tenant slug"),
):
    """OAuth2 password-flow token endpoint (form-encoded, username=email).

    Exists so standard OAuth2 tooling — most importantly the Swagger UI
    Authorize dialog — can authenticate; /auth/login remains the JSON login the
    frontend uses. Both mint identical tokens.
    """
    tenant_obj = db.query(Tenant).filter(Tenant.slug == tenant).first()
    if not tenant_obj:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant not found")
    set_tenant_context(db, str(tenant_obj.id))
    user = db.query(User).filter(
        User.tenant_id == tenant_obj.id, User.email == form.username
    ).first()

    hashed_password = cast(str, user.password) if user is not None else ""
    if not user or not verify_password(form.password, hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access_token = create_access_token(_token_claims(user))
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/login", response_model=LoginResult)
def login(data: LoginIn, db: Session = Depends(get_db), tenant: str = Query("demo", description="Tenant slug")):
    """Sign in.

    Returns a session, or — when the account has a second factor — a challenge
    that can do nothing but be exchanged for one at /auth/mfa/verify. Accounts
    without MFA are unaffected: they get the same access token as before.
    """
    tenant_obj = db.query(Tenant).filter(Tenant.slug == tenant).first()
    if not tenant_obj:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant not found")
    # Bind RLS tenant before reading the users table.
    set_tenant_context(db, str(tenant_obj.id))
    user = db.query(User).filter(User.tenant_id == tenant_obj.id, User.email == data.email).first()
        
    # Cast ORM attribute to str for static type checkers (runtime value is already a string)
    hashed_password = cast(str, user.password) if user is not None else ""
    
    if not user or not verify_password(data.password, hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.mfa_enabled:
        # The password was right, and that is all this says. No access token is
        # issued here — if one were, the second factor would be a screen rather
        # than a control.
        return {
            "mfa_required": True,
            "challenge_token": create_mfa_challenge_token(user.id, user.tenant_id),
            "token_type": "mfa_challenge",
        }

    access_token = create_access_token(_token_claims(user))
    return {"access_token": access_token, "token_type": "bearer", "user": user}


@router.post("/mfa/verify", response_model=TokenWithUser)
def mfa_verify(
    data: MfaVerifyIn,
    db: Session = Depends(get_db),
):
    """Finish a sign-in by proving the second factor.

    Takes the challenge from /login plus a code from the authenticator app, or
    one recovery code. Only here does a real token get issued.
    """
    payload = decode_access_token(data.challenge_token)
    if not payload or payload.get("purpose") != PURPOSE_MFA_CHALLENGE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That sign-in has expired. Start again.",
        )

    tenant_id = payload.get("tenant_id")
    set_tenant_context(db, str(tenant_id))
    user = db.query(User).filter(
        User.id == payload.get("sub"), User.tenant_id == tenant_id
    ).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )

    try:
        MfaService(db).verify(user, data.code)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {
        "access_token": create_access_token(_token_claims(user)),
        "token_type": "bearer",
        "user": user,
    }


# --- enrolling and managing your own second factor ---------------------------

@router.get("/mfa", response_model=MfaStatusOut)
def mfa_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = MfaService(db)
    return {
        "enabled": bool(current_user.mfa_enabled),
        "confirmed_at": current_user.mfa_confirmed_at,
        "recovery_codes_remaining": service.remaining_recovery_codes(current_user),
    }


@router.post("/mfa/setup", response_model=MfaEnrolmentOut)
def mfa_setup(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a secret to scan. Nothing is switched on until it is confirmed."""
    try:
        return MfaService(db).begin_enrolment(current_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/mfa/confirm", response_model=MfaRecoveryCodesOut)
def mfa_confirm(
    data: MfaCodeIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Prove the app works, switch it on, and hand over the recovery codes.

    Shown once. They are stored hashed, so they cannot be shown again.
    """
    try:
        return {"recovery_codes": MfaService(db).confirm_enrolment(current_user, data.code)}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/mfa/disable", status_code=status.HTTP_200_OK)
def mfa_disable(
    data: MfaDisableIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Turn it off. Needs the password and a current code."""
    try:
        MfaService(db).disable(current_user, data.password, data.code)
        return {"success": True}
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/mfa/recovery-codes", response_model=MfaRecoveryCodesOut)
def mfa_regenerate_recovery_codes(
    data: MfaCodeIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace the set, invalidating the old one."""
    try:
        return {
            "recovery_codes": MfaService(db).regenerate_recovery_codes(
                current_user, data.code
            )
        }
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Revoke every token issued to this user by bumping token_version: the next
    # request carrying an old token fails the version check in get_current_user.
    current_user.token_version = (current_user.token_version or 0) + 1
    db.add(current_user)
    db.commit()
    return {"ok": True}


@router.put("/me", response_model=UserOut)
def update_me(
    payload: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit your own profile.

    Role is intentionally not editable here. This endpoint previously accepted
    a `role` as a bare argument — which FastAPI binds to the query string — so
    any authenticated user could grant themselves any role with
    `PUT /auth/me?role=admin`. Role is the input to every authorization
    decision in the system (segregation of duties, the approval matrix,
    delegation, autopilot bounds), so a self-service role change defeats all of
    them at once. Changing a role now requires users.manage via
    `PATCH /users/{user_id}/role`.
    """
    if payload.full_name is not None:
        current_user.full_name = payload.full_name.strip() or None
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/change-password", response_model=Token)
def change_password(
    payload: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change your own password.

    Credentials arrive in the request body, never as query parameters — the
    previous signature used bare `str` arguments, which FastAPI binds to the
    query string, putting both passwords in the URL and therefore in access
    logs and browser history.
    """
    current_password = payload.current_password
    new_password = payload.new_password

    # Verify current password
    hashed_password = cast(str, current_user.password)
    if not verify_password(current_password, hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password invalid")
    # Update password and bump token_version so any OTHER session that knew the
    # old password is logged out. The caller would be logged out too, so we
    # hand back a fresh token carrying the new version to keep this session live.
    current_user.password = get_password_hash(new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    new_token = create_access_token(_token_claims(current_user))
    return {"access_token": new_token, "token_type": "bearer"}


@router.post("/refresh", response_model=Token)
def refresh(current_user: User = Depends(get_current_user)):
    # Depending on get_current_user means a revoked/stale token cannot be
    # exchanged for a new one (it fails the token_version check first). The new
    # token carries the live claims (incl. current token_version) and a fresh exp.
    new_token = create_access_token(_token_claims(current_user))
    return {"access_token": new_token, "token_type": "bearer"}
