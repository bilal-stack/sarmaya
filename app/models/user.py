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

    # --- multi-factor authentication ---------------------------------------
    #
    # Every other control in this system reasons about identities: segregation
    # of duties, maker-checker, approval limits. A stolen password makes all of
    # them wrong at once, silently, with an audit trail naming the victim.
    #
    #: Encrypted at rest (app.core.mfa). A database dump alone should not hand
    #: over the second factor.
    mfa_secret = Column(String(255), nullable=True)
    #: Only after a code has been verified. Enrolment that enabled MFA before
    #: proving the app works would lock people out of their own accounts.
    mfa_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    mfa_confirmed_at = Column(DateTime, nullable=True)
    #: The last TOTP timestep accepted. What stops a code being replayed inside
    #: the window it is still technically valid for.
    mfa_last_timestep = Column(Integer, nullable=True)
    #: Consecutive failures. Six digits against a 30-second window is brute-
    #: forceable without a limit.
    mfa_failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    
    # Relationships
    tenant = relationship("Tenant", backref="users")
    
    __table_args__ = (
        {"schema": None},  # For RLS, but keeps in public schema
    )
