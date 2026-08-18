from sqlalchemy import Column, String, Integer, JSON, ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, SoftDeleteMixin
from app.core.enums import VendorStatus

class Vendor(BaseModel, SoftDeleteMixin):
    __tablename__ = "vendors"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    legal_name = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    vendor_code = Column(String(100), nullable=True)
    
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    address = Column(String, nullable=True)
    
    # Banking
    bank_account_name = Column(String(255), nullable=True)
    bank_account_number = Column(String(100), nullable=True)
    bank_name = Column(String(255), nullable=True)
    iban = Column(String(50), nullable=True)
    swift_code = Column(String(20), nullable=True)
    
    # Tax
    tax_id = Column(String(100), nullable=True)
    tax_certificate_url = Column(String(500), nullable=True)
    
    # Status
    status = Column(SQLEnum(VendorStatus), default=VendorStatus.ACTIVE)  # active, inactive, blocked, pending_verification
    risk_score = Column(Integer, default=0)
    risk_flags = Column(JSON, default=[])
    
    # Metadata
    custom_metadata = Column(JSON, default={})
    notes = Column(String, nullable=True)
    
    # Audit
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    # Relationships
    tenant = relationship("Tenant", backref="vendors")
    creator = relationship("User", backref="vendors_created")
