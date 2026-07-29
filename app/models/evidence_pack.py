from sqlalchemy import Column, String, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class EvidencePack(BaseModel):
    """An audit-ready bundle assembled for one transaction chain.

    Build Book: "Evidence Pack Generator: one-click audit-ready bundle with
    hashes, logs, and policy snapshots", and the canonical EvidencePack entity
    (hashes, attachments, extraction — immutable references).

    The pack contents are assembled from live records at generation time; this
    row records *that* a pack was produced, by whom, and its `pack_hash` — a
    SHA-256 over the canonical bundle. Re-generating later and comparing hashes
    proves whether anything underlying the export has since changed, and the
    manifest keeps the attachment hashes and chain-verification result even if
    the pack is regenerated.
    """

    __tablename__ = "evidence_packs"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    correlation_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    # SHA-256 over the canonical bundle: the integrity seal for this export.
    pack_hash = Column(String(64), nullable=False)

    # Counts, object references, attachment hashes and the chain-verification
    # result as they stood when the pack was generated.
    manifest = Column(JSON, nullable=False)

    generated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant = relationship("Tenant", backref="evidence_packs")
