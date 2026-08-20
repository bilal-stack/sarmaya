"""The admin console's error monitor.

Definition of Done, admin console: config screens, job monitor, audit viewer,
error monitor. This is the last of the four.

Gated on `audit.view` rather than a new permission: the roles that are supposed
to see whether the platform is behaving are the same ones trusted with the
audit trail, and inventing a second permission for the same audience is how
permission sets rot.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.services.system_health_service import SystemHealthService

router = APIRouter(prefix="/system", tags=["System"])


@router.get("/health")
def system_health(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Is anything silently not running?

    Returns a single overall status plus the readings behind it, so the console
    can lead with the answer and show the evidence underneath.
    """
    try:
        return SystemHealthService(db).report(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
