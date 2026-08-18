from sqlalchemy import Column, DateTime, String, func
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


class SoftDeleteMixin:
    """Records that are withdrawn rather than destroyed.

    Build Book non-negotiable: *immutable audit — guardrails to prevent hard
    deletes*. A deleted invoice or vendor took its row with it while the audit
    entry describing the deletion stayed behind, pointing at an id that no
    longer resolved. The trail said something happened to something that,
    as far as the database was concerned, had never existed.

    Carrying the mixin does two things automatically: rows with `deleted_at`
    set drop out of every ORM query (see `_exclude_soft_deleted` in
    core.database), and `session.delete()` on the model is refused outright.
    """
    deleted_at = Column(DateTime, nullable=True, index=True)
    deleted_by = Column(UUID(as_uuid=True), nullable=True)
    #: Why it was withdrawn. Required by the services, because "who and when"
    #: without "why" is the half of a deletion nobody can act on later.
    deletion_reason = Column(String, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class BaseModel(Base, UUIDMixin, TimestampMixin):
    """
    Combined base model with UUID primary key + auto timestamps.
    All tenant-scoped entities inherit from this.
    """
    __abstract__ = True
