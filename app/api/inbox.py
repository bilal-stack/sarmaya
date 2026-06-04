from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.inbox import DecisionInbox
from app.services.decision_inbox_service import DecisionInboxService

router = APIRouter(prefix="/inbox", tags=["Decision Inbox"])


@router.get("", response_model=DecisionInbox)
def get_decision_inbox(
    limit: int = Query(default=100, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """One cross-task surface of everything awaiting the caller's action:
    pending invoices reduced to their single most-blocking next step
    (review duplicate, verify vendor, or approve), prioritized, each linking to
    its Live Audit timeline. Filtered to what the caller is permitted to act on.
    """
    return DecisionInboxService(db).get_inbox(current_user, limit=limit)
