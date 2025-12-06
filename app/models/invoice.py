from sqlalchemy import Column, String, Integer, Date, Numeric, JSON, ForeignKey, DateTime, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.core.enums import InvoiceState, Currency

class Invoice(BaseModel):
    __tablename__ = "invoices"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Basic info
    invoice_number = Column(String(100), nullable=False)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    vendor_name = Column(String(255), nullable=False)
    
    # Dates
    invoice_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=True)
    
    # Amounts
    currency = Column(SQLEnum(Currency), default=Currency.PKR)
    subtotal_amount = Column(Numeric(15, 2), nullable=True)
    tax_amount = Column(Numeric(15, 2), nullable=True)
    total_amount = Column(Numeric(15, 2), nullable=False)
    
    description = Column(String, nullable=True)
    
    # Workflow
    current_state = Column(SQLEnum(InvoiceState), default=InvoiceState.DRAFT)
    
    # OCR
    ocr_confidence = Column(Integer, nullable=True)
    ocr_extracted_data = Column(JSON, nullable=True)
    
    # File
    pdf_file_id = Column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    
    # Approval
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejection_reason = Column(String, nullable=True)
    
    # Future links (post-MVP)
    purchase_order_id = Column(UUID(as_uuid=True), nullable=True)
    grn_id = Column(UUID(as_uuid=True), nullable=True)
    payment_batch_id = Column(UUID(as_uuid=True), nullable=True)
    
    # GL
    gl_account_code = Column(String(50), nullable=True)
    cost_center = Column(String(50), nullable=True)
    
    # Metadata
    custom_metadata = Column(JSON, default={})
    tags = Column(JSON, default=[])
    
    # Audit
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    
    # Relationships
    tenant = relationship("Tenant", backref="invoices")
    vendor = relationship("Vendor", backref="invoices")
    approver = relationship("User", foreign_keys=[approved_by], backref="invoices_approved")
    creator = relationship("User", foreign_keys=[created_by], backref="invoices_created")
    pdf_file = relationship("File", backref="invoices")
