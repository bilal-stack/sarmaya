from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing import cast, Optional

from app.core.database import get_db, set_tenant_context
from app.core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    decode_access_token,
)
from app.schemas.auth import LoginIn, Token, TokenWithUser
from app.schemas.user import UserCreate, UserOut
from app.models.user import User
from app.models.tenant import Tenant
from app.core.roles import DEFAULT_ROLE, is_valid_role

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
    user_in: UserCreate,
    db: Session = Depends(get_db),
    tenant: str = Query("demo", description="Tenant slug; default 'demo'"),
):
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
    role = (user_in.role or DEFAULT_ROLE).lower()
    if not is_valid_role(role):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
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


@router.post("/login", response_model=TokenWithUser)
def login(data: LoginIn, db: Session = Depends(get_db), tenant: str = Query("demo", description="Tenant slug")):
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

    access_token = create_access_token(_token_claims(user))
    return {"access_token": access_token, "token_type": "bearer", "user": user}


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
    full_name: Optional[str] = None,
    role: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Update profile fields; role changes allowed only if valid (authorization checks can be added later)
    if full_name is not None:
        current_user.full_name = full_name.strip() or None
    if role is not None:
        role_val = role.strip().lower()
        if not is_valid_role(role_val):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
        current_user.role = role_val
    db.add(current_user)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/change-password", response_model=Token)
def change_password(
    current_password: str,
    new_password: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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
