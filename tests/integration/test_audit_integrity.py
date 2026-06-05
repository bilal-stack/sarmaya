"""Integration tests for the tamper-evident audit hash chain
(app/services/audit_integrity.py).

Each audit entry is hash-chained to the previous entry for the same object, so
altering, deleting, or reordering an object's history breaks the chain and is
reported by verify_object_chain.
"""
import time
import uuid

import pytest

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.services.audit import log_audit
from app.services.audit_service import AuditService
from app.services.audit_integrity import verify_object_chain

pytestmark = pytest.mark.integration


def _ordered(db, object_id):
    return (
        db.query(AuditLog)
        .filter(AuditLog.object_id == object_id)
        .order_by(AuditLog.timestamp.asc(), AuditLog.id.asc())
        .all()
    )


def _chain(db, tenant_id, user_id, object_id, n=3):
    """Write n chained audit events for one object via the real log_audit path."""
    for i in range(n):
        log_audit(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            object_type="invoice",
            object_id=object_id,
            action=f"step_{i}",
            after_value={"i": i},
        )
        # Keep timestamps strictly increasing so chain order is deterministic.
        time.sleep(0.002)


class TestHashChain:
    def test_valid_chain_verifies(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        obj = uuid.uuid4()
        _chain(db, tenant.id, admin["id"], obj, n=3)

        result = verify_object_chain(db, "invoice", obj)
        assert result["total_events"] == 3
        assert result["verified"] is True
        assert result["broken_at_index"] is None

    def test_genesis_has_no_prev_hash(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        obj = uuid.uuid4()
        _chain(db, tenant.id, admin["id"], obj, n=1)

        row = _ordered(db, obj)[0]
        assert row.prev_hash is None
        assert row.entry_hash is not None

    def test_each_entry_links_to_the_previous(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        obj = uuid.uuid4()
        _chain(db, tenant.id, admin["id"], obj, n=3)

        rows = _ordered(db, obj)
        assert rows[1].prev_hash == rows[0].entry_hash
        assert rows[2].prev_hash == rows[1].entry_hash

    def test_tampered_content_is_detected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        obj = uuid.uuid4()
        _chain(db, tenant.id, admin["id"], obj, n=3)

        rows = _ordered(db, obj)
        # Alter the middle entry's payload without recomputing its stored hash.
        rows[1].after_value = {"i": 999}
        db.flush()

        result = verify_object_chain(db, "invoice", obj)
        assert result["verified"] is False
        assert result["broken_at_index"] == 1
        assert "altered" in result["detail"].lower()

    def test_deleted_entry_is_detected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        obj = uuid.uuid4()
        _chain(db, tenant.id, admin["id"], obj, n=3)

        rows = _ordered(db, obj)
        db.delete(rows[1])  # remove the middle event
        db.flush()

        result = verify_object_chain(db, "invoice", obj)
        assert result["verified"] is False
        # The event after the deleted one now points at a prev_hash that is no
        # longer present in the chain.
        assert result["broken_at_index"] == 1

    def test_empty_object_chain_is_vacuously_valid(self, db):
        result = verify_object_chain(db, "invoice", uuid.uuid4())
        assert result["total_events"] == 0
        assert result["verified"] is True


class TestVerifyPermissions:
    def test_verify_requires_object_view(self, db, make_user):
        # APPROVER lacks vendors.view, so cannot verify a vendor's chain.
        approver = make_user(UserRole.APPROVER)
        with pytest.raises(PermissionError):
            AuditService(db).verify_chain("vendor", uuid.uuid4(), approver)
