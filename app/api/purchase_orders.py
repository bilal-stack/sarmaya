from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.purchase_order import (
    PurchaseOrderCreate, PurchaseOrderUpdate, PurchaseOrderResponse,
    PurchaseOrderListResponse, RejectRequest,
    GoodsReceiptCreate, GoodsReceiptResponse,
)
from app.services.purchase_order_service import PurchaseOrderService
from app.services.goods_receipt_service import GoodsReceiptService

router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"])


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    message = str(exc)
    if "not found" in message.lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.get("", response_model=List[PurchaseOrderListResponse])
def list_purchase_orders(
    state: Optional[str] = None,
    vendor_id: Optional[UUID] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Purchase orders in the caller's tenant."""
    try:
        return PurchaseOrderService(db).list_orders(
            current_user, state=state, vendor_id=vendor_id, limit=limit, offset=offset
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("", response_model=PurchaseOrderResponse, status_code=status.HTTP_201_CREATED)
def create_purchase_order(
    payload: PurchaseOrderCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Raise a draft purchase order.

    Totals are computed from the lines rather than taken from the request, so
    the header can never disagree with what was ordered — the three-way match
    compares against these numbers.
    """
    try:
        return PurchaseOrderService(db).create_order(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{po_id}", response_model=PurchaseOrderResponse)
def get_purchase_order(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PurchaseOrderService(db).get_order(po_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.put("/{po_id}", response_model=PurchaseOrderResponse)
def update_purchase_order(
    po_id: UUID,
    payload: PurchaseOrderUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Edit a draft. Submitted orders are frozen: the approval, the receipt and
    the invoice must all refer to the same order."""
    try:
        return PurchaseOrderService(db).update_order(po_id, payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{po_id}/submit", response_model=PurchaseOrderResponse)
def submit_purchase_order(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Send a draft for approval, routed by the same approval matrix as invoices."""
    try:
        order, _required_role = PurchaseOrderService(db).submit_for_approval(
            po_id, current_user
        )
        return order
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{po_id}/approve", response_model=PurchaseOrderResponse)
def approve_purchase_order(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Approve the spend. Requires purchase_orders.approve, honours delegated
    authority, and refuses an order the approver raised."""
    try:
        return PurchaseOrderService(db).approve_order(po_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{po_id}/reject", response_model=PurchaseOrderResponse)
def reject_purchase_order(
    po_id: UUID,
    payload: RejectRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PurchaseOrderService(db).reject_order(po_id, payload.reason, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{po_id}/issue", response_model=PurchaseOrderResponse)
def issue_purchase_order(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Send an approved order to the vendor — the point after which goods may
    arrive. Guarded on the vendor being verified."""
    try:
        return PurchaseOrderService(db).issue_order(po_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{po_id}/close", response_model=PurchaseOrderResponse)
def close_purchase_order(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return PurchaseOrderService(db).close_order(po_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# ============================================
# GOODS RECEIPTS
# ============================================

@router.get("/{po_id}/receipts", response_model=List[GoodsReceiptResponse])
def list_goods_receipts(
    po_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What has arrived against this order."""
    try:
        return GoodsReceiptService(db).list_for_order(po_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post(
    "/{po_id}/receipts",
    response_model=GoodsReceiptResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_goods_receipt(
    po_id: UUID,
    payload: GoodsReceiptCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Record a delivery against an issued order.

    Requires purchase_orders.receive, which the clerk who raises orders holds
    and the people who approve them do not — whoever confirms goods arrived
    should not also have authorised the spend, or the delivery leg of the
    three-way match verifies nothing.
    """
    try:
        return GoodsReceiptService(db).record_receipt(po_id, payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
