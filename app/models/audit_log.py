from sqlalchemy import Column, String, JSON, ForeignKey, DateTime, Boolean, Integer
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.orm import relationship
from app.models.base import BaseModel, UTC_NOW


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Who
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    user_email = Column(String(255), nullable=True)
    user_role = Column(String(50), nullable=True)
    
    # What
    object_type = Column(String(50), nullable=False)  # invoice, vendor, user, policy, etc.
    object_id = Column(UUID(as_uuid=True), nullable=False)
    
    action = Column(String(100), nullable=False)  # created, updated, approved, rejected, etc.
    
    # Details
    before_value = Column(JSON, nullable=True)
    after_value = Column(JSON, nullable=True)
    changes = Column(JSON, nullable=True)
    comment = Column(String, nullable=True)
    
    # Context
    custom_metadata = Column(JSON, default={})
    ip_address = Column(INET, nullable=True)
    user_agent = Column(String, nullable=True)
    
    # NEW: Workflow context
    workflow_step = Column(String(100), nullable=True)  # e.g., "pending_approval", "approved"
    workflow_type = Column(String(50), nullable=True)   # e.g., "invoice", "purchase_order"
    
    # NEW: File/Document linkage
    file_id = Column(UUID(as_uuid=True), ForeignKey("files.id"), nullable=True)
    document_hash = Column(String(64), nullable=True)  # SHA-256 from files table
    file_path = Column(String(500), nullable=True)     # Cached path for historical reference
    
    # NEW: AI assistance tracking
    ai_assisted = Column(Boolean, default=False)
    ai_provider = Column(String(50), nullable=True)    # "openai", "claude", etc.
    ai_confidence = Column(Integer, nullable=True)     # 0-100
    
    # Chain identity — links this event to the whole transaction story.
    # Deliberately NOT part of the integrity hash (see DR-011).
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # Tamper-evidence: each entry is hash-chained to the previous entry for the
    # same object. entry_hash = SHA-256(prev_hash + canonical(this row)); the
    # first event for an object has prev_hash = NULL. Altering, deleting, or
    # reordering an object's history breaks the chain (see audit_integrity).
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True)

    # Timestamp (override to use timestamp instead of created_at). UTC, like
    # every other timestamp the application writes — see UTC_NOW in models.base
    # for why a bare now() is wrong here.
    timestamp = Column(DateTime, server_default=UTC_NOW, nullable=False)
    
    # Relationships
    tenant = relationship("Tenant", backref="audit_logs")
    user = relationship("User", backref="audit_logs")
    file = relationship("File", backref="audit_logs")
