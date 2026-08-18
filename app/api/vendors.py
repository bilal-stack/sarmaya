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
from app.services.vendor_bank_service import VendorBankService
from app.schemas.vendor_bank_change import (
    BankChangeRequest, RejectBankChangeRequest, BankChangeResponse,
)

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


@router.get("/bank-changes", response_model=List[BankChangeResponse])
def list_bank_changes(
    vendor_id: Optional[UUID] = None,
    state: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Proposed and historical bank changes, newest first.

    Declared before /{vendor_id} so the literal path is not parsed as a UUID —
    the same reason review-queue sits above it.
    """
    try:
        return [
            BankChangeResponse.for_user(c, current_user)
            for c in VendorBankService(db).list_changes(
                current_user, vendor_id=vendor_id, state=state
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{vendor_id}", response_model=VendorResponse)
def get_vendor(
    vendor_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorResponse.for_user(
            VendorService(db).get_vendor(vendor_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
def create_vendor(
    payload: VendorCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return VendorResponse.for_user(
            VendorService(db).create_vendor(payload, current_user), current_user
        )
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
        return VendorResponse.for_user(
            VendorService(db).update_vendor(vendor_id, payload, current_user),
            current_user,
        )
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
        return VendorResponse.for_user(
            VendorService(db).set_status(vendor_id, payload.status, current_user),
            current_user,
        )
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


# --- bank detail changes -----------------------------------------------------
#
# Build Book A1 control: vendor bank change verification with dual approval and
# a cooling period. Bank fields are refused by PATCH /vendors/{id} and come
# through here instead, because redirecting a vendor's payments is the most
# common invoice fraud there is and every downstream control passes while it
# happens: the invoice is genuine, the approval is genuine, the release is
# genuine. Only the destination changed.


@router.post("/{vendor_id}/bank-change", response_model=BankChangeResponse,
             status_code=status.HTTP_201_CREATED)
def request_bank_change(
    vendor_id: UUID,
    payload: BankChangeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Propose new bank details.

    Records the old values alongside the new, so the trail shows the
    substitution rather than only the result. Payments to this vendor are held
    from now until the change is resolved — including payments to the *old*
    account, because during a disputed change neither destination is known to
    be right.
    """
    try:
        return BankChangeResponse.for_user(
            VendorBankService(db).request_change(vendor_id, payload, current_user),
            current_user,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/bank-changes/{change_id}/approve", response_model=BankChangeResponse)
def approve_bank_change(
    change_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Agree the new account is legitimate, starting the cooling period.

    Requires vendors.approve_bank_change, which whoever maintains vendors
    deliberately does not hold, and refuses the requester with no admin
    exemption — this is the exact step the fraud needs.

    Does not change the vendor. Approval starts a clock; the details are
    applied separately once it has run.
    """
    try:
        return BankChangeResponse.for_user(
            VendorBankService(db).approve_change(change_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/bank-changes/{change_id}/apply", response_model=VendorResponse)
def apply_bank_change(
    change_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Write the approved details onto the vendor, once the clock has run.

    Refused while the cooling period is still going. The wait is the control:
    it is the window in which the real vendor can say they never asked for this.
    """
    try:
        return VendorResponse.for_user(
            VendorBankService(db).apply_change(change_id, current_user), current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/bank-changes/{change_id}/reject", response_model=BankChangeResponse)
def reject_bank_change(
    change_id: UUID,
    payload: RejectBankChangeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return BankChangeResponse.for_user(
            VendorBankService(db).reject_change(change_id, payload.reason, current_user),
            current_user,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/bank-changes/{change_id}/cancel", response_model=BankChangeResponse)
def cancel_bank_change(
    change_id: UUID,
    payload: RejectBankChangeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Withdraw a request you raised."""
    try:
        return BankChangeResponse.for_user(
            VendorBankService(db).cancel_change(change_id, payload.reason, current_user),
            current_user,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)
