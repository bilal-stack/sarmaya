from pydantic import BaseModel, EmailStr
from app.schemas.user import UserOut


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenWithUser(BaseModel):
    """Token response with user details"""
    access_token: str
    token_type: str
    user: UserOut
    
    class Config:
        from_attributes = True
