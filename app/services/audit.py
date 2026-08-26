import uuid
from sqlalchemy.orm import Session
from typing import Optional, Dict, Any
from uuid import UUID

from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.audit_integrity import append_to_chain
from app.services.correlation import resolve_correlation_id
from app.utils.datetime_helpers import utc_now


def log_audit(
    db: Session,
    tenant_id: str | UUID,
    user_id: str | UUID,
    object_type: str,
    object_id: UUID,
    action: str,
    before_value: Optional[Dict[str, Any]] = None,
    after_value: Optional[Dict[str, Any]] = None,
    comment: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    workflow_step: Optional[str] = None,
    workflow_type: Optional[str] = None,
    file_id: Optional[UUID] = None,
    document_hash: Optional[str] = None,
    file_path: Optional[str] = None,
    ai_assisted: bool = False,
    ai_provider: Optional[str] = None,
    ai_confidence: Optional[int] = None,
    correlation_id=None,
):
    """
    Enhanced audit logging with full compliance tracking

    New fields:
    - workflow_step: Current workflow state (e.g., "pending_approval")
    - workflow_type: Type of workflow (e.g., "invoice")
    - file_id: Link to files table
    - document_hash: SHA-256 hash from files
    - file_path: Cached file path for historical record
    - ai_assisted: Whether AI was used in this action
    - ai_provider: Which AI provider ("openai", "claude", etc.)
    - ai_confidence: AI confidence score (0-100)
    """
    # Chain identity: use the caller's if given, else inherit the subject
    # object's, so no call site can silently drop an event out of its story.
    if correlation_id is None:
        correlation_id = resolve_correlation_id(db, object_type, object_id)

    # Get user email and role if user exists.
    #
    # Note for anyone optimising this: `Session.get` looks like the obvious
    # improvement, since this runs in loops and re-reads the same acting user
    # once per audit entry. It does not help here. `_exclude_soft_deleted` in
    # app/core/database.py attaches `with_loader_criteria` to every SELECT, and
    # SQLAlchemy disables get()'s identity-map shortcut whenever loader
    # criteria may apply — the criteria could exclude a row that is in the map.
    # Measured: warm `db.get` calls still emit one SELECT each.
    user = db.query(User).filter(User.id == user_id).first()
    user_email = user.email if user else None
    user_role = user.role if user else None

    audit = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
        object_type=object_type,
        object_id=object_id,
        action=action,
        before_value=before_value,
        after_value=after_value,
        comment=comment,
        custom_metadata={},
        ip_address=ip_address,
        user_agent=user_agent,
        timestamp=utc_now(),
        workflow_step=workflow_step,
        workflow_type=workflow_type,
        file_id=file_id,
        document_hash=document_hash,
        file_path=file_path,
        ai_assisted=ai_assisted,
        ai_provider=ai_provider,
        ai_confidence=ai_confidence,
        correlation_id=correlation_id,
    )
    db.add(audit)
    # Persist first, then reload, so the integrity hash is computed over the
    # exact values Postgres stores (e.g. enums and tz-naive timestamps), which
    # is what verification later re-reads. Hashing the in-memory Python values
    # instead would not match after a round-trip.
    db.flush()
    db.refresh(audit)
    # Tamper-evident chaining: link this entry to the object's latest entry.
    append_to_chain(db, audit)
    db.flush()

