"""Withdrawing a record instead of destroying it.

One helper rather than three near-identical blocks, so every withdrawal records
the same three facts — who, when, and why — and so the next module that needs
one inherits the reason requirement rather than deciding about it again.

A withdrawn row keeps its id resolvable, which is the point: the audit entry
describing the deletion refers to something that still exists. It drops out of
every ORM query automatically (see `_exclude_soft_deleted`), so callers do not
filter for it and cannot forget to.
"""
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.audit import log_audit
from app.utils.datetime_helpers import utc_now, to_utc, make_naive


def withdraw(
    db: Session,
    record,
    current_user: dict,
    reason: str,
    *,
    object_type: str,
    before_value: Optional[dict] = None,
) -> None:
    """Mark a record withdrawn and log it. Raises if no reason is given.

    The reason is mandatory rather than optional because a deletion is the one
    event nobody can reconstruct from what is left: every other action leaves
    the changed record behind to be read, and this one leaves an absence.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "A reason is required to withdraw a record. It is the only "
            "explanation anyone reading the trail later will have."
        )

    record.deleted_at = make_naive(to_utc(utc_now()))
    record.deleted_by = current_user["id"]
    record.deletion_reason = reason.strip()
    db.add(record)
    db.flush()

    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type=object_type,
        object_id=record.id,
        action="deleted",
        comment=reason.strip(),
        before_value=before_value or {},
        after_value={"deleted_at": str(record.deleted_at)},
    )


def restore(db: Session, record, current_user: dict, object_type: str) -> None:
    """Undo a withdrawal.

    Possible only because the row was kept. Under the old hard delete the
    equivalent operation was re-typing the record and hoping it matched.
    """
    record.deleted_at = None
    record.deleted_by = None
    record.deletion_reason = None
    db.add(record)
    db.flush()

    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type=object_type,
        object_id=record.id,
        action="restored",
    )
