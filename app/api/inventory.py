"""Inventory: items, locations, stock, adjustments, quality checks, returns.

Build Book Variant D1. One router because these are one module — an adjustment
is meaningless without the balance it changes, and a quality check without the
receipt it inspected.

The routes worth reading twice are the approvals. `POST /adjustments/{id}/approve`
is called once for a small adjustment and twice, by two different people, for
one over the threshold; the service decides which, and returns the record so
the caller can see whether it posted or is still waiting for a second
signature. That is deliberate — an endpoint named `/second-approve` would let a
client choose which signature it was providing.
"""
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.models.inventory import REASON_CODES
from app.services.inventory_adjustment_service import InventoryAdjustmentService
from app.services.item_catalog_service import ItemCatalogService
from app.services.quality_check_service import QualityCheckService
from app.services.receiving_exception_service import ReceivingExceptionService
from app.services.stock_service import InsufficientStock, StockService
from app.services.vendor_return_service import VendorReturnService

router = APIRouter(prefix="/inventory", tags=["Inventory"])


def _raise_for(exc: Exception):
    """Refusal is 403, insufficient stock is 409, bad input is 400.

    Stock is a conflict rather than a bad request: the request was well formed
    and would have been fine a moment earlier or against a different balance,
    which is exactly what 409 means and what a client needs to distinguish to
    know whether retrying could ever help.
    """
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, InsufficientStock):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --- schemas ---------------------------------------------------------------

class ItemCreate(BaseModel):
    sku: str
    name: str
    uom: str = "each"
    category: Optional[str] = None
    description: Optional[str] = None
    is_stocked: bool = True
    reorder_point: Optional[Decimal] = None
    standard_cost: Optional[Decimal] = None


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    uom: Optional[str] = None
    reorder_point: Optional[Decimal] = None
    standard_cost: Optional[Decimal] = None
    is_active: Optional[bool] = None


class ItemResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    uom: str
    category: Optional[str] = None
    is_stocked: bool
    reorder_point: Optional[Decimal] = None
    standard_cost: Optional[Decimal] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class LocationCreate(BaseModel):
    code: str
    name: str
    org_unit_id: Optional[UUID] = None
    is_receiving_bay: bool = False
    is_quarantine: bool = False


class LocationResponse(BaseModel):
    id: UUID
    code: str
    name: str
    org_unit_id: Optional[UUID] = None
    is_receiving_bay: bool
    is_quarantine: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class AdjustmentLineIn(BaseModel):
    item_id: UUID
    quantity_change: Decimal
    note: Optional[str] = None

    @field_validator("quantity_change")
    @classmethod
    def _not_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("a change of zero adjusts nothing")
        return v


class AdjustmentCreate(BaseModel):
    location_id: UUID
    reason_code: str
    reason_note: Optional[str] = None
    lines: List[AdjustmentLineIn] = Field(min_length=1)

    @field_validator("reason_code")
    @classmethod
    def _known_reason(cls, v: str) -> str:
        if v not in REASON_CODES:
            raise ValueError(f"one of: {', '.join(REASON_CODES)}")
        return v


class RejectIn(BaseModel):
    reason: str


class QualityCheckIn(BaseModel):
    quantity_accepted: Decimal = Decimal("0")
    quantity_rejected: Decimal = Decimal("0")
    reason_code: Optional[str] = None
    notes: Optional[str] = None


class PutawayIn(BaseModel):
    destination_id: UUID
    quantity: Decimal


class ReturnLineIn(BaseModel):
    item_id: UUID
    quantity: Decimal
    goods_receipt_line_id: Optional[UUID] = None
    note: Optional[str] = None


class ReturnCreate(BaseModel):
    vendor_id: UUID
    location_id: UUID
    reason_code: str
    reason_note: Optional[str] = None
    purchase_order_id: Optional[UUID] = None
    lines: List[ReturnLineIn] = Field(min_length=1)


class CreditIn(BaseModel):
    credit_note_reference: str


# --- items and locations ---------------------------------------------------

@router.get("/items", response_model=List[ItemResponse])
def list_items(
    active_only: bool = True,
    category: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ItemCatalogService(db).list_items(
            current_user, active_only=active_only, category=category
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/items", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: ItemCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ItemCatalogService(db).create_item(
            current_user, **payload.model_dump()
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.patch("/items/{item_id}", response_model=ItemResponse)
def update_item(
    item_id: UUID,
    payload: ItemUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ItemCatalogService(db).update_item(
            item_id, current_user, **payload.model_dump(exclude_unset=True)
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/locations", response_model=List[LocationResponse])
def list_locations(
    active_only: bool = True,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ItemCatalogService(db).list_locations(
            current_user, active_only=active_only
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post(
    "/locations", response_model=LocationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_location(
    payload: LocationCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ItemCatalogService(db).create_location(
            current_user, **payload.model_dump()
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- stock -----------------------------------------------------------------

@router.get("/stock")
def stock_on_hand(
    location_id: Optional[UUID] = None,
    include_zero: bool = False,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What is on hand, by item and location, with reorder flags."""
    try:
        return StockService(db).balances(
            current_user, location_id=location_id, include_zero=include_zero
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/movements")
def stock_movements(
    item_id: Optional[UUID] = None,
    location_id: Optional[UUID] = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The ledger: why a balance is what it is.

    This is the question a stored quantity can never answer, which is why the
    balance is derived from these rather than the other way round.
    """
    try:
        movements = StockService(db).movements(
            current_user, item_id=item_id, location_id=location_id, limit=limit
        )
        return [
            {
                "id": m.id,
                "item_id": m.item_id,
                "location_id": m.location_id,
                "quantity": float(m.quantity),
                "movement_type": m.movement_type,
                "reason_code": m.reason_code,
                "source_type": m.source_type,
                "source_id": m.source_id,
                "note": m.note,
                "created_at": m.created_at,
            }
            for m in movements
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/reconcile")
def reconcile(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Where the stored balance disagrees with the ledger.

    Should always be empty. A non-empty answer means something wrote a balance
    without a movement, which is a bug rather than a data-entry problem — so
    the discrepancies are reported and deliberately not silently corrected.
    """
    try:
        StockService(db)._require_view(current_user)
        return {"discrepancies": StockService(db).reconcile_balances()}
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- adjustments -----------------------------------------------------------

@router.get("/adjustments")
def list_adjustments(
    state: Optional[str] = None,
    location_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            _adjustment_dict(a)
            for a in InventoryAdjustmentService(db).list_adjustments(
                current_user, state=state, location_id=location_id
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/adjustments/{adjustment_id}")
def get_adjustment(
    adjustment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _adjustment_dict(
            InventoryAdjustmentService(db).get(adjustment_id, current_user),
            with_lines=True,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/adjustments", status_code=status.HTTP_201_CREATED)
def create_adjustment(
    payload: AdjustmentCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        adjustment = InventoryAdjustmentService(db).create(
            current_user,
            location_id=payload.location_id,
            reason_code=payload.reason_code,
            reason_note=payload.reason_note,
            lines=[line.model_dump() for line in payload.lines],
        )
        return _adjustment_dict(adjustment, with_lines=True)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/adjustments/{adjustment_id}/submit")
def submit_adjustment(
    adjustment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _adjustment_dict(
            InventoryAdjustmentService(db).submit(adjustment_id, current_user)
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/adjustments/{adjustment_id}/approve")
def approve_adjustment(
    adjustment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Approve, and post once every required signature is present.

    One route for both signatures. The service decides whether this call is the
    first or the second, because a client that could choose would be able to
    provide the second signature without the first ever existing.
    """
    try:
        return _adjustment_dict(
            InventoryAdjustmentService(db).approve(adjustment_id, current_user)
        )
    except (ValueError, PermissionError, InsufficientStock) as e:
        _raise_for(e)


@router.post("/adjustments/{adjustment_id}/reject")
def reject_adjustment(
    adjustment_id: UUID,
    payload: RejectIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _adjustment_dict(
            InventoryAdjustmentService(db).reject(
                adjustment_id, current_user, payload.reason
            )
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/adjustments/{adjustment_id}/cancel")
def cancel_adjustment(
    adjustment_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _adjustment_dict(
            InventoryAdjustmentService(db).cancel(adjustment_id, current_user)
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- quality checks --------------------------------------------------------

@router.post("/receipt-lines/{receipt_line_id}/quality-check")
def record_quality_check(
    receipt_line_id: UUID,
    payload: QualityCheckIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Inspect a delivered line. Rejected stock moves to quarantine."""
    try:
        check = QualityCheckService(db).record(
            receipt_line_id, current_user,
            quantity_accepted=payload.quantity_accepted,
            quantity_rejected=payload.quantity_rejected,
            reason_code=payload.reason_code,
            notes=payload.notes,
        )
        return {
            "id": check.id,
            "outcome": check.outcome,
            "quantity_accepted": float(check.quantity_accepted),
            "quantity_rejected": float(check.quantity_rejected),
            "reason_code": check.reason_code,
            "notes": check.notes,
            "inspected_at": check.inspected_at,
        }
    except (ValueError, PermissionError, InsufficientStock) as e:
        _raise_for(e)


@router.post("/receipt-lines/{receipt_line_id}/putaway")
def putaway(
    receipt_line_id: UUID,
    payload: PutawayIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        QualityCheckService(db).putaway(
            receipt_line_id, payload.destination_id, payload.quantity, current_user
        )
        return {"success": True}
    except (ValueError, PermissionError, InsufficientStock) as e:
        _raise_for(e)


@router.get("/receipt-lines/{receipt_line_id}/exception")
def explain_exception(
    receipt_line_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Why a delivery did not match its order, and what to do next.

    The computed half — short, over, late, rejected — is always present. The AI
    explanation is added when it is available and confident, and its absence is
    stated rather than left blank: a receiving clerk deciding whether to reject
    a delivery needs an answer now, not a screen that looks broken.
    """
    try:
        return ReceivingExceptionService(db).explain(receipt_line_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/uninspected")
def uninspected(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Delivered but never checked — stock the system believes it has and
    nobody has confirmed."""
    try:
        return QualityCheckService(db).uninspected_lines(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- returns ---------------------------------------------------------------

@router.get("/returns")
def list_returns(
    state: Optional[str] = None,
    vendor_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            _return_dict(r)
            for r in VendorReturnService(db).list_returns(
                current_user, state=state, vendor_id=vendor_id
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/returns", status_code=status.HTTP_201_CREATED)
def create_return(
    payload: ReturnCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        vendor_return = VendorReturnService(db).create(
            current_user,
            vendor_id=payload.vendor_id,
            location_id=payload.location_id,
            reason_code=payload.reason_code,
            reason_note=payload.reason_note,
            purchase_order_id=payload.purchase_order_id,
            lines=[line.model_dump() for line in payload.lines],
        )
        return _return_dict(vendor_return)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/returns/{return_id}/{action}")
def act_on_return(
    return_id: UUID,
    action: str,
    payload: Optional[dict] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """submit | approve | dispatch | reject | cancel | credit.

    One route because these are the same kind of step on the same record, and
    six near-identical handlers is six places for one to drift.
    """
    service = VendorReturnService(db)
    payload = payload or {}

    try:
        if action == "submit":
            result = service.submit(return_id, current_user)
        elif action == "approve":
            result = service.approve(return_id, current_user)
        elif action == "dispatch":
            result = service.dispatch(return_id, current_user)
        elif action == "reject":
            result = service.reject(return_id, current_user, payload.get("reason", ""))
        elif action == "cancel":
            result = service.cancel(return_id, current_user)
        elif action == "credit":
            result = service.record_credit(
                return_id, current_user, payload.get("credit_note_reference", "")
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unknown action. One of: submit, approve, dispatch, "
                       "reject, cancel, credit",
            )
        return _return_dict(result)
    except (ValueError, PermissionError, InsufficientStock) as e:
        _raise_for(e)


# --- rendering -------------------------------------------------------------

def _adjustment_dict(adjustment, with_lines: bool = False) -> dict:
    data = {
        "id": adjustment.id,
        "adjustment_number": adjustment.adjustment_number,
        "location_id": adjustment.location_id,
        "reason_code": adjustment.reason_code,
        "reason_note": adjustment.reason_note,
        "current_state": adjustment.current_state,
        "total_value": float(adjustment.total_value or 0),
        "requires_dual_approval": adjustment.requires_dual_approval,
        "approved_by": adjustment.approved_by,
        "second_approved_by": adjustment.second_approved_by,
        "posted_at": adjustment.posted_at,
        "created_by": adjustment.created_by,
        "created_at": adjustment.created_at,
        "correlation_id": adjustment.correlation_id,
    }
    if with_lines:
        data["lines"] = [
            {
                "line_number": line.line_number,
                "item_id": line.item_id,
                "quantity_change": float(line.quantity_change),
                "quantity_before": (
                    float(line.quantity_before)
                    if line.quantity_before is not None else None
                ),
                "unit_cost": float(line.unit_cost) if line.unit_cost else None,
                "note": line.note,
            }
            for line in sorted(adjustment.lines, key=lambda x: x.line_number)
        ]
    return data


def _return_dict(vendor_return) -> dict:
    return {
        "id": vendor_return.id,
        "return_number": vendor_return.return_number,
        "vendor_id": vendor_return.vendor_id,
        "location_id": vendor_return.location_id,
        "reason_code": vendor_return.reason_code,
        "vendor_attributable": vendor_return.vendor_attributable,
        "current_state": vendor_return.current_state,
        "total_value": float(vendor_return.total_value or 0),
        "approved_by": vendor_return.approved_by,
        "dispatched_at": vendor_return.dispatched_at,
        "credit_note_reference": vendor_return.credit_note_reference,
        "credited_at": vendor_return.credited_at,
        "created_at": vendor_return.created_at,
        "correlation_id": vendor_return.correlation_id,
    }
