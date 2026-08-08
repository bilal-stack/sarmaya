from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import PurchaseOrderState, Currency


class PurchaseOrderLineCreate(BaseModel):
    """One ordered item. Quantity and unit price are required because the
    three-way match compares them against what arrives and what is billed;
    an order without them cannot be matched later."""
    description: str
    product_code: Optional[str] = None
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)

    @field_validator("description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Line description cannot be blank")
        return v.strip()

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.unit_price


class PurchaseOrderCreate(BaseModel):
    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    order_date: Optional[date] = None
    expected_date: Optional[date] = None
    currency: Currency = Currency.PKR
    description: Optional[str] = None
    tax_amount: Decimal = Decimal("0")
    lines: List[PurchaseOrderLineCreate] = []


class PurchaseOrderUpdate(BaseModel):
    """Edits allowed while the order is still a draft."""
    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    expected_date: Optional[date] = None
    description: Optional[str] = None
    tax_amount: Optional[Decimal] = None
    lines: Optional[List[PurchaseOrderLineCreate]] = None


class RejectRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A rejection reason is required")
        return v.strip()


class PurchaseOrderLineResponse(BaseModel):
    id: UUID
    line_number: int
    description: str
    product_code: Optional[str] = None
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    received_quantity: Decimal

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderResponse(BaseModel):
    @field_validator("currency", mode="before")
    @classmethod
    def _currency_default(cls, v):
        """Tolerate a null currency.

        The column is nullable and carries only a Python-side default, so any
        row written outside the ORM — a migration, an import, a seed script —
        has no currency, and a required enum here turns that single row into a
        500 on the read path. The domain default stands in instead; a missing
        currency is a data defect, not a reason the record cannot be read.
        """
        return v if v is not None else Currency.PKR

    id: UUID
    tenant_id: UUID
    po_number: str
    vendor_id: Optional[UUID] = None
    vendor_name: str
    order_date: date
    expected_date: Optional[date] = None
    currency: Currency
    subtotal_amount: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    total_amount: Decimal
    description: Optional[str] = None
    current_state: PurchaseOrderState
    correlation_id: Optional[UUID] = None
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_by: UUID
    created_at: datetime
    lines: List[PurchaseOrderLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class PurchaseOrderListResponse(BaseModel):
    id: UUID
    po_number: str
    vendor_name: str
    order_date: date
    total_amount: Decimal
    current_state: PurchaseOrderState
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GoodsReceiptLineCreate(BaseModel):
    """What arrived against one order line. Negative records a return, so a
    correction is appended rather than editing away the original claim."""
    purchase_order_line_id: UUID
    quantity_received: Decimal


class GoodsReceiptCreate(BaseModel):
    received_date: Optional[date] = None
    delivery_note: Optional[str] = None
    notes: Optional[str] = None
    lines: List[GoodsReceiptLineCreate] = []


class GoodsReceiptLineResponse(BaseModel):
    id: UUID
    line_number: int
    purchase_order_line_id: UUID
    quantity_received: Decimal

    model_config = ConfigDict(from_attributes=True)


class GoodsReceiptResponse(BaseModel):
    id: UUID
    purchase_order_id: UUID
    grn_number: str
    received_date: date
    delivery_note: Optional[str] = None
    notes: Optional[str] = None
    correlation_id: Optional[UUID] = None
    received_by: UUID
    created_at: datetime
    lines: List[GoodsReceiptLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class MatchDiscrepancy(BaseModel):
    kind: str
    detail: str


class ThreeWayMatchResult(BaseModel):
    """Advisory here; enforced by the invoice approval gate."""
    result: str
    reason: str
    purchase_order_id: Optional[str] = None
    po_number: Optional[str] = None
    discrepancies: List[dict] = []
    tolerance: Optional[dict] = None
