"""Cryptographic integrity for the audit trail.

Each audit entry is hash-chained to the previous entry for the *same object*
(tenant + object_type + object_id):

    entry_hash = SHA-256( prev_hash + "\\n" + canonical(entry) )

where canonical(entry) is a deterministic JSON of the entry's substantive
fields. The first event for an object is the genesis (prev_hash = None).

This makes the per-object Live Audit timeline tamper-evident: altering a stored
field, deleting an event, inserting one, or reordering events all break the
chain and are reported by `verify_object_chain`.

Determinism note: the hash must match whether it is computed at write time
(Python-native values: str-enums, tz-aware datetimes) or at verify time
(values reloaded from Postgres). `_normalize` collapses both sides to the same
canonical form — enums to their value, datetimes to naive-UTC ISO, UUIDs to str.
"""
from __future__ import annotations

import enum
import hashlib
import json
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.utils.datetime_helpers import to_utc

# Substantive, immutable columns included in the integrity hash. Volatile or
# derived columns (custom_metadata, ip_address, user_agent, file_path, the hash
# columns themselves) are intentionally excluded.
HASHED_FIELDS = (
    "id",
    "tenant_id",
    "user_id",
    "user_email",
    "user_role",
    "object_type",
    "object_id",
    "action",
    "before_value",
    "after_value",
    "changes",
    "comment",
    "workflow_step",
    "workflow_type",
    "document_hash",
    "ai_assisted",
    "ai_provider",
    "ai_confidence",
    "timestamp",
)


def _normalize(value):
    """Collapse a value to a representation that is identical whether it came
    from a Python-native write or a Postgres reload."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        # The timestamp column is tz-naive in Postgres; normalize both the
        # tz-aware (write-time) and tz-naive (read-time) forms to naive UTC.
        return to_utc(value).replace(tzinfo=None).isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _canonical(entry) -> str:
    payload = {field: _normalize(getattr(entry, field, None)) for field in HASHED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_entry_hash(entry, prev_hash: Optional[str]) -> str:
    """Hash of `entry` chained onto `prev_hash`. `entry` may be an AuditLog ORM
    row or any object exposing the HASHED_FIELDS as attributes."""
    base = f"{prev_hash or ''}\n{_canonical(entry)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def append_to_chain(db: Session, audit: AuditLog) -> None:
    """Set prev_hash/entry_hash on a not-yet-persisted audit row, linking it to
    the latest existing entry for the same object. Call before db.add/flush.

    The caller must already hold the tenant RLS context; the previous-entry
    lookup is tenant-scoped both by RLS and explicitly here.
    """
    prev = (
        db.query(AuditLog)
        .filter(
            AuditLog.tenant_id == audit.tenant_id,
            AuditLog.object_type == audit.object_type,
            AuditLog.object_id == audit.object_id,
            AuditLog.id != audit.id,
        )
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .first()
    )
    prev_hash = prev.entry_hash if prev else None
    audit.prev_hash = prev_hash
    audit.entry_hash = compute_entry_hash(audit, prev_hash)


def verify_object_chain(db: Session, object_type: str, object_id: UUID) -> dict:
    """Recompute an object's hash chain and report the first break, if any.

    Returns a dict with: object_type, object_id, total_events, verified,
    broken_at_index (0-based index of the first bad event, or None), and a
    human-readable detail when broken.
    """
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.object_type == object_type, AuditLog.object_id == object_id)
        .order_by(AuditLog.timestamp.asc(), AuditLog.id.asc())
        .all()
    )

    prev_hash: Optional[str] = None
    broken_at: Optional[int] = None
    detail: Optional[str] = None

    for index, log in enumerate(logs):
        if log.entry_hash is None:
            broken_at, detail = index, (
                "Entry has no integrity hash — it predates hash chaining or was "
                "inserted outside the application."
            )
            break
        if log.prev_hash != prev_hash:
            broken_at, detail = index, (
                "Previous-hash link does not match the prior entry — an event may "
                "have been inserted, deleted, or reordered."
            )
            break
        if log.entry_hash != compute_entry_hash(log, prev_hash):
            broken_at, detail = index, (
                "Entry hash does not match its content — the event was altered "
                "after it was recorded."
            )
            break
        prev_hash = log.entry_hash

    return {
        "object_type": object_type,
        "object_id": object_id,
        "total_events": len(logs),
        "verified": broken_at is None,
        "broken_at_index": broken_at,
        "detail": detail,
    }
