"""Draining the notification outbox.

The counterpart to `NotificationService._send`, which only queues. This is the
part that talks to a mail server, and it runs outside the request that produced
the message — an admin button or a cron, the same shape as the SLA escalation
runner (DR-009), because the same argument applies: work that must happen but
must not happen *inside* somebody's click.

Failure handling is the reason this is a queue rather than a thread. A send
that fails is recorded with its error and retried on a backoff; one that keeps
failing is marked failed rather than retried forever, so a permanently bad
address cannot starve the queue. Nothing is silently dropped, which is what the
previous swallowed exception did.
"""
import logging
from datetime import timedelta
from typing import Dict, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.roles import has_permission, PERM_MANAGE_WORKFLOW
from app.models.notification_outbox import (
    NotificationOutbox, STATUS_PENDING, STATUS_SENT, STATUS_FAILED, MAX_ATTEMPTS,
)
from app.services.notification_service import NotificationService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

#: Minutes to wait before attempt n+1. Short at first, because most failures
#: are a mail server restarting; longer after, because a message still failing
#: on the fourth try is usually not going to succeed on the fifth either.
BACKOFF_MINUTES = {1: 1, 2: 5, 3: 15, 4: 60}


def _now():
    return make_naive(to_utc(utc_now()))


class NotificationDispatcher:
    def __init__(self, db: Session):
        self.db = db
        self.notifications = NotificationService(db)

    def dispatch(self, current_user: dict, limit: int = 100) -> Dict:
        """Attempt every message that is due. Returns what happened.

        Only this tenant's messages: the session is bound to the caller's
        tenant, so the query is scoped like every other. A deployment wanting
        one cron for all tenants runs this per tenant rather than bypassing
        that boundary.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError(
                "You do not have permission to dispatch notifications"
            )

        now = _now()

        if not settings.SMTP_ENABLED:
            # Not configured is not the same as failed to deliver, and treating
            # it as a failure would be actively harmful: five scheduled runs
            # against a deployment that has not set SMTP up yet would burn every
            # message's attempts and mark the whole queue permanently failed —
            # so turning SMTP on later would find a backlog it refuses to send,
            # which is the opposite of what keeping the rows was for.
            waiting = (
                self.db.query(func.count(NotificationOutbox.id))
                .filter(NotificationOutbox.status == STATUS_PENDING)
                .scalar()
            )
            if waiting:
                logger.info(
                    "SMTP disabled; %s message(s) held. Set SMTP_ENABLED=true to "
                    "send them.", waiting,
                )
            return {
                "attempted": 0, "sent": 0, "failed": 0, "retrying": 0,
                "held": waiting,
            }
        due = (
            self.db.query(NotificationOutbox)
            .filter(
                NotificationOutbox.status == STATUS_PENDING,
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.created_at.asc())
            .limit(limit)
            .all()
        )

        sent = 0
        failed = 0
        retrying = 0
        for message in due:
            outcome = self._attempt(message)
            if outcome == STATUS_SENT:
                sent += 1
            elif outcome == STATUS_FAILED:
                failed += 1
            else:
                retrying += 1

        self.db.commit()
        return {
            "attempted": len(due),
            "sent": sent,
            "failed": failed,
            "retrying": retrying,
            "held": 0,
        }

    def _attempt(self, message: NotificationOutbox) -> str:
        message.attempts += 1
        message.last_attempt_at = _now()
        try:
            self.notifications._deliver(
                message.to_email, message.subject, message.body
            )
            message.status = STATUS_SENT
            message.sent_at = _now()
            message.last_error = None
            message.next_attempt_at = None
        except Exception as exc:
            # Recorded, not swallowed. The old code logged and moved on, so a
            # notification that never arrived left nothing anyone would find.
            message.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if message.attempts >= MAX_ATTEMPTS:
                message.status = STATUS_FAILED
                message.next_attempt_at = None
                logger.error(
                    "Giving up on notification %s to %s after %s attempts: %s",
                    message.id, message.to_email, message.attempts,
                    message.last_error,
                )
            else:
                minutes = BACKOFF_MINUTES.get(message.attempts, 60)
                message.next_attempt_at = _now() + timedelta(minutes=minutes)
        self.db.add(message)
        self.db.flush()
        return message.status

    # --- reads ---------------------------------------------------------------

    def queue_summary(self, current_user: dict) -> Dict:
        """Counts by status, so a stuck queue is visible without a DB console."""
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to view the queue")

        counts = {STATUS_PENDING: 0, STATUS_SENT: 0, STATUS_FAILED: 0}
        for status, count in (
            self.db.query(NotificationOutbox.status, func.count())
            .group_by(NotificationOutbox.status)
            .all()
        ):
            counts[status] = count
        return counts

    def list_messages(
        self, current_user: dict, status: Optional[str] = None, limit: int = 100
    ):
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to view the queue")
        query = self.db.query(NotificationOutbox)
        if status:
            query = query.filter(NotificationOutbox.status == status)
        return (
            query.order_by(NotificationOutbox.created_at.desc()).limit(limit).all()
        )

    def retry_failed(self, current_user: dict) -> int:
        """Put failed messages back in the queue.

        For after the cause is fixed — a corrected SMTP host, a mailbox that
        was full. Deliberately manual: automatic resurrection would loop
        forever on a genuinely undeliverable address, which is what MAX_ATTEMPTS
        exists to stop.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to retry notifications")

        failed = (
            self.db.query(NotificationOutbox)
            .filter(NotificationOutbox.status == STATUS_FAILED)
            .all()
        )
        for message in failed:
            message.status = STATUS_PENDING
            message.attempts = 0
            message.next_attempt_at = _now()
            self.db.add(message)
        self.db.commit()
        return len(failed)

