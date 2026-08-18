from pydantic import BaseModel, ConfigDict, EmailStr, field_validator
from typing import Optional
from datetime import datetime
from uuid import UUID

from app.core.enums import VendorStatus


class VendorBase(BaseModel):
    legal_name: str
    display_name: Optional[str] = None
    vendor_code: Optional[str] = None

    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None

    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban: Optional[str] = None
    swift_code: Optional[str] = None

    tax_id: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("legal_name")
    @classmethod
    def legal_name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("legal_name is required")
        return v.strip()


class VendorCreate(VendorBase):
    status: VendorStatus = VendorStatus.ACTIVE


class VendorUpdate(BaseModel):
    legal_name: Optional[str] = None
    display_name: Optional[str] = None
    vendor_code: Optional[str] = None

    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None

    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_name: Optional[str] = None
    iban: Optional[str] = None
    swift_code: Optional[str] = None

    tax_id: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("legal_name")
    @classmethod
    def legal_name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("legal_name cannot be blank")
        return v.strip() if v else v


class VendorStatusUpdate(BaseModel):
    status: VendorStatus


class VendorResponse(VendorBase):
    id: UUID
    tenant_id: UUID
    status: VendorStatus
    risk_score: int
    created_at: datetime
    updated_at: datetime
    #: False when the account fields above are masked, so a client can say
    #: "hidden for your role" rather than rendering bullets as if that were
    #: the stored value.
    bank_details_visible: bool = True

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def for_user(cls, vendor, current_user: dict) -> "VendorResponse":
        """Serialise a vendor, masking the account identifiers unless the
        caller holds `vendors.view_bank_details`.

        Build this through here rather than returning the ORM row from the
        endpoint: FastAPI would serialise every field the model declares, and
        the response model cannot see who is asking.
        """
        from app.core.roles import has_permission, PERM_VIEW_BANK_DETAILS
        from app.utils.masking import mask_account

        response = cls.model_validate(vendor)
        if has_permission(current_user["role"], PERM_VIEW_BANK_DETAILS):
            return response

        response.bank_account_number = mask_account(response.bank_account_number)
        response.iban = mask_account(response.iban)
        response.bank_details_visible = False
        return response


class VendorListResponse(BaseModel):
    id: UUID
    legal_name: str
    display_name: Optional[str] = None
    vendor_code: Optional[str] = None
    email: Optional[str] = None
    status: VendorStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VendorReviewItem(VendorListResponse):
    """A non-ACTIVE vendor on the reviewer worklist, annotated with the volume
    of pending-approval invoices the governance gate is holding for it."""
    blocked_invoice_count: int
    blocked_total_amount: float
