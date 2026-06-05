from sqlalchemy import Column, String, Integer, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class ConfigVersion(BaseModel):
    """Append-only version history for editable tenant configuration.

    Every change to a versioned config object (an approval policy, the autopilot
    settings, or a workflow's state machine) appends one row here holding the
    full post-change JSON snapshot plus a monotonic version number. The live
    tables (policies, workflow_states) remain the current source of truth; this
    table is the immutable history behind them — the Build Book's "versioned JSON
    per workflow", and the basis for later rollback.
    """

    __tablename__ = "config_versions"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # What is being versioned. config_type is the family; config_key identifies
    # the specific object within it (a policy id, a workflow_type, or the
    # autopilot singleton key). version is monotonic per (tenant, type, key).
    config_type = Column(String(50), nullable=False)   # approval_policy | autopilot | workflow
    config_key = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False)

    # Full snapshot of the config object as it stood after this change.
    snapshot = Column(JSON, nullable=False)

    change_action = Column(String(20), nullable=False)  # created | updated | deleted | restored
    change_reason = Column(String, nullable=True)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant = relationship("Tenant", backref="config_versions")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "config_type", "config_key", "version",
            name="uq_config_versions_object_version",
        ),
    )
