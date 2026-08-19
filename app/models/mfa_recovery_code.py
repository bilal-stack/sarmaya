"""Single-use codes for when the second factor is gone.

They are passwords, and are stored the same way: hashed, never in the clear. A
stored list in plaintext turns one database read into a permanent MFA bypass
for every enrolled user, which is a worse position than not offering recovery
codes at all.

Kept as rows rather than a column of ten so that using one is a fact with a
timestamp — "which of my codes have been spent, and when" is a question
somebody asks exactly once, in the worst circumstances.
"""
from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class MfaRecoveryCode(BaseModel):
    __tablename__ = "mfa_recovery_codes"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: The hash. The code itself is shown once, at enrolment, and never again.
    code_hash = Column(String(255), nullable=False)
    #: Single use. Set the moment it is accepted.
    used_at = Column(DateTime, nullable=True)

    user = relationship("User", backref="mfa_recovery_codes")
