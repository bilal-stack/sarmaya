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

    model_config = ConfigDict(from_attributes=True)


class VendorListResponse(BaseModel):
    id: UUID
    legal_name: str
    display_name: Optional[str] = None
    vendor_code: Optional[str] = None
    email: Optional[str] = None
    status: VendorStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
