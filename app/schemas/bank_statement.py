from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.enums import Currency, PaymentState


class StatementImportRequest(BaseModel):
    """A statement pasted or uploaded as text.

    `source_format` is optional: the parser detects the format from the content,
    because banks name these files anything and a `.txt` holding CAMT XML is
    common. Passing it explicitly overrides detection.
    """
    content: str
    source_format: Optional[str] = None
    filename: Optional[str] = None

    @field_validator("content")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("The statement file is empty")
        return v

    @field_validator("source_format")
    @classmethod
    def _known_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        allowed = {"camt053", "mt940", "csv"}
        if v.strip().lower() not in allowed:
            raise ValueError(f"Unsupported format. Expected one of: {', '.join(sorted(allowed))}")
        return v.strip().lower()


class ConfirmMatchRequest(BaseModel):
    payment_id: UUID


class UnmatchRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A reason is required to undo a match")
        return v.strip()


class MatchCandidateResponse(BaseModel):
    """A suggested payment, with the reasoning that produced it.

    The reasons are returned, not just the score: a reconciler asked to confirm
    a match needs to see why it was proposed, and an opaque number would make
    confirmation a formality.
    """
    payment_id: UUID
    payment_number: str
    payment_date: date
    total_amount: Decimal
    currency: Optional[Currency] = None
    score: int
    confidence: str
    reasons: List[str] = []


class BankStatementLineResponse(BaseModel):
    id: UUID
    line_number: int
    value_date: Optional[date] = None
    booking_date: Optional[date] = None
    amount: Decimal
    is_debit: bool
    currency: Optional[Currency] = None
    description: Optional[str] = None
    counterparty: Optional[str] = None
    bank_reference: Optional[str] = None
    matched_payment_id: Optional[UUID] = None
    matched_by: Optional[UUID] = None
    matched_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class UnexplainedDebitResponse(BankStatementLineResponse):
    """A debit with nothing matched against it, and what might explain it.

    An empty `candidates` list is the meaningful case: it separates "not
    reconciled yet" from "no instruction in this system could have produced
    this", and only the second is a fraud signal.
    """
    statement_reference: Optional[str] = None
    candidates: List[MatchCandidateResponse] = []


class BankStatementResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    statement_reference: str
    account_identifier: Optional[str] = None
    source_format: str
    statement_date: Optional[date] = None
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None
    currency: Optional[Currency] = None
    file_hash: str
    original_filename: Optional[str] = None
    imported_by: UUID
    created_at: datetime
    lines: List[BankStatementLineResponse] = []

    model_config = ConfigDict(from_attributes=True)


class BankStatementListResponse(BaseModel):
    id: UUID
    statement_reference: str
    account_identifier: Optional[str] = None
    source_format: str
    statement_date: Optional[date] = None
    closing_balance: Optional[Decimal] = None
    currency: Optional[Currency] = None
    #: A CSV carries no statement identifier of its own, so every CSV import
    #: reports the same reference and the list cannot tell two files apart.
    #: The filename is the only thing distinguishing them to the person who
    #: downloaded them.
    original_filename: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OutstandingPaymentResponse(BaseModel):
    """A released run the bank has not confirmed."""
    id: UUID
    payment_number: str
    payment_date: date
    total_amount: Decimal
    currency: Optional[Currency] = None
    current_state: PaymentState
    released_at: Optional[datetime] = None
    days_outstanding: int

    model_config = ConfigDict(from_attributes=True)


class ReconciliationSummary(BaseModel):
    """Both sides of the gap.

    Returned together on purpose: a reconciler looking only at outstanding
    payments never sees the debit nobody instructed, which is the item that
    matters most.
    """
    instructed_not_cleared: List[OutstandingPaymentResponse] = []
    cleared_not_instructed: List[UnexplainedDebitResponse] = []
