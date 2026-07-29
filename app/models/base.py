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


#: Server-side "now" as a *UTC* wall clock.
#:
#: These columns are tz-naive, and plain ``now()`` returns a ``timestamptz``
#: that Postgres converts into the server's local zone on the way in. That
#: silently stored every created_at in local time while all application code
#: (utc_now / make_naive) writes UTC — a skew equal to the server's offset.
#: In a system that orders audit events and prices SLA timers, two clocks in
#: one column is a correctness bug, not a display quirk.
UTC_NOW = func.timezone("UTC", func.now())


class TimestampMixin:
    """Mixin for audit timestamps (created_at, updated_at). Both are UTC."""
    created_at = Column(DateTime, server_default=UTC_NOW, nullable=False)
    updated_at = Column(DateTime, server_default=UTC_NOW, onupdate=UTC_NOW, nullable=False)


class BaseModel(Base, UUIDMixin, TimestampMixin):
    """
    Combined base model with UUID primary key + auto timestamps.
    All tenant-scoped entities inherit from this.
    """
    __abstract__ = True
