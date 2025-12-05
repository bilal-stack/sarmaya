from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Generator

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


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
    email = payload.get("email")
    role = payload.get("role")
    
    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    # Verify user exists
    user = db.query(User).filter(User.id == user_id, User.tenant_id == tenant_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    
    return {
        "id": user_id,
        "tenant_id": tenant_id,
        "email": email,
        "role": role or "user",
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
    
    # Set RLS context variable
    db.execute(text("SET LOCAL app.current_tenant_id = :tenant_id"), {"tenant_id": str(tenant_id)})
    
    try:
        yield db
    finally:
        # Context is reset automatically when session closes
        pass
