from sqlalchemy import Column, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import registry
import uuid

# Use registry instead of declarative_base() (SQLAlchemy 2.0 pattern)
mapper_registry = registry()
Base = mapper_registry.generate_base()


class UUIDMixin:
    """Mixin for UUID primary keys"""
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Mixin for audit timestamps (created_at, updated_at)"""
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class BaseModel(Base, UUIDMixin, TimestampMixin):
    """
    Combined base model with UUID primary key + auto timestamps.
    All tenant-scoped entities inherit from this.
    """
    __abstract__ = True
