"""Change watchlist alerts.

Build Book differentiator: *vendor bank changes, master data edits, and policy
overrides trigger real-time alerts to a watchlist role.*

All three are already audited, but an audit trail answers "what happened to
this record" for someone who has already decided to look at that record. None
of these three announce themselves, and they share a property that makes that
dangerous: each one changes where money goes or who may send it, without
touching a single invoice. Somebody watching invoices sees nothing.

Kept as rows rather than only emailed, because an alert nobody can list is an
alert nobody can prove they reviewed. Acknowledgement is recorded for the same
reason.
"""
from sqlalchemy import Column, String, ForeignKey, DateTime, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

#: What triggered the alert. The three the Build Book names.
CATEGORY_BANK_CHANGE = "vendor_bank_change"
CATEGORY_MASTER_DATA = "master_data_edit"
CATEGORY_POLICY = "policy_override"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"


class WatchlistAlert(BaseModel):
    __tablename__ = "watchlist_alerts"

    OBJECT_TYPE = "watchlist_alert"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    category = Column(String(50), nullable=False, index=True)
    severity = Column(String(20), nullable=False, default=SEVERITY_MEDIUM)

    #: What the alert is about, so the reader can open it. Not a foreign key:
    #: alerts point at several different tables, and one of them (a withdrawn
    #: policy or vendor) may be soft-deleted by the time anyone looks.
    object_type = Column(String(50), nullable=False)
    object_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    #: One line a person can read without opening anything.
    summary = Column(String, nullable=False)
    #: Before/after where there is one — the Build Book asks for inline diffs
    #: on exactly these three kinds of change.
    detail = Column(JSONB, nullable=True)

    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    acknowledged_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    #: What the reviewer concluded. An acknowledgement with no note records
    #: that somebody clicked, which is not the same as somebody checking.
    acknowledgement_note = Column(String, nullable=True)

    tenant = relationship("Tenant", backref="watchlist_alerts")

    __table_args__ = (
        # The default view is "what has nobody looked at yet", newest first.
        Index(
            "ix_watchlist_alerts_open",
            "tenant_id", "created_at",
            postgresql_where=(acknowledged_at.is_(None)),
        ),
    )
