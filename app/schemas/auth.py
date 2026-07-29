from typing import Optional

from pydantic import BaseModel, EmailStr, ConfigDict, Field, field_validator, model_validator
from app.schemas.user import UserOut

#: Minimum password length. Deliberately modest — length is the only rule
#: enforced, because composition rules (a digit, a symbol) push people toward
#: predictable substitutions without adding real strength.
MIN_PASSWORD_LENGTH = 8


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ProfileUpdate(BaseModel):
    """Request body for editing your own profile.

    Deliberately has no `role` field. Role is the input to every authorization
    decision in the system, so it can never be self-service — see
    `PATCH /users/{id}/role`, which requires users.manage.
    """
    full_name: Optional[str] = None


class PasswordChange(BaseModel):
    """Request body for changing your own password.

    A model rather than bare `str` arguments on the endpoint: FastAPI treats
    unannotated scalars as *query parameters*, which would put both passwords
    in the request URL — and therefore into access logs, proxy logs, browser
    history and any error report carrying the full URL.
    """
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)

    @field_validator("new_password")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Password cannot be blank")
        return v

    @model_validator(mode="after")
    def _must_actually_change(self):
        if self.current_password == self.new_password:
            raise ValueError("New password must be different from the current one")
        return self


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenWithUser(BaseModel):
    """Token response with user details"""
    access_token: str
    token_type: str
    user: UserOut
    
    model_config = ConfigDict(from_attributes=True)
