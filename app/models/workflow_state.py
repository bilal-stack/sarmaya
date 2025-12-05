from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
import uuid


class WorkflowState(BaseModel):
    __tablename__ = "workflow_states"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    workflow_type = Column(String(100), nullable=False)  # invoice, purchase_order, grn, etc.
    state_name = Column(String(100), nullable=False)
    display_name = Column(String(100), nullable=True)
    state_order = Column(Integer, nullable=False)
    
    is_initial = Column(Boolean, default=False)
    is_final = Column(Boolean, default=False)
    
    allowed_transitions = Column(JSON, default=[])
    color = Column(String(20), nullable=True)
    
    # Relationships
    tenant = relationship("Tenant", backref="workflow_states")
