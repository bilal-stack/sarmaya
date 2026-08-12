from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.enums import RequisitionState, Currency


class RequisitionLineCreate(BaseModel):
    description: str
    product_code: Optional[str] = None
    quantity: Decimal
    #: An *estimate*, not a price — nobody has quoted yet. The distinction is
    #: what lets an approver see later whether the market agreed with them.
    estimated_unit_price: Decimal

    @field_validator("quantity", "estimated_unit_price")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Quantity and estimated unit price must be greater than zero")
        return v


class RequisitionCreate(BaseModel):
    """A request to buy something.

    No vendor field, deliberately: a requisition states a need, and naming a
    supplier here would let the requester pre-select the winner before anyone
    has quoted.
    """
    title: str
    #: What the approver is actually deciding on, so it is required rather than
    #: an optional note.
    justification: str
    budget_code: Optional[str] = None
    department: Optional[str] = None
    requested_date: Optional[date] = None
    needed_by: Optional[date] = None
    currency: Optional[Currency] = None
    lines: List[RequisitionLineCreate] = []

    @field_validator("title")
    @classmethod
    def _title_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A requisition needs a title")
        return v.strip()

    @field_validator("justification")
    @classmethod
    def _justification_required(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError(
                "Give a justification an approver can act on (at least 10 characters)"
            )
        return v.strip()


class RejectRequisitionRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A rejection reason is required")
        return v.strip()


class RequisitionLineResponse(BaseModel):
    id: UUID
    line_number: int
    description: str
    product_code: Optional[str] = None
    quantity: Decimal
    estimated_unit_price: Decimal
    estimated_amount: Decimal

    model_config = ConfigDict(from_attributes=True)


class RequisitionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    requisition_number: str
    title: str
    justification: str
    budget_code: Optional[str] = None
    department: Optional[str] = None
    requested_date: date
    needed_by: Optional[date] = None
    currency: Optional[Currency] = None
    estimated_amount: Decimal
    current_state: RequisitionState
    correlation_id: Optional[UUID] = None
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_by: UUID
    created_at: datetime
    lines: List[RequisitionLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class RequisitionListResponse(BaseModel):
    id: UUID
    requisition_number: str
    title: str
    department: Optional[str] = None
    estimated_amount: Decimal
    currency: Optional[Currency] = None
    current_state: RequisitionState
    requested_date: date
    needed_by: Optional[date] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
