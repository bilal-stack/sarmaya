from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List, Optional

from app.api.deps import get_current_user, get_db_session
from app.models.user import User
from app.schemas.user import UserOut
from app.core.roles import has_permission, PERM_VIEW_USERS

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("", response_model=List[UserOut])
def list_users(
    active_only: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Users in the caller's tenant.

    Read-only, and gated by users.view — it exposes colleagues' names, emails
    and roles, which is exactly the directory a delegate picker needs and
    exactly what a general user should not be able to enumerate. Tenant
    isolation is enforced by RLS.
    """
    if not has_permission(current_user["role"], PERM_VIEW_USERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view users",
        )
    query = db.query(User)
    if active_only:
        query = query.filter(User.is_active.is_(True))
    return query.order_by(User.email.asc()).all()
