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

class UserOut(UserBase):
    id: UUID

    model_config = ConfigDict(from_attributes=True)
