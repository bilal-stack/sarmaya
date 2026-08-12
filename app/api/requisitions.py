from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.requisition import (
    RequisitionCreate, RequisitionResponse, RequisitionListResponse,
    RejectRequisitionRequest,
)
from app.services.requisition_service import RequisitionService

router = APIRouter(prefix="/requisitions", tags=["Requisitions"])


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.get("", response_model=List[RequisitionListResponse])
def list_requisitions(
    state: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return RequisitionService(db).list_requisitions(current_user, state=state)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("", response_model=RequisitionResponse, status_code=status.HTTP_201_CREATED)
def create_requisition(
    payload: RequisitionCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Raise a request to buy something.

    This is the first record in the chain, and the one that answers "why was
    this ordered at all?" — a question nothing downstream can answer. It mints
    the correlation id that the RFQ, the quotes, the order, the receipts, the
    invoice and the payment all inherit.

    No vendor: a requisition states a need, and naming a supplier here would
    let the requester pre-select the winner before anyone has quoted.
    """
    try:
        return RequisitionService(db).create_requisition(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{requisition_id}", response_model=RequisitionResponse)
def get_requisition(
    requisition_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return RequisitionService(db).get_requisition(requisition_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{requisition_id}/submit", response_model=RequisitionResponse)
def submit_requisition(
    requisition_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Send the request for approval. Guards refuse a requisition with no lines
    or no justification an approver could act on."""
    try:
        return RequisitionService(db).submit_requisition(requisition_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{requisition_id}/approve", response_model=RequisitionResponse)
def approve_requisition(
    requisition_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Authorise the need.

    Requires requisitions.approve, refuses the person who raised it, and
    applies the same approval-matrix limits as an invoice — the thresholds
    that decide who may approve spending decide who may authorise a request
    to spend. The approved estimate becomes the ceiling for any order raised
    against it.
    """
    try:
        return RequisitionService(db).approve_requisition(requisition_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{requisition_id}/reject", response_model=RequisitionResponse)
def reject_requisition(
    requisition_id: UUID,
    payload: RejectRequisitionRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return RequisitionService(db).reject_requisition(
            requisition_id, payload.reason, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{requisition_id}/cancel", response_model=RequisitionResponse)
def cancel_requisition(
    requisition_id: UUID,
    payload: RejectRequisitionRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return RequisitionService(db).cancel_requisition(
            requisition_id, payload.reason, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)
