from pydantic import BaseModel, field_validator, ConfigDict
from typing import Optional
from uuid import UUID
from app.core.roles import is_valid_role, DEFAULT_ROLE, list_roles
from app.core.enums import UserRole

ALLOWED_ROLES = {"admin", "ap_clerk", "manager", "cfo", "approver", "auditor", "user", "system"}

class UserBase(BaseModel):
    email: str
    full_name: Optional[str] = None
    role: Optional[UserRole] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[UserRole]) -> Optional[UserRole]:
        if v is None:
            return UserRole.AP_CLERK
        return v

class UserCreate(UserBase):
    password: str


class RegistrationRequest(BaseModel):
    """Self-registration. Deliberately has no `role` field.

    `/auth/register` used to accept UserCreate, which inherits `role` from
    UserBase — so an unauthenticated request could name its own role and the
    endpoint honoured it. Posting `{"role": "admin"}` against any tenant slug
    returned 201 with an administrator's token. Verified against a running
    server before this was changed.

    The field is removed rather than ignored: a field the API accepts and
    silently discards reads like an oversight to the next person, and inviting
    them to wire it back up is how this returns.
    """
    email: str
    password: str
    full_name: Optional[str] = None


class AdminUserCreate(BaseModel):
    """An account created by an administrator, who *may* choose the role.

    The counterpart to the above: choosing a role is a privileged act, so it
    lives behind users.manage rather than on a public endpoint.
    """
    email: str
    password: str
    full_name: Optional[str] = None
    role: Optional[UserRole] = None

    @field_validator("password")
    @classmethod
    def _long_enough(cls, v: str) -> str:
        if len(v) < 12:
            raise ValueError(
                "Password must be at least 12 characters. This account may be "
                "given authority to approve and release payments."
            )
        return v

class UserOut(UserBase):
    id: UUID

    model_config = ConfigDict(from_attributes=True)
