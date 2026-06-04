"""Integration tests for Restricted Autopilot (autopilot_service.py).

Verifies it is opt-in (off by default), only auto-approves invoices within the
configured safe bounds, previews without changes, attributes/logs each approval,
is reversible, and is permission-gated.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus, InvoiceState
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.audit_log import AuditLog
from app.services.autopilot_service import AutopilotService, ACTION_AUTO_APPROVED
from app.schemas.autopilot import AutopilotConfig

pytestmark = pytest.mark.integration


def _vendor(db, tenant_id, status=VendorStatus.ACTIVE):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id,
               legal_name=f"V-{uuid.uuid4().hex[:6]}", status=status)
    db.add(v)
    db.flush()
    return v


def _pending(db, tenant_id, created_by, amount, vendor, *, potential_dup=None, dup_ack=False):
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name,
        vendor_id=vendor.id,
        invoice_date=date(2026, 1, 1),
        total_amount=amount,
        current_state=InvoiceState.PENDING_APPROVAL.value,
        created_by=created_by,
        potential_duplicate_id=potential_dup,
        duplicate_acknowledged=dup_ack,
    )
    db.add(inv)
    db.flush()
    return inv


def _enable(db, admin, limit=100_000):
    return AutopilotService(db).set_config(
        AutopilotConfig(enabled=True, max_auto_approve_amount=limit), admin
    )


class TestAutopilotConfig:
    def test_disabled_by_default(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        cfg = AutopilotService(db).get_config(admin)
        assert cfg.enabled is False

    def test_enable_persists(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        _enable(db, admin, limit=75_000)
        cfg = AutopilotService(db).get_config(admin)
        assert cfg.enabled is True
        assert cfg.max_auto_approve_amount == 75_000

    def test_run_disabled_raises(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            AutopilotService(db).run(admin)


class TestAutopilotRun:
    def test_only_eligible_are_approved(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _enable(db, admin, limit=100_000)

        active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
        unverified = _vendor(db, tenant.id, VendorStatus.PENDING_VERIFICATION)

        eligible = _pending(db, tenant.id, admin["id"], 50_000, active)
        over_limit = _pending(db, tenant.id, admin["id"], 200_000, active)
        bad_vendor = _pending(db, tenant.id, admin["id"], 50_000, unverified)
        original = _pending(db, tenant.id, admin["id"], 40_000, active)
        flagged = _pending(db, tenant.id, admin["id"], 40_000, active, potential_dup=original.id)
        # original is itself eligible; flagged is not.

        result = AutopilotService(db).run(admin)

        approved_ids = {c["invoice_id"] for c in result["approved"]}
        assert eligible.id in approved_ids
        assert original.id in approved_ids
        assert over_limit.id not in approved_ids
        assert bad_vendor.id not in approved_ids
        assert flagged.id not in approved_ids

        db.refresh(eligible)
        db.refresh(over_limit)
        assert eligible.current_state == InvoiceState.APPROVED.value
        assert eligible.approved_by is not None
        assert over_limit.current_state == InvoiceState.PENDING_APPROVAL.value

        # Each approval is logged as auto_approved.
        logged = db.query(AuditLog).filter(
            AuditLog.object_id == eligible.id, AuditLog.action == ACTION_AUTO_APPROVED
        ).count()
        assert logged == 1

    def test_preview_makes_no_changes(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _enable(db, admin, limit=100_000)
        active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
        inv = _pending(db, tenant.id, admin["id"], 50_000, active)

        preview = AutopilotService(db).preview(admin)
        assert preview["enabled"] is True
        assert preview["eligible_count"] == 1
        assert any(c["invoice_id"] == inv.id and c["eligible"] for c in preview["candidates"])

        db.refresh(inv)
        assert inv.current_state == InvoiceState.PENDING_APPROVAL.value  # unchanged


class TestAutopilotRevert:
    def test_revert_returns_to_pending(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _enable(db, admin, limit=100_000)
        active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
        inv = _pending(db, tenant.id, admin["id"], 50_000, active)

        AutopilotService(db).run(admin)
        db.refresh(inv)
        assert inv.current_state == InvoiceState.APPROVED.value

        reverted = AutopilotService(db).revert_auto_approval(inv.id, admin)
        assert reverted.current_state == InvoiceState.PENDING_APPROVAL.value
        assert reverted.approved_by is None

    def test_revert_non_auto_approved_rejected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
        # Manually approved (no auto_approved audit) -> cannot be reverted via autopilot.
        inv = _pending(db, tenant.id, admin["id"], 50_000, active)
        inv.current_state = InvoiceState.APPROVED.value
        db.flush()
        with pytest.raises(ValueError):
            AutopilotService(db).revert_auto_approval(inv.id, admin)


class TestAutopilotPermissions:
    def test_clerk_cannot_run_or_configure(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        svc = AutopilotService(db)
        with pytest.raises(PermissionError):
            svc.preview(clerk)
        with pytest.raises(PermissionError):
            svc.get_config(clerk)
        with pytest.raises(PermissionError):
            svc.set_config(AutopilotConfig(enabled=True), clerk)
