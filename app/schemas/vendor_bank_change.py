from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.enums import BankChangeState


class BankChangeRequest(BaseModel):
    """Propose new bank details for a vendor.

    Every field is optional individually — a vendor may change only their IBAN
    — but at least one must be given, and the reason never is. "They emailed
    us" and "they sent a letter we rang the old number to confirm" are
    different answers, and the approver is deciding on exactly that difference.
    """
    reason: str
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban: Optional[str] = None
    swift_code: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError(
                "Say how this change was received and how it was verified "
                "(at least 10 characters). It is what the approver is judging."
            )
        return v.strip()

    @model_validator(mode="after")
    def _at_least_one_detail(self):
        if not any([
            self.bank_account_name, self.bank_account_number,
            self.bank_name, self.iban, self.swift_code,
        ]):
            raise ValueError("Propose at least one new bank detail")
        return self


class RejectBankChangeRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A reason is required")
        return v.strip()


class BankChangeResponse(BaseModel):
    """A proposed change, with both sides of it.

    The old values are returned alongside the new because the substitution is
    the thing a reviewer is looking at — an approver shown only the new account
    number has nothing to compare it against.
    """
    id: UUID
    tenant_id: UUID
    vendor_id: UUID
    reason: str

    new_bank_account_name: Optional[str] = None
    new_bank_account_number: Optional[str] = None
    new_bank_name: Optional[str] = None
    new_iban: Optional[str] = None
    new_swift_code: Optional[str] = None

    old_bank_account_name: Optional[str] = None
    old_bank_account_number: Optional[str] = None
    old_bank_name: Optional[str] = None
    old_iban: Optional[str] = None
    old_swift_code: Optional[str] = None

    current_state: BankChangeState
    requested_by: UUID
    requested_at: datetime
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    #: When a payment may first use the new details. Until then payments to
    #: this vendor are held.
    effective_at: Optional[datetime] = None
    applied_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    #: False when the account fields above are masked.
    bank_details_visible: bool = True

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def for_user(cls, change, current_user: dict) -> "BankChangeResponse":
        """Serialise a change, masking both accounts unless the caller holds
        `vendors.view_bank_details`.

        Listing changes needs only `vendors.view`, which the read-only auditor
        holds — so masking the vendor record while leaving this open would have
        been theatre: the same account numbers are here, old and new together.
        """
        from app.core.roles import has_permission, PERM_VIEW_BANK_DETAILS
        from app.utils.masking import mask_account

        response = cls.model_validate(change)
        if has_permission(current_user["role"], PERM_VIEW_BANK_DETAILS):
            return response

        for field in ("new_bank_account_number", "new_iban",
                      "old_bank_account_number", "old_iban"):
            setattr(response, field, mask_account(getattr(response, field)))
        response.bank_details_visible = False
        return response
