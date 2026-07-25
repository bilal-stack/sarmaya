from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from typing import Generator

from app.core.database import get_db, set_tenant_context
from app.core.security import decode_access_token
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> dict:
    """
    Extract user from JWT token and return user dict with tenant_id
    """
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")

    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    # users is RLS-protected; bind the tenant before reading it, otherwise a
    # non-BYPASSRLS app role sees zero rows and every request 401s.
    set_tenant_context(db, str(tenant_id))

    # Verify user exists
    user = db.query(User).filter(User.id == user_id, User.tenant_id == tenant_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Token revocation: every token carries the user's token_version at issue
    # time. Logout (and password change) bumps the live counter, so a token
    # minted before the bump no longer matches and is rejected here. Tokens
    # issued before this feature lack the claim — default both sides to 0 so
    # they keep working until they expire or the user logs out once.
    if payload.get("token_version", 0) != (user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    # Read identity (role, email, active) live from the user row rather than
    # trusting the token claims. A JWT is valid for hours, so a role change or
    # deactivation must take effect on the next request — not at token expiry.
    return {
        "id": str(user.id),
        "tenant_id": str(user.tenant_id),
        "email": user.email,
        "role": getattr(user.role, "value", user.role) or "user",
        "token_version": user.token_version or 0,
    }


def get_db_session(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Generator[Session, None, None]:
    """
    Get database session with RLS context set for current tenant
    This automatically filters all queries by tenant_id
    """
    tenant_id = current_user["tenant_id"]

    # get_current_user already bound the tenant on this same (per-request) session;
    # set it again defensively in case this dependency is used on its own.
    set_tenant_context(db, str(tenant_id))

    try:
        yield db
    finally:
        # Context is reset automatically when session closes
        pass
