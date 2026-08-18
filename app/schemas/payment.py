from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.enums import PaymentState, Currency


class PaymentCreate(BaseModel):
    """Prepare a run over a set of approved invoices.

    Amounts are not accepted from the caller: each line is settled at the
    invoice's own total, so a run cannot quietly pay a different figure from
    the one that was approved.
    """
    invoice_ids: List[UUID]
    payment_date: Optional[date] = None
    method: str = "bank_transfer"
    reference: Optional[str] = None
    notes: Optional[str] = None
    currency: Optional[Currency] = None

    @field_validator("invoice_ids")
    @classmethod
    def _at_least_one(cls, v):
        if not v:
            raise ValueError("A payment must settle at least one invoice")
        return v


class RejectPaymentRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A rejection reason is required")
        return v.strip()


class PaymentLineResponse(BaseModel):
    """One invoice being settled, with the destination copied at preparation.

    These carry the same account identifiers as the vendor record, so masking
    the vendor while leaving these open would just move the leak: the auditor
    holds payments.view.
    """
    id: UUID
    line_number: Decimal
    invoice_id: UUID
    amount: Decimal
    vendor_id: Optional[UUID] = None
    vendor_name: str
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban: Optional[str] = None
    swift_code: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PaymentResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    payment_number: str
    payment_date: date
    method: str
    reference: Optional[str] = None
    notes: Optional[str] = None
    currency: Optional[Currency] = None
    total_amount: Decimal
    current_state: PaymentState
    correlation_id: Optional[UUID] = None
    prepared_by: UUID
    released_by: Optional[UUID] = None
    #: Resolved server-side. Maker-checker is the point of a payment run, and
    #: two raw UUIDs prove nothing to the person reading the screen — while
    #: reading them from the user directory would need users.view, which the
    #: clerks who prepare runs deliberately do not have.
    prepared_by_name: Optional[str] = None
    released_by_name: Optional[str] = None
    released_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    bank_file_hash: Optional[str] = None
    bank_file_generated_at: Optional[datetime] = None
    created_at: datetime
    lines: List[PaymentLineResponse] = []
    #: False when the destination accounts on the lines are masked.
    bank_details_visible: bool = True

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def for_user(cls, payment, current_user: dict) -> "PaymentResponse":
        """Serialise a run, masking each line's destination unless the caller
        holds `vendors.view_bank_details`."""
        from app.core.roles import has_permission, PERM_VIEW_BANK_DETAILS
        from app.utils.masking import mask_account

        response = cls.model_validate(payment)
        if has_permission(current_user["role"], PERM_VIEW_BANK_DETAILS):
            return response

        for line in response.lines:
            line.bank_account_number = mask_account(line.bank_account_number)
            line.iban = mask_account(line.iban)
        response.bank_details_visible = False
        return response


class PaymentListResponse(BaseModel):
    id: UUID
    payment_number: str
    payment_date: date
    total_amount: Decimal
    current_state: PaymentState
    prepared_by: UUID
    released_by: Optional[UUID] = None
    prepared_by_name: Optional[str] = None
    released_by_name: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PayableInvoice(BaseModel):
    """An approved invoice not already claimed by an open or released run."""
    id: UUID
    invoice_number: str
    vendor_name: str
    invoice_date: date
    total_amount: Decimal

    model_config = ConfigDict(from_attributes=True)
