from sqlalchemy import CheckConstraint, Column, Date, String, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

#: What a pack covers.
#:
#: `chain` answers "what happened to this invoice" — one correlation id, end
#: to end. `period` and `control` answer the question an auditor actually
#: opens with, which is whether a control operated across a quarter; those
#: carry a date range instead, and `control` narrows it to one named control.
SCOPE_CHAIN = "chain"
SCOPE_PERIOD = "period"
SCOPE_CONTROL = "control"

PACK_SCOPES = (SCOPE_CHAIN, SCOPE_PERIOD, SCOPE_CONTROL)


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
    #: Set for chain packs and null for the others — a period does not have a
    #: correlation id. Migration 044 enforces that pairing with a check
    #: constraint rather than leaving a sealed document free to disagree with
    #: its own stated scope.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    scope = Column(String(20), nullable=False, default=SCOPE_CHAIN, index=True)
    #: Which control this evidences, for scope="control". A string, not an
    #: enum: the registry lives in app/services/audit_pack.py next to the
    #: actions that evidence each one, and adding a control should not need a
    #: migration to become storable.
    control = Column(String(50), nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)

    # SHA-256 over the canonical bundle: the integrity seal for this export.
    pack_hash = Column(String(64), nullable=False)

    # Counts, object references, attachment hashes and the chain-verification
    # result as they stood when the pack was generated.
    manifest = Column(JSON, nullable=False)

    generated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    tenant = relationship("Tenant", backref="evidence_packs")

    __table_args__ = (
        # Declared here as well as in migration 044 so create_all builds it
        # too — the test database is assembled from the models, and a
        # constraint that exists only in production is one nothing proves.
        # A sealed document that disagrees with its own stated scope is worth
        # refusing at the database rather than trusting every caller.
        CheckConstraint(
            "(scope = 'chain' AND correlation_id IS NOT NULL "
            "  AND period_start IS NULL AND period_end IS NULL "
            "  AND control IS NULL)"
            " OR (scope = 'period' AND correlation_id IS NULL "
            "  AND period_start IS NOT NULL AND period_end IS NOT NULL "
            "  AND control IS NULL)"
            " OR (scope = 'control' AND correlation_id IS NULL "
            "  AND period_start IS NOT NULL AND period_end IS NOT NULL "
            "  AND control IS NOT NULL)",
            name="ck_evidence_packs_scope_coherent",
        ),
    )
