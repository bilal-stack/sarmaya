from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any, List
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from app.core.enums import InvoiceState, Currency


class InvoiceBase(BaseModel):
    invoice_number: str
    vendor_name: str
    invoice_date: date
    due_date: Optional[date] = None
    total_amount: Decimal
    tax_amount: Optional[Decimal] = None
    subtotal_amount: Optional[Decimal] = None
    description: Optional[str] = None
    currency: Currency = Currency.PKR


class InvoiceCreate(InvoiceBase):
    # Optional explicit link to a vendor master record. When given it takes
    # precedence over vendor_name (the vendor's legal_name becomes canonical).
    vendor_id: Optional[UUID] = None


class InvoiceUpdate(BaseModel):
    invoice_number: Optional[str] = None
    vendor_name: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    total_amount: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    description: Optional[str] = None


class InvoiceResponse(InvoiceBase):
    id: UUID
    tenant_id: UUID
    vendor_id: Optional[UUID] = None
    current_state: InvoiceState
    ocr_confidence: Optional[int] = None
    ocr_extracted_data: Optional[Dict[str, Any]] = None
    line_items: Optional[List[Dict[str, Any]]] = []  # ✅ ADD
    pdf_file_id: Optional[UUID] = None
    approved_by: Optional[UUID] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    potential_duplicate_id: Optional[UUID] = None
    duplicate_acknowledged: bool = False
    # The transaction chain this invoice belongs to. Exposed so the audit
    # console can be reached from the invoice itself — without it, evidence
    # packs and chain lookups are only addressable by reading the database.
    # Null for invoices created before correlation IDs were introduced.
    correlation_id: Optional[UUID] = None
    created_by: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvoiceListResponse(BaseModel):
    id: UUID
    invoice_number: str
    vendor_id: Optional[UUID] = None
    vendor_name: str
    invoice_date: date
    total_amount: Decimal
    current_state: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvoiceUploadResponse(BaseModel):
    success: bool
    invoice_id: Optional[UUID] = None
    invoice_number: Optional[str] = None
    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    vendor_status: Optional[str] = None
    invoice_date: Optional[str] = None
    total_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    currency: Optional[str] = None
    current_state: Optional[str] = None
    ocr_confidence: Optional[int] = None
    ocr_data: Optional[Dict[str, Any]] = None
    field_explanations: Optional[Dict[str, Any]] = None
    duplicate_warning: Optional[Dict[str, Any]] = None
    file_id: Optional[UUID] = None
    error: Optional[str] = None
    message: Optional[str] = None
    existing_invoice_id: Optional[UUID] = None


class InvoiceStats(BaseModel):
    by_status: Dict[str, Dict[str, Any]]
    total_invoices: int
    total_amount: float
    this_month: Dict[str, Any]
