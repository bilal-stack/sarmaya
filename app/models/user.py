from sqlalchemy import Column, String, Boolean, DateTime, JSON, ForeignKey, Integer, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.core.enums import UserRole

class User(BaseModel):
    __tablename__ = "users"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    
    role = Column(SQLEnum(UserRole), default=UserRole.USER)  # admin, manager, approver, user, auditor
    permissions = Column(JSON, default=[])
    
    is_active = Column(Boolean, default=True)
    last_login = Column(DateTime, nullable=True)

    # Monotonic counter embedded in every JWT this user is issued. Logout (and
    # password change) increments it, which invalidates all previously issued
    # tokens on their next request — the revocation mechanism for stateless JWTs.
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    
    # Relationships
    tenant = relationship("Tenant", backref="users")
    
    __table_args__ = (
        {"schema": None},  # For RLS, but keeps in public schema
    )
