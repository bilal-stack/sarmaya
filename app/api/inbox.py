from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.schemas.inbox import DecisionInbox
from app.services.decision_inbox_service import DecisionInboxService
from app.services.sla_service import SlaService

router = APIRouter(prefix="/inbox", tags=["Decision Inbox"])


@router.get("", response_model=DecisionInbox)
def get_decision_inbox(
    limit: int = Query(default=100, le=200),
    overdue_only: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """One cross-task surface of everything awaiting the caller's action:
    pending invoices reduced to their single most-blocking next step
    (review duplicate, verify vendor, or approve), prioritized, each linking to
    its Live Audit timeline. Filtered to what the caller is permitted to act on.

    Items carry their SLA deadline; breached items sort first and are counted in
    `overdue_count`. `overdue_only=true` is the Build Book's "Overdue" view.
    """
    return DecisionInboxService(db).get_inbox(
        current_user, limit=limit, overdue_only=overdue_only
    )


@router.post("/escalate-overdue")
def escalate_overdue(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Escalate every SLA-breached invoice once per state entry: records an
    sla_escalated audit event and notifies the configured escalation role.
    Idempotent — safe to run repeatedly (button or cron)."""
    try:
        return SlaService(db).run_escalations(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
