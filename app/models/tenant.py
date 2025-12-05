from sqlalchemy import Column, String, Boolean, DateTime, JSON
from sqlalchemy.dialects.postgresql import UUID
from app.models.base import BaseModel
import uuid


class Tenant(BaseModel):
    __tablename__ = "tenants"
    
    # Remove hardcoded default; use uuid.uuid4() instead
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    
    # Hybrid isolation support
    isolation_level = Column(String(50), default="rls", nullable=False)
    schema_name = Column(String(100), nullable=True)
    database_name = Column(String(100), nullable=True)
    connection_string = Column(String, nullable=True)
    
    # Configuration
    logo_url = Column(String(500), nullable=True)
    custom_settings = Column(JSON, default={})
    
    # Billing
    subscription_tier = Column(String(50), default="free")
    
    # Status
    is_active = Column(Boolean, default=True)
    trial_ends_at = Column(DateTime, nullable=True)
