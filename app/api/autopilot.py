from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.autopilot import AutopilotPreview, AutopilotRunResult
from app.services.autopilot_service import AutopilotService

router = APIRouter(prefix="/autopilot", tags=["Autopilot"])


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/preview", response_model=AutopilotPreview)
def preview_autopilot(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Dry run: show which pending invoices Autopilot would auto-approve and why
    others are skipped. Makes no changes (human-in-the-loop)."""
    try:
        return AutopilotService(db).preview(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/run", response_model=AutopilotRunResult)
def run_autopilot(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Auto-approve every eligible pending invoice within the configured bounds.
    Each approval is attributed to the triggering operator and logged as
    auto_approved. Requires Autopilot to be enabled."""
    try:
        return AutopilotService(db).run(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{invoice_id}/revert")
def revert_auto_approval(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Undo an autopilot approval that has not been paid, returning the invoice to
    pending approval for manual handling."""
    try:
        invoice = AutopilotService(db).revert_auto_approval(invoice_id, current_user)
        return {"invoice_id": str(invoice.id), "current_state": invoice.current_state}
    except (ValueError, PermissionError) as e:
        _raise_for(e)
