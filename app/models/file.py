from sqlalchemy import Column, String, Integer, BigInteger, JSON, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
import uuid


class File(BaseModel):
    __tablename__ = "files"
    
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    
    original_filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    
    mime_type = Column(String(100), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    file_hash = Column(String(64), nullable=True)
    
    # Link to any object
    object_type = Column(String(50), nullable=True)  # invoice, vendor, contract, etc.
    object_id = Column(UUID(as_uuid=True), nullable=True)
    
    # Storage
    storage_type = Column(String(50), default="local")  # local, s3, azure, gcs
    
    # Metadata
    custom_metadata = Column(JSON, default={})
    
    # Audit
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    
    # Relationships
    tenant = relationship("Tenant", backref="files")
    uploader = relationship("User", backref="files_uploaded")
