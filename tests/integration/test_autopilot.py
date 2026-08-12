"""Integration tests for Restricted Autopilot (autopilot_service.py).

Verifies it is opt-in (off by default), only auto-approves invoices within the
configured safe bounds, previews without changes, attributes/logs each approval,
is reversible, and is permission-gated.
"""
import uuid
from datetime import date
from decimal import Decimal

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


class TestAutopilotCannotExceedTheRunnersOwnAuthority:
    """The autopilot cap is set by an admin and is unrelated to the approval
    matrix. Without a second check, a manager limited to 250k could click run
    against a 1m cap and approve a 900k invoice — recorded as `approved_by`
    that manager, an authority the matrix explicitly denies them.

    Reproduced against the service before this check existed: the manager's
    manual approval was refused with "can only approve invoices up to 250000",
    and the same manager's autopilot run approved it anyway.
    """

    def _enable(self, db, admin, cap):
        from app.schemas.autopilot import AutopilotConfig

        AutopilotService(db).set_config(
            AutopilotConfig(
                enabled=True, max_auto_approve_amount=cap,
                require_active_vendor=True, require_no_duplicate=True,
            ),
            admin,
        )

    def _pending(self, db, tenant, admin, amount, number):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Bounded Vendor",
            status=VendorStatus.ACTIVE, created_by=admin["id"],
        )
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            vendor_name=vendor.legal_name, invoice_number=number,
            invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
            current_state=InvoiceState.PENDING_APPROVAL, created_by=admin["id"],
        )
        db.add(invoice)
        db.flush()
        return invoice

    def test_a_manager_cannot_auto_approve_beyond_their_limit(
        self, db, tenant, make_user
    ):
        from app.core.roles import can_approve_amount

        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        self._enable(db, admin, cap=1_000_000)
        invoice = self._pending(db, tenant, admin, "900000", "OVER-LIMIT-001")

        # The manual path refuses this; autopilot must agree.
        assert can_approve_amount("manager", 900_000)[0] is False

        result = AutopilotService(db).run(manager)

        assert result["approved_count"] == 0
        db.refresh(invoice)
        state = str(getattr(invoice.current_state, "value", invoice.current_state))
        assert state.lower() == InvoiceState.PENDING_APPROVAL.value
        assert invoice.approved_by is None

    def test_the_preview_says_why(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        self._enable(db, admin, cap=1_000_000)
        self._pending(db, tenant, admin, "900000", "OVER-LIMIT-002")

        preview = AutopilotService(db).preview(manager)
        reasons = " ".join(c["reason"] for c in preview["candidates"])
        assert "250000" in reasons or "up to" in reasons

    def test_within_their_limit_still_runs(self, db, tenant, make_user):
        """The control: the bound must not disable autopilot altogether."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        self._enable(db, admin, cap=1_000_000)
        invoice = self._pending(db, tenant, admin, "1000", "WITHIN-LIMIT-001")

        result = AutopilotService(db).run(manager)

        assert result["approved_count"] == 1
        db.refresh(invoice)
        assert str(
            getattr(invoice.current_state, "value", invoice.current_state)
        ).lower() == InvoiceState.APPROVED.value

    def test_an_unlimited_approver_is_unaffected(self, db, tenant, make_user):
        """A CFO has no ceiling in the matrix, so only the autopilot cap binds."""
        admin = make_user(UserRole.ADMIN)
        cfo = make_user(UserRole.CFO)
        self._enable(db, admin, cap=1_000_000)
        self._pending(db, tenant, admin, "900000", "CFO-OK-001")

        assert AutopilotService(db).run(cfo)["approved_count"] == 1


class TestAutopilotRespectsMakerChecker:
    """Autopilot calls transition_state directly rather than going through
    InvoiceService.approve_invoice, so nothing in it consulted the rule that
    refuses approving what you created.

    Unreachable as the roles stand — only `admin` holds both invoices.create
    and invoices.approve, and DR-005 exempts admins from SoD anyway. It goes
    live the moment another role gains both, which the Build Book's HR and
    procurement administration roles plausibly will, and whoever grants that
    permission will be thinking about who may raise invoices rather than about
    a bulk-approval path that quietly skips maker-checker.

    So the condition is created here deliberately: a manager is given
    invoices.create for the duration of the test, which is exactly the change
    that would make this real.
    """

    @pytest.fixture
    def manager_who_can_also_raise(self, monkeypatch):
        """The permission grant that makes the gap reachable."""
        from app.core.roles import (
            MANAGER, ROLE_PERMISSIONS, PERM_CREATE_INVOICE,
        )

        monkeypatch.setitem(
            ROLE_PERMISSIONS, MANAGER,
            [*ROLE_PERMISSIONS[MANAGER], PERM_CREATE_INVOICE],
        )

    def _enabled(self, db, admin):
        AutopilotService(db).set_config(
            AutopilotConfig(
                enabled=True, max_auto_approve_amount=50_000,
                require_active_vendor=True, require_no_duplicate=True,
            ),
            admin,
        )

    def _pending_raised_by(self, db, tenant, raiser, number):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="SoD Vendor",
            status=VendorStatus.ACTIVE, created_by=raiser["id"],
        )
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            vendor_name=vendor.legal_name, invoice_number=number,
            invoice_date=date(2026, 8, 1), total_amount=Decimal("1000"),
            current_state=InvoiceState.PENDING_APPROVAL,
            created_by=raiser["id"],
        )
        db.add(invoice)
        db.flush()
        return invoice

    def test_it_will_not_approve_an_invoice_the_runner_raised(
        self, db, tenant, make_user, manager_who_can_also_raise
    ):
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        self._enabled(db, admin)
        own = self._pending_raised_by(db, tenant, manager, "OWN-SOD-001")

        result = AutopilotService(db).run(manager)

        assert result["approved_count"] == 0
        db.refresh(own)
        assert str(
            getattr(own.current_state, "value", own.current_state)
        ).lower() == InvoiceState.PENDING_APPROVAL.value
        assert own.approved_by is None

    def test_the_preview_says_why(self, db, tenant, make_user, manager_who_can_also_raise):
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        self._enabled(db, admin)
        self._pending_raised_by(db, tenant, manager, "OWN-SOD-002")

        preview = AutopilotService(db).preview(manager)
        reasons = " ".join(c["reason"] for c in preview["candidates"])
        assert "someone other than whoever created it" in reasons

    def test_someone_elses_invoice_still_runs(
        self, db, tenant, make_user, manager_who_can_also_raise
    ):
        """The control: the bound must not disable autopilot altogether."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        clerk = make_user(UserRole.AP_CLERK)
        self._enabled(db, admin)
        theirs = self._pending_raised_by(db, tenant, clerk, "THEIRS-SOD-001")

        result = AutopilotService(db).run(manager)

        assert result["approved_count"] == 1
        db.refresh(theirs)
        assert str(
            getattr(theirs.current_state, "value", theirs.current_state)
        ).lower() == InvoiceState.APPROVED.value

    def test_an_admin_is_still_exempt(self, db, tenant, make_user):
        """DR-005's carve-out, so a one-person tenant keeps working — and so
        this behaves exactly as the manual approval path does."""
        admin = make_user(UserRole.ADMIN)
        self._enabled(db, admin)
        own = self._pending_raised_by(db, tenant, admin, "ADMIN-SOD-001")

        assert AutopilotService(db).run(admin)["approved_count"] == 1
        db.refresh(own)
        assert str(
            getattr(own.current_state, "value", own.current_state)
        ).lower() == InvoiceState.APPROVED.value
