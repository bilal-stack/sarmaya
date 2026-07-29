from sqlalchemy import Column, String, Integer, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class PolicyEval(BaseModel):
    """Append-only snapshot of a single policy evaluation.

    Build Book requirement (Security & Audit): "Every policy evaluation is
    stored with policy_version, inputs snapshot, output decision, and reasons",
    and the canonical PolicyEval entity. This makes a routing decision
    reproducible after the fact — you can show not just *what* was decided but
    which rule version decided it and on what inputs, even if the policy has
    since been edited or deleted.

    policy_version is the config_versions number for the matched policy at
    evaluation time, so it lines up with the config history and rollback.
    """

    __tablename__ = "policy_evals"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # What was evaluated. policy_key is the rule family (e.g. approval_limit);
    # policy_id/policy_name identify the specific rule that matched (null when
    # no configured rule matched and the hardcoded default was used).
    policy_key = Column(String(100), nullable=False)
    policy_id = Column(UUID(as_uuid=True), nullable=True)
    policy_name = Column(String(255), nullable=True)
    policy_version = Column(Integer, nullable=True)

    inputs = Column(JSON, nullable=False)     # snapshot of what was evaluated
    output = Column(JSON, nullable=False)     # the decision
    reasons = Column(JSON, default=list)      # human-readable justification

    # What the decision was about.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    object_type = Column(String(50), nullable=True)
    object_id = Column(UUID(as_uuid=True), nullable=True)
    evaluated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant = relationship("Tenant", backref="policy_evals")
