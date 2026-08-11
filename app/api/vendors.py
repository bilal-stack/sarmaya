from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.core.enums import VendorStatus
from app.schemas.vendor import (
    VendorCreate,
    VendorUpdate,
    VendorStatusUpdate,
    VendorResponse,
    VendorListResponse,
    VendorReviewItem,
)
from app.services.vendor_service import VendorService

router = APIRouter(prefix="/vendors", tags=["Vendors"])


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    # A vendor the caller cannot see was answered with 400, where every other
    # module here answers 404. Same information either way — a vendor belonging
    # to another tenant is indistinguishable from one that does not exist — but
    # a client cannot tell "you sent nonsense" from "it is not there", and the
    # inconsistency invites someone to read meaning into the difference.
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.get("/", response_model=List[VendorListResponse])
def list_vendors(
    status_filter: Optional[VendorStatus] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        vendors, _ = VendorService(db).list_vendors(
            current_user=current_user,
            status_filter=status_filter,
            search=search,
            limit=limit,
            offset=offset,
        )
        return vendors
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/review-queue", response_model=List[VendorReviewItem])
def vendor_review_queue(
    limit: int = Query(default=100, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Reviewer worklist: vendors awaiting verification (or blocked), each with
    the count/value of pending-approval invoices the governance gate is holding.

    Activate a vendor (PATCH /vendors/{id}/status) to unblock its invoices.
    Declared before /{vendor_id} so the literal path isn't parsed as a UUID.
    """
    try:
        return VendorService(db).get_review_queue(current_user, limit=limit)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{vendor_id}", response_model=VendorResponse)
def get_vendor(
    vendor_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorService(db).get_vendor(vendor_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
def create_vendor(
    payload: VendorCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorService(db).create_vendor(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.patch("/{vendor_id}", response_model=VendorResponse)
def update_vendor(
    vendor_id: UUID,
    payload: VendorUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorService(db).update_vendor(vendor_id, payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.patch("/{vendor_id}/status", response_model=VendorResponse)
def update_vendor_status(
    vendor_id: UUID,
    payload: VendorStatusUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorService(db).set_status(vendor_id, payload.status, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.delete("/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vendor(
    vendor_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        VendorService(db).delete_vendor(vendor_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
