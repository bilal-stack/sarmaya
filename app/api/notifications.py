"""Notification queue endpoints.

Messages are queued by whatever action produced them and delivered here,
outside that action's request. Same operational shape as SLA escalation: safe
to call repeatedly, driven by an admin button or a cron.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from datetime import datetime
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.services.notification_dispatcher import NotificationDispatcher

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class QueuedMessage(BaseModel):
    id: UUID
    to_email: str
    subject: str
    category: Optional[str] = None
    status: str
    attempts: int
    last_error: Optional[str] = None
    last_attempt_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DispatchResult(BaseModel):
    attempted: int
    sent: int
    failed: int      # gave up after the attempt limit
    retrying: int    # failed this time, queued for another go
    held: int = 0    # not attempted at all because SMTP is switched off


def _raise_for(exc: Exception):
    code = (
        status.HTTP_403_FORBIDDEN if isinstance(exc, PermissionError)
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=code, detail=str(exc))


@router.post("/dispatch", response_model=DispatchResult)
def dispatch_queue(
    limit: int = Query(100, le=500),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Attempt delivery of every message that is due.

    Idempotent in the way that matters: a message already sent is not picked
    up again, and one that fails is rescheduled rather than lost.
    """
    try:
        return NotificationDispatcher(db).dispatch(current_user, limit=limit)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/queue", response_model=List[QueuedMessage])
def list_queue(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What is queued, sent or given up on — so a stuck queue is visible."""
    try:
        return NotificationDispatcher(db).list_messages(
            current_user, status=status_filter, limit=limit
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/queue/summary")
def queue_summary(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return NotificationDispatcher(db).queue_summary(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/queue/retry-failed")
def retry_failed(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Requeue everything that was given up on, after fixing the cause."""
    try:
        return {"requeued": NotificationDispatcher(db).retry_failed(current_user)}
    except (ValueError, PermissionError) as e:
        _raise_for(e)
