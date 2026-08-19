"""What a person has been told, and has not yet looked at.

The counterpart to the outbox's delivery half. Email is where a notification
goes to be missed — the people who approve things are not sitting in a shared
AP mailbox all day — so every notification also lands here, where it is visible
the next time they open the app.

Deliberately not a second inbox. The Decision Inbox is the system of record for
*what you must do*; this is a record of *what you were told*, which is a
different question and a shorter-lived one. An item can be read here and still
be waiting there.
"""
import logging
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification_outbox import NotificationOutbox, CHANNEL_IN_APP
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)


class NotificationFeedService:
    def __init__(self, db: Session):
        self.db = db

    def _mine(self, current_user: dict):
        """Only ever your own.

        No permission check, because there is no role that grants reading
        somebody else's notifications — the filter *is* the authorisation, and
        expressing it once here means no endpoint can forget it.
        """
        return (
            self.db.query(NotificationOutbox)
            .filter(
                NotificationOutbox.channel == CHANNEL_IN_APP,
                NotificationOutbox.user_id == current_user["id"],
            )
        )

    def unread_count(self, current_user: dict) -> int:
        return self._mine(current_user).filter(
            NotificationOutbox.read_at.is_(None)
        ).count()

    def list(
        self, current_user: dict, unread_only: bool = False, limit: int = 50
    ) -> List[NotificationOutbox]:
        query = self._mine(current_user)
        if unread_only:
            query = query.filter(NotificationOutbox.read_at.is_(None))
        return (
            query.order_by(NotificationOutbox.created_at.desc()).limit(limit).all()
        )

    def mark_read(self, notification_id: UUID, current_user: dict) -> NotificationOutbox:
        notification = self._mine(current_user).filter(
            NotificationOutbox.id == notification_id
        ).first()
        if not notification:
            # Same answer whether it does not exist or belongs to somebody
            # else: a different message for each would confirm the existence of
            # other people's notifications.
            raise ValueError("Notification not found")

        if notification.read_at is None:
            notification.read_at = make_naive(to_utc(utc_now()))
            self.db.add(notification)
            self.db.commit()
        return notification

    def mark_all_read(self, current_user: dict) -> int:
        """For the "clear the bell" button.

        Marks what is unread *now* rather than issuing a blanket update, so a
        notification that arrives while the request is in flight is not
        silently marked read — the one it would hide is the one that just
        happened.
        """
        unread = self._mine(current_user).filter(
            NotificationOutbox.read_at.is_(None)
        ).all()
        now = make_naive(to_utc(utc_now()))
        for notification in unread:
            notification.read_at = now
            self.db.add(notification)
        if unread:
            self.db.commit()
        return len(unread)
