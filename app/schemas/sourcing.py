from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.enums import RFQState, QuoteState, Currency


class RFQCreate(BaseModel):
    """Open a tender against an approved requisition."""
    requisition_id: UUID
    title: Optional[str] = None
    issued_date: Optional[date] = None
    #: When quoting closes. Past this the field is known, so quotes lock.
    closes_at: Optional[datetime] = None
    currency: Optional[Currency] = None
    vendor_ids: List[UUID] = []


class InviteVendorRequest(BaseModel):
    vendor_id: UUID


class QuoteLineCreate(BaseModel):
    description: str
    product_code: Optional[str] = None
    quantity: Decimal
    unit_price: Decimal

    @field_validator("quantity", "unit_price")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Quantity and unit price must be greater than zero")
        return v


class QuoteCreate(BaseModel):
    """What one vendor offered, as captured by the buyer.

    `total_amount` is used only when the quote came as a headline figure with
    no breakdown; priced lines take precedence, because a comparison can only
    show *where* a vendor is cheaper if the lines are there.
    """
    vendor_id: UUID
    quote_reference: Optional[str] = None
    quote_date: Optional[date] = None
    valid_until: Optional[date] = None
    currency: Optional[Currency] = None
    total_amount: Optional[Decimal] = None
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    #: Does it meet the requirement? A cheaper bid for the wrong specification
    #: is a different quote, not a better one.
    is_compliant: bool = True
    non_compliance_reason: Optional[str] = None
    lines: List[QuoteLineCreate] = []


class AwardRequest(BaseModel):
    """Pick the winner.

    `justification` is required by the service whenever the chosen quote is not
    the lowest compliant one — that is the decision an auditor will always ask
    about, so the reason is captured at the moment it is made.
    """
    quote_id: UUID
    justification: Optional[str] = None


class CancelRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A reason is required")
        return v.strip()


class QuoteLineResponse(BaseModel):
    id: UUID
    line_number: int
    description: str
    product_code: Optional[str] = None
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal

    model_config = ConfigDict(from_attributes=True)


class QuoteResponse(BaseModel):
    id: UUID
    rfq_id: UUID
    vendor_id: UUID
    vendor_name: str
    quote_reference: Optional[str] = None
    quote_date: Optional[date] = None
    valid_until: Optional[date] = None
    currency: Optional[Currency] = None
    total_amount: Decimal
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    notes: Optional[str] = None
    is_compliant: bool
    non_compliance_reason: Optional[str] = None
    current_state: QuoteState
    captured_by: UUID
    created_at: datetime
    lines: List[QuoteLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class InvitedVendorResponse(BaseModel):
    vendor_id: UUID
    vendor_name: str
    invited_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RFQResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    rfq_number: str
    title: str
    requisition_id: UUID
    issued_date: Optional[date] = None
    closes_at: Optional[datetime] = None
    currency: Optional[Currency] = None
    current_state: RFQState
    correlation_id: Optional[UUID] = None
    awarded_quote_id: Optional[UUID] = None
    awarded_by: Optional[UUID] = None
    awarded_at: Optional[datetime] = None
    award_justification: Optional[str] = None
    cancellation_reason: Optional[str] = None
    created_by: UUID
    created_at: datetime
    invited_vendors: List[InvitedVendorResponse] = []
    quotes: List[QuoteResponse] = []

    model_config = ConfigDict(from_attributes=True)


class RFQListResponse(BaseModel):
    id: UUID
    rfq_number: str
    title: str
    current_state: RFQState
    issued_date: Optional[date] = None
    closes_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class QuoteComparisonRow(BaseModel):
    quote_id: UUID
    vendor_id: UUID
    vendor_name: str
    total_amount: Decimal
    currency: Optional[Currency] = None
    lead_time_days: Optional[int] = None
    payment_terms: Optional[str] = None
    is_compliant: bool
    non_compliance_reason: Optional[str] = None
    state: QuoteState
    lines: int


class QuoteComparison(BaseModel):
    """The quotes side by side, and the two facts that frame the decision:
    which is the cheapest compliant offer, and whether it came in above what
    the approval covered."""
    rfq_id: UUID
    rfq_number: str
    state: RFQState
    invited_count: int
    quoted_count: int
    #: Invited and silent. A tender answered by one of five invitees is a
    #: different decision from one answered by all five.
    no_response_vendors: List[str] = []
    lowest_compliant_quote_id: Optional[UUID] = None
    requisition_estimate: Optional[Decimal] = None
    lowest_exceeds_estimate: bool = False
    quotes: List[QuoteComparisonRow] = []
