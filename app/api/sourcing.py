from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.sourcing import (
    RFQCreate, RFQResponse, RFQListResponse, InviteVendorRequest,
    QuoteCreate, QuoteResponse, AwardRequest, CancelRequest, QuoteComparison,
)
from app.schemas.purchase_order import PurchaseOrderResponse
from app.services.sourcing_service import SourcingService

router = APIRouter(prefix="/rfqs", tags=["Sourcing"])


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.get("", response_model=List[RFQListResponse])
def list_rfqs(
    state: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return SourcingService(db).list_rfqs(current_user, state=state)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("", response_model=RFQResponse, status_code=status.HTTP_201_CREATED)
def create_rfq(
    payload: RFQCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Open a tender against an approved requisition.

    The requisition must already be approved: going to market on an unapproved
    need commits the company's name to a purchase nobody authorised. The RFQ
    inherits the requisition's correlation id, so sourcing appears in the same
    story as the need that prompted it.
    """
    try:
        return SourcingService(db).create_rfq(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{rfq_id}", response_model=RFQResponse)
def get_rfq(
    rfq_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return SourcingService(db).get_rfq(rfq_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{rfq_id}/comparison", response_model=QuoteComparison)
def compare_quotes(
    rfq_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The quotes side by side.

    Names the lowest *compliant* quote rather than the lowest — a cheaper bid
    for the wrong specification is a different quote, not a better one — and
    lists the vendors who were invited and never answered, because a tender
    answered by one of five invitees is a different decision from one answered
    by all five.
    """
    try:
        return SourcingService(db).compare_quotes(rfq_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/vendors", response_model=RFQResponse)
def invite_vendor(
    rfq_id: UUID,
    payload: InviteVendorRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Invite one more vendor to quote. Blocked vendors are refused, and
    nobody can be invited once quoting has closed."""
    try:
        return SourcingService(db).invite_vendor(
            rfq_id, payload.vendor_id, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/issue", response_model=RFQResponse)
def issue_rfq(
    rfq_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Send it out. Refused with fewer than two invited vendors — for a single
    source, raise the order directly rather than dressing it as a tender."""
    try:
        return SourcingService(db).issue_rfq(rfq_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/quotes", response_model=QuoteResponse,
             status_code=status.HTTP_201_CREATED)
def record_quote(
    rfq_id: UUID,
    payload: QuoteCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Capture what a vendor offered.

    Vendors do not log in, so a buyer enters this and the record says who typed
    it. Only invited vendors may be quoted, one quote each, and nothing may be
    recorded once the RFQ has closed.
    """
    try:
        return SourcingService(db).record_quote(rfq_id, payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/close", response_model=RFQResponse)
def close_rfq(
    rfq_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """End quoting. After this no quote may be added or altered by anyone,
    including the buyer — which is what makes the quotes evidence rather than a
    record of what someone wrote down once the field was known."""
    try:
        return SourcingService(db).close_rfq(rfq_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/award", response_model=RFQResponse)
def award_quote(
    rfq_id: UUID,
    payload: AwardRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Pick the winner.

    Requires sourcing.award, which the buyer who ran the tender deliberately
    does not hold. Awarding anything other than the lowest compliant quote
    requires a written reason — recorded on the award and in the audit trail,
    with the figure it was measured against.
    """
    try:
        return SourcingService(db).award_quote(
            rfq_id, payload.quote_id, payload.justification, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/convert", response_model=PurchaseOrderResponse,
             status_code=status.HTTP_201_CREATED)
def convert_award_to_order(
    rfq_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Raise the purchase order the award decided on.

    Where the upstream half joins the downstream one. The order inherits the
    requisition's correlation id, so the whole story reads end to end: need,
    tender, quotes, award, order, receipt, invoice, payment, bank line.

    Refused if the award exceeds the approved estimate — the approval was
    granted against that figure, and the market coming back higher needs the
    requisition re-approved rather than quietly absorbed. The requisition is
    marked converted, so one approval cannot cover two orders.
    """
    try:
        return SourcingService(db).convert_award_to_order(rfq_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{rfq_id}/cancel", response_model=RFQResponse)
def cancel_rfq(
    rfq_id: UUID,
    payload: CancelRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return SourcingService(db).cancel_rfq(rfq_id, payload.reason, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
