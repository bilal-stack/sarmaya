from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.delegation import DelegationCreate, DelegationResponse
from app.services.delegation import DelegationService

router = APIRouter(prefix="/delegations", tags=["Delegations"])


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[DelegationResponse])
def list_delegations(
    include_inactive: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Delegations you granted or received. User managers see the tenant's."""
    return DelegationService(db).list_for_user(current_user, include_inactive=include_inactive)


@router.post("", response_model=DelegationResponse, status_code=status.HTTP_201_CREATED)
def create_delegation(
    payload: DelegationCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Temporarily lend your approval authority to another user.

    The delegate can act in your role for the window, but segregation of duties
    still binds them — borrowed authority never lets someone approve their own
    work, and approvals record both who acted and whose authority they used.
    """
    try:
        return DelegationService(db).create(
            current_user,
            to_user_id=payload.to_user_id,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            from_user_id=payload.from_user_id,
            reason=payload.reason,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{delegation_id}/revoke", response_model=DelegationResponse)
def revoke_delegation(
    delegation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Withdraw a delegation early."""
    try:
        return DelegationService(db).revoke(delegation_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
