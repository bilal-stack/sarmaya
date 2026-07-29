from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class Delegation(BaseModel):
    """Temporary transfer of approval authority from one user to another.

    Build Book: "Delegation supports temporary assignment of approvals and
    tasks with start and end dates." A delegation lends the delegator's role
    authority to the delegate for a bounded window — it never changes either
    user's own role, and it is always visible in the audit trail: an action
    taken under delegation records who actually acted and whose authority they
    used.

    Segregation of duties still applies to the person acting, so delegation
    cannot be used to approve one's own work.
    """

    __tablename__ = "delegations"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Whose authority is lent, and to whom.
    from_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    to_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)

    reason = Column(String, nullable=True)
    # Set when withdrawn early; a revoked delegation is never active again.
    revoked_at = Column(DateTime, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant = relationship("Tenant", backref="delegations")
