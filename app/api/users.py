from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID

from pydantic import BaseModel

from app.api.deps import get_current_user, get_db_session
from app.models.user import User
from app.schemas.user import AdminUserCreate, UserOut
from app.services.audit import log_audit
from app.core.security import get_password_hash
from app.core.roles import (
    has_permission, is_valid_role, ADMIN, DEFAULT_ROLE,
    PERM_VIEW_USERS, PERM_MANAGE_USERS,
)

router = APIRouter(prefix="/users", tags=["Users"])


class RoleUpdate(BaseModel):
    """Body for a role change. A model, not a bare argument, so the value
    travels in the request body rather than the URL."""
    role: str


@router.get("", response_model=List[UserOut])
def list_users(
    active_only: bool = Query(default=True),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Users in the caller's tenant.

    Read-only, and gated by users.view — it exposes colleagues' names, emails
    and roles, which is exactly the directory a delegate picker needs and
    exactly what a general user should not be able to enumerate.

    Scoped to the caller's tenant explicitly rather than relying on RLS: the
    policies come from migration 003, so they do not exist in a create_all
    database, and without the filter this endpoint listed every tenant's staff.
    """
    if not has_permission(current_user["role"], PERM_VIEW_USERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view users",
        )
    query = db.query(User).filter(User.tenant_id == current_user["tenant_id"])
    if active_only:
        query = query.filter(User.is_active.is_(True))
    return query.order_by(User.email.asc()).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: AdminUserCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Create an account in the caller's tenant. Requires users.manage.

    The counterpart to closing self-registration. Accounts in an
    accounts-payable system are granted by someone accountable for the grant,
    not claimed by whoever finds the signup page — so the act sits behind a
    permission, is confined to the caller's own tenant, and is audited with the
    role that was handed out.

    Choosing the new user's role is allowed here precisely because the caller
    already holds users.manage. That is the distinction the registration
    endpoint failed to make.
    """
    if not has_permission(current_user["role"], PERM_MANAGE_USERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage users",
        )

    role = str(getattr(payload.role, "value", payload.role) or DEFAULT_ROLE).lower()
    if not is_valid_role(role):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role"
        )

    email = payload.email.strip().lower()
    # Scoped to the caller's tenant explicitly, for the same reason as the role
    # change below: RLS is absent from any create_all database.
    existing = (
        db.query(User)
        .filter(User.tenant_id == current_user["tenant_id"], User.email == email)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with that email already exists in this organisation",
        )

    user = User(
        tenant_id=current_user["tenant_id"],
        email=email,
        full_name=payload.full_name,
        password=get_password_hash(payload.password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()

    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="user",
        object_id=user.id,
        action="user_created",
        after_value={"email": user.email, "role": role},
    )
    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}/role", response_model=UserOut)
def set_user_role(
    user_id: UUID,
    payload: RoleUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Change another user's role. Requires users.manage.

    Role is the input to every authorization decision in the system, so this
    is deliberately narrow:

      * It is not self-service. `PUT /auth/me` used to accept a role, which
        let any user grant themselves admin; changing a role now takes a
        distinct permission.
      * You cannot change your own role, even as an admin. Self-promotion is
        the escalation path this endpoint exists to close, and an admin
        demoting themselves is how a tenant ends up with nobody who can
        administer it.
      * The last active admin cannot be demoted, for the same reason.
      * Every change is audited with the before and after role.
    """
    if not has_permission(current_user["role"], PERM_MANAGE_USERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage users",
        )

    new_role = (payload.role or "").strip().lower()
    if not is_valid_role(new_role):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role"
        )

    if str(user_id) == str(current_user["id"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own role",
        )

    # Scoped to the caller's tenant explicitly, not left to RLS. RLS is created
    # by migration 003 and so is absent from any database built with
    # create_all — which is every developer and test database here. Relying on
    # it alone would mean this endpoint, the most privilege-sensitive write in
    # the system, has no tenant boundary at all in those environments.
    tenant_users = db.query(User).filter(User.tenant_id == current_user["tenant_id"])

    user = tenant_users.filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    previous_role = getattr(user.role, "value", user.role)
    if str(previous_role).lower() == new_role:
        return user

    if str(previous_role).lower() == ADMIN:
        # Unreachable as the roles currently stand, and kept deliberately.
        # Only `admin` holds users.manage and the self-change above is already
        # refused, so the caller is always another admin and is always counted
        # here — the branch cannot fire today. It goes live the moment any
        # other role is granted users.manage (the Build Book adds HR and
        # procurement administration), and whoever grants it is unlikely to
        # also think about tenant lockout. Six lines against an unrecoverable
        # state is worth keeping; see DR-015.
        #
        # Counted within the tenant: another tenant's admins are no help to
        # this one.
        remaining_admins = (
            tenant_users
            .filter(User.role == ADMIN, User.is_active.is_(True), User.id != user_id)
            .count()
        )
        if remaining_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote the last remaining administrator",
            )

    user.role = new_role
    # Every token already issued to this user encodes the old role, so bump
    # token_version to revoke them; otherwise a demoted user keeps their old
    # authority until their token happens to expire.
    user.token_version = (user.token_version or 0) + 1
    db.add(user)

    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="user",
        object_id=user.id,
        action="role_changed",
        before_value={"role": str(previous_role).lower()},
        after_value={"role": new_role},
    )

    db.commit()
    db.refresh(user)
    return user
