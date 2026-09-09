"""The control matrices: approval routing and segregation of duties as grids.

Read-only, and gated on audit.view rather than a dashboard permission. These
describe the shape of the controls rather than any record — which is exactly
what somebody looking for a way around them would want — so the audience that
reads the audit trail is the right audience here.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.services.matrices import approval_matrix, sod_matrix

router = APIRouter(prefix="/matrices", tags=["matrices"])


def _raise_for(exc: Exception) -> None:
    """Same mapping the rest of the API uses: refusal is 403, bad input 400."""
    if isinstance(exc, PermissionError):
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/approval")
def approval(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Role x amount, plus the bands no rule covers and the rules never reached."""
    try:
        return approval_matrix(db, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/sod")
def sod(current_user: dict = Depends(get_current_user)):
    """Every separation, and how much actually stands in the way of each.

    Takes no db session: the answer is a function of the permission model and
    the rules in code, both of which are the same for every tenant. Nothing
    here reads a record, which is why there is nothing to isolate.
    """
    try:
        return sod_matrix(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
