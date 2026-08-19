"""Queued notifications: the transactional outbox.

Email used to be sent inside the request that triggered it, which made every
approval, escalation and watchlist alert wait on a mail server before the user
got a response — and swallowed the failure, so nobody learned when it did not
arrive.

A row here is written in the *same transaction* as the action it describes.
That is the property worth having: if the approval rolls back, no message is
queued, and if the approval commits, the message is queued for certain. A
thread or a fire-and-forget task gives neither guarantee — it can send mail for
work that was rolled back, and lose mail for work that was not.

What the queue costs is a drain step. What it buys, in a system whose whole
argument is that governance events are recorded: an SLA escalation survives a
restart, a failed send is visible and retried rather than logged and forgotten,
and you can show afterwards that the approver was told.
"""
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
#: Given up on. Distinct from pending-with-attempts: something has to be able
#: to say "this was never delivered" without a human counting retries.
STATUS_FAILED = "failed"

#: After this many attempts the message stops being retried. Chosen so a mail
#: server down for a working day is ridden out, while a permanently bad address
#: stops consuming the queue.
MAX_ATTEMPTS = 5


class NotificationOutbox(BaseModel):
    __tablename__ = "notification_outbox"

    OBJECT_TYPE = "notification"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    to_email = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=False)
    body = Column(String, nullable=False)

    #: What produced it — 'sla_escalation', 'watchlist', 'awaiting_action', …
    #: Kept for triage: "which kind of message is failing" is the first
    #: question anyone asks of a stuck queue.
    category = Column(String(50), nullable=True, index=True)

    status = Column(String(20), nullable=False, default=STATUS_PENDING, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    #: Earliest the next attempt may run. Backoff lives here rather than in the
    #: dispatcher's memory, so it survives a restart like everything else.
    next_attempt_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    tenant = relationship("Tenant", backref="notification_outbox")

    __table_args__ = (
        # The drain's only query: what is due, oldest first.
        Index(
            "ix_notification_outbox_due",
            "status", "next_attempt_at",
            postgresql_where=(status == STATUS_PENDING),
        ),
    )
