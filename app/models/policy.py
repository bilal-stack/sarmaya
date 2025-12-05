from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
import uuid


class Policy(BaseModel):
    __tablename__ = "policies"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    
    policy_type = Column(String(100), nullable=False)  # approval_limit, document_required, etc.
    policy_name = Column(String(255), nullable=False)
    description = Column(String, nullable=True)
    
    rule_config = Column(JSON, nullable=False)
    applies_to = Column(String(100), nullable=True)  # invoice, purchase_order, all
    
    is_active = Column(Boolean, default=True)
    priority = Column(Integer, default=0)
    
    # Relationships
    tenant = relationship("Tenant", backref="policies")
