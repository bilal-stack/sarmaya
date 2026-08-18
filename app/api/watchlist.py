"""Change watchlist endpoints.

Build Book differentiator: vendor bank changes, master data edits and policy
overrides alert a watchlist role in real time. The emails are the "real time"
half; this is the half that lets somebody show, later, that the alerts were
read and by whom.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.watchlist import (
    WatchlistAlertResponse, WatchlistFeed, AcknowledgeRequest,
)
from app.services.watchlist_service import WatchlistService

router = APIRouter(prefix="/watchlist", tags=["Change Watchlist"])


def _raise_for(exc: Exception):
    code = (
        status.HTTP_403_FORBIDDEN if isinstance(exc, PermissionError)
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=code, detail=str(exc))


@router.get("", response_model=WatchlistFeed)
def list_alerts(
    open_only: bool = Query(False, description="Only alerts nobody has reviewed"),
    category: Optional[str] = Query(None),
    limit: int = Query(100, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Alerts newest first, with the count still awaiting review."""
    service = WatchlistService(db)
    try:
        return WatchlistFeed(
            open_count=service.open_count(current_user),
            items=[
                WatchlistAlertResponse.model_validate(a)
                for a in service.list_alerts(
                    current_user, open_only=open_only, category=category, limit=limit
                )
            ],
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{alert_id}/acknowledge", response_model=WatchlistAlertResponse)
def acknowledge_alert(
    alert_id: UUID,
    payload: AcknowledgeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Record that this change was reviewed, and what the reviewer concluded.

    Refused to whoever made the change: the alert exists to put a second person
    in front of it, and self-acknowledgement would let the one action the
    watchlist is for clear its own flag.
    """
    try:
        return WatchlistService(db).acknowledge(alert_id, current_user, payload.note)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
