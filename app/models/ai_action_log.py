from sqlalchemy import Column, String, Integer, Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class AIActionLog(BaseModel):
    """Append-only log of every AI invocation.

    The Build Book requires AI to be gated and fully auditable: each AI action
    is logged with its model/provider, prompt version, confidence, latency, and
    status (Appendix A event family: ai.requested/completed/failed_schema/
    hitl_requested). AI assists — it never finalizes — so this is the evidence
    trail of what the AI suggested, with what inputs, and whether its output was
    trusted (status) or fell back (failed_schema).
    """

    __tablename__ = "ai_action_logs"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    action = Column(String(50), nullable=False)   # duplicate_detection | nl_query | chat | invoice_extraction
    status = Column(String(30), nullable=False)   # completed | failed_schema | hitl_requested | error

    # Provenance (reproducibility requirement).
    ai_provider = Column(String(50), nullable=True)
    ai_model = Column(String(100), nullable=True)
    prompt_version = Column(String(50), nullable=True)

    confidence = Column(Float, nullable=True)
    latency_ms = Column(Integer, nullable=True)

    # Explainability trace: short in/out summaries (truncated, no raw payloads).
    input_summary = Column(String, nullable=True)
    output_summary = Column(String, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Optional link to the object the AI acted on.
    object_type = Column(String(50), nullable=True)
    object_id = Column(UUID(as_uuid=True), nullable=True)

    tenant = relationship("Tenant", backref="ai_action_logs")
