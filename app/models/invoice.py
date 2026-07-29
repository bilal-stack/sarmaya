from sqlalchemy import Column, String, Integer, Date, Numeric, JSON, ForeignKey, DateTime, Boolean, func, Enum as SQLEnum
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
    
    # Chain identity: links every record across modules that belongs to the
    # same transaction story (Build Book: universal correlation IDs).
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Workflow
    current_state = Column(SQLEnum(InvoiceState), default=InvoiceState.DRAFT)
    # When the invoice entered its current state — the SLA timer start
    # (Build Book: "SLA timers start when a task enters a state").
    state_entered_at = Column(DateTime, server_default=func.now(), nullable=True)
    
    # OCR
    ocr_confidence = Column(Integer, nullable=True)
    ocr_extracted_data = Column(JSON, nullable=True)

    # Duplicate review: a fuzzy-matched likely duplicate found at upload. Stays
    # unacknowledged (blocking approval) until a reviewer overrides with a
    # logged reason.
    potential_duplicate_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)
    duplicate_acknowledged = Column(Boolean, nullable=False, server_default="false", default=False)
    
    # ✅ ADD: Line items
    line_items = Column(JSON, default=[])
    
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
