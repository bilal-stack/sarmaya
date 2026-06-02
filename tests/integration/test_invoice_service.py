"""Integration tests for InvoiceService approval/rejection/payment governance.

Requires a live test Postgres (see conftest). Exercises the real service +
repository + workflow fallback + policy fallback against the database.

NOTE: two tests below (marked BUG) encode the *intended* governance behavior
for reject and mark-paid. They FAIL against the current code because those
service methods skip the permission check, and they should PASS once the bug
fixes land.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationService
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.invoice import InvoiceCreate
from app.core.enums import InvoiceState, UserRole, VendorStatus

pytestmark = pytest.mark.integration


def _make_vendor(db, tenant_id, legal_name, status=VendorStatus.ACTIVE):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, legal_name=legal_name, status=status)
    db.add(v)
    db.flush()
    return v


def _invoice_payload(**overrides) -> InvoiceCreate:
    data = {
        "invoice_number": f"INV-{uuid.uuid4().hex[:8]}",
        "vendor_name": "Acme Ltd",
        "invoice_date": date.today(),
        "total_amount": Decimal("1000"),
    }
    data.update(overrides)
    return InvoiceCreate(**data)


def _make_invoice(db, tenant_id, created_by, state, amount, vendor=None,
                  vendor_status=VendorStatus.ACTIVE):
    """Create an invoice linked to a vendor master record.

    Defaults to an ACTIVE vendor so the approval/payment governance gate is
    satisfied; pass ``vendor_status`` (or an explicit ``vendor``) to exercise
    the gate against PENDING_VERIFICATION / BLOCKED vendors.
    """
    if vendor is None:
        vendor = _make_vendor(
            db, tenant_id, f"Vendor-{uuid.uuid4().hex[:6]}", status=vendor_status
        )
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor.id,
        vendor_name=vendor.legal_name,
        invoice_date=date.today(),
        total_amount=Decimal(str(amount)),
        current_state=state,
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


class TestApprove:
    def test_manager_approves_within_limit(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        result = InvoiceService(db).approve_invoice(inv.id, manager)

        assert result.current_state == InvoiceState.APPROVED.value
        assert str(result.approved_by) == manager["id"]
        assert result.approved_at is not None

    def test_manager_cannot_approve_over_limit(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 300_000)

        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, manager)

    def test_cfo_approves_large_invoice(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 300_000)

        result = InvoiceService(db).approve_invoice(inv.id, cfo)
        assert result.current_state == InvoiceState.APPROVED.value

    def test_cannot_approve_draft(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.DRAFT.value, 100_000)

        with pytest.raises(ValueError):
            InvoiceService(db).approve_invoice(inv.id, manager)

    def test_double_approve_blocked(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.APPROVED.value, 100_000)

        with pytest.raises(ValueError):
            InvoiceService(db).approve_invoice(inv.id, manager)


class TestDuplicateOverride:
    def _flagged_invoice(self, db, tenant, clerk, amount=100_000):
        """A pending-approval invoice flagged as a potential duplicate of an
        earlier one sharing the same (active) vendor."""
        vendor = _make_vendor(db, tenant.id, f"Vendor-{uuid.uuid4().hex[:6]}")
        original = _make_invoice(
            db, tenant.id, clerk["id"], InvoiceState.APPROVED.value, amount, vendor=vendor
        )
        flagged = _make_invoice(
            db, tenant.id, clerk["id"], InvoiceState.PENDING_APPROVAL.value, amount,
            vendor=vendor,
        )
        flagged.potential_duplicate_id = original.id
        db.add(flagged)
        db.flush()
        return flagged, original

    def test_flagged_duplicate_blocks_approval(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        flagged, _ = self._flagged_invoice(db, tenant, clerk)

        with pytest.raises(ValueError, match="potential duplicate"):
            InvoiceService(db).approve_invoice(flagged.id, manager)

    def test_resolve_unblocks_approval(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        svc = InvoiceService(db)
        flagged, _ = self._flagged_invoice(db, tenant, clerk)

        resolved = svc.resolve_duplicate(flagged.id, "Different PO, not a dup", manager)
        assert resolved.duplicate_acknowledged is True

        approved = svc.approve_invoice(flagged.id, manager)
        assert approved.current_state == InvoiceState.APPROVED.value

    def test_resolve_requires_reason(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        flagged, _ = self._flagged_invoice(db, tenant, clerk)

        with pytest.raises(ValueError, match="reason"):
            InvoiceService(db).resolve_duplicate(flagged.id, "   ", manager)

    def test_resolve_requires_approve_permission(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        flagged, _ = self._flagged_invoice(db, tenant, clerk)

        # AP_CLERK can create but not approve, so cannot override duplicates.
        with pytest.raises(PermissionError):
            InvoiceService(db).resolve_duplicate(flagged.id, "looks fine", clerk)

    def test_resolve_without_flag_raises(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(
            db, tenant.id, clerk["id"], InvoiceState.PENDING_APPROVAL.value, 100_000
        )

        with pytest.raises(ValueError, match="no potential duplicate"):
            InvoiceService(db).resolve_duplicate(inv.id, "n/a", manager)

    def test_resolve_logs_audit_with_reason(self, db, tenant, make_user):
        from app.models.audit_log import AuditLog

        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        flagged, _ = self._flagged_invoice(db, tenant, clerk)

        InvoiceService(db).resolve_duplicate(flagged.id, "Verified distinct invoice", manager)

        log = (
            db.query(AuditLog)
            .filter(
                AuditLog.object_id == flagged.id,
                AuditLog.action == "duplicate_overridden",
            )
            .first()
        )
        assert log is not None
        assert "Verified distinct invoice" in (log.comment or "")


class TestSubmit:
    def test_submit_sets_pending_and_required_role(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.DRAFT.value, 100_000)

        result, required_role = InvoiceService(db).submit_for_approval(inv.id, clerk)
        assert result.current_state == InvoiceState.PENDING_APPROVAL.value
        assert required_role == "manager"


class TestReject:
    def test_reject_requires_reason(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        with pytest.raises(ValueError):
            InvoiceService(db).reject_invoice(inv.id, "  ", manager)

    def test_manager_can_reject(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        result = InvoiceService(db).reject_invoice(inv.id, "missing PO", manager)
        assert result.current_state == InvoiceState.REJECTED.value
        assert result.rejection_reason == "missing PO"

    def test_ap_clerk_cannot_reject(self, db, tenant, make_user):
        """BUG: reject_invoice currently has no permission check, so an AP
        clerk (no reject permission) can reject. Should raise PermissionError."""
        clerk = make_user(UserRole.AP_CLERK)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        with pytest.raises(PermissionError):
            InvoiceService(db).reject_invoice(inv.id, "no reason", clerk)


class TestMarkPaid:
    def test_cfo_can_mark_paid(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.APPROVED.value, 100_000)

        result = InvoiceService(db).mark_as_paid(inv.id, cfo)
        assert result.current_state == InvoiceState.PAID.value

    def test_cannot_mark_paid_from_pending(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        with pytest.raises(ValueError):
            InvoiceService(db).mark_as_paid(inv.id, cfo)

    def test_ap_clerk_cannot_mark_paid(self, db, tenant, make_user):
        """BUG: mark_as_paid currently has no permission check, so an AP clerk
        can mark an approved invoice paid. Should raise PermissionError."""
        clerk = make_user(UserRole.AP_CLERK)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.APPROVED.value, 100_000)

        with pytest.raises(PermissionError):
            InvoiceService(db).mark_as_paid(inv.id, clerk)


class TestDashboardSummary:
    def test_counts_pending_and_month_and_top_vendors(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        acme = _make_vendor(db, tenant.id, "Acme Ltd")
        globex = _make_vendor(db, tenant.id, "Globex Inc")

        # 2 pending, 1 draft. Acme out-spends Globex.
        _make_invoice(db, tenant.id, clerk["id"],
                      InvoiceState.PENDING_APPROVAL.value, 5000, vendor=acme)
        _make_invoice(db, tenant.id, clerk["id"],
                      InvoiceState.PENDING_APPROVAL.value, 3000, vendor=acme)
        _make_invoice(db, tenant.id, clerk["id"],
                      InvoiceState.DRAFT.value, 1000, vendor=globex)

        summary = InvoiceService(db).get_dashboard_summary()

        assert summary["pending_approvals"] == 2
        assert summary["invoices_this_month"]["count"] == 3
        assert summary["invoices_this_month"]["total_amount"] == 9000.0

        top = summary["top_vendors"]
        assert top[0]["vendor_name"] == "Acme Ltd"
        assert top[0]["total_amount"] == 8000.0

    def test_empty_tenant_returns_zeroes(self, db, tenant, make_user):
        make_user(UserRole.AP_CLERK)
        summary = InvoiceService(db).get_dashboard_summary()

        assert summary["pending_approvals"] == 0
        assert summary["invoices_this_month"]["count"] == 0
        assert summary["top_vendors"] == []


class TestNotifications:
    """Workflow events fire emails to the right people, and delivery failures
    never break the workflow. SMTP is patched out at the _deliver boundary."""

    @staticmethod
    def _capture(monkeypatch):
        sent = []
        monkeypatch.setattr(
            NotificationService,
            "_deliver",
            lambda self, to, subject, body: sent.append((to, subject, body)),
        )
        return sent

    def test_submit_notifies_approvers(self, db, tenant, make_user, monkeypatch):
        sent = self._capture(monkeypatch)
        clerk = make_user(UserRole.AP_CLERK)
        make_user(UserRole.MANAGER, email="mgr@test.com")
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.DRAFT.value, 100_000)

        InvoiceService(db).submit_for_approval(inv.id, clerk)

        recipients = [to for to, _, _ in sent]
        assert "mgr@test.com" in recipients  # manager approves <=250k

    def test_approve_notifies_creator(self, db, tenant, make_user, monkeypatch):
        sent = self._capture(monkeypatch)
        clerk = make_user(UserRole.AP_CLERK, email="clerk@test.com")
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        InvoiceService(db).approve_invoice(inv.id, manager)

        assert "clerk@test.com" in [to for to, _, _ in sent]
        assert any("approved" in subj.lower() for _, subj, _ in sent)

    def test_reject_notifies_creator_with_reason(self, db, tenant, make_user, monkeypatch):
        sent = self._capture(monkeypatch)
        clerk = make_user(UserRole.AP_CLERK, email="clerk@test.com")
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        InvoiceService(db).reject_invoice(inv.id, "missing PO", manager)

        assert "clerk@test.com" in [to for to, _, _ in sent]
        assert any("missing PO" in body for _, _, body in sent)

    def test_delivery_failure_does_not_break_approval(self, db, tenant, make_user, monkeypatch):
        def boom(self, to, subject, body):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(NotificationService, "_deliver", boom)
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000)

        # A dead SMTP server must not stop the invoice from being approved.
        result = InvoiceService(db).approve_invoice(inv.id, manager)
        assert result.current_state == InvoiceState.APPROVED.value


class TestVendorGovernanceGate:
    """Approval & payment are blocked until the linked vendor is ACTIVE.

    A vendor auto-created from an upload starts PENDING_VERIFICATION; a human
    with vendors.manage must verify/activate it before money can move. BLOCKED
    vendors are never payable.
    """

    def test_cannot_approve_pending_verification_vendor(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000,
                            vendor_status=VendorStatus.PENDING_VERIFICATION)

        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, manager)

    def test_cannot_approve_blocked_vendor(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000,
                            vendor_status=VendorStatus.BLOCKED)

        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, manager)

    def test_cannot_mark_paid_pending_verification_vendor(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.APPROVED.value, 100_000,
                            vendor_status=VendorStatus.PENDING_VERIFICATION)

        with pytest.raises(PermissionError):
            InvoiceService(db).mark_as_paid(inv.id, cfo)

    def test_approve_succeeds_after_vendor_activated(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        vendor = _make_vendor(db, tenant.id, "Pending Co",
                              status=VendorStatus.PENDING_VERIFICATION)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000,
                            vendor=vendor)

        # Blocked while pending...
        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, manager)

        # ...verify/activate the vendor, then approval goes through.
        vendor.status = VendorStatus.ACTIVE
        db.flush()

        result = InvoiceService(db).approve_invoice(inv.id, manager)
        assert result.current_state == InvoiceState.APPROVED.value

    def test_blocked_approval_is_audited(self, db, tenant, make_user):
        from app.models.audit_log import AuditLog

        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 100_000,
                            vendor_status=VendorStatus.PENDING_VERIFICATION)

        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, manager)

        # The denial is persisted (committed) as its own audit record even
        # though the approval was rejected and the txn otherwise rolled back.
        record = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == inv.id, AuditLog.action == "approval_blocked")
            .first()
        )
        assert record is not None
        assert record.after_value["reason"] == "vendor_pending_verification"
        assert str(record.user_id) == manager["id"]


class TestBlockedOnVendorWorklist:
    def test_lists_only_pending_invoices_with_inactive_vendor(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        svc = InvoiceService(db)

        # Stuck: pending approval + pending-verification vendor.
        stuck = _make_invoice(db, tenant.id, clerk["id"],
                              InvoiceState.PENDING_APPROVAL.value, 50_000,
                              vendor_status=VendorStatus.PENDING_VERIFICATION)
        # Not stuck: pending approval but vendor is active.
        _make_invoice(db, tenant.id, clerk["id"],
                      InvoiceState.PENDING_APPROVAL.value, 50_000,
                      vendor_status=VendorStatus.ACTIVE)
        # Not in scope: draft, even with an inactive vendor.
        _make_invoice(db, tenant.id, clerk["id"],
                      InvoiceState.DRAFT.value, 50_000,
                      vendor_status=VendorStatus.PENDING_VERIFICATION)

        blocked = svc.get_invoices_blocked_on_vendor()
        ids = {i.id for i in blocked}
        assert stuck.id in ids
        assert len(ids) == 1

    def test_invoice_drops_off_worklist_after_vendor_activated(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _make_vendor(db, tenant.id, "Pending Supplier",
                              status=VendorStatus.PENDING_VERIFICATION)
        inv = _make_invoice(db, tenant.id, clerk["id"],
                            InvoiceState.PENDING_APPROVAL.value, 50_000, vendor=vendor)
        svc = InvoiceService(db)

        assert inv.id in {i.id for i in svc.get_invoices_blocked_on_vendor()}

        vendor.status = VendorStatus.ACTIVE
        db.flush()

        assert inv.id not in {i.id for i in svc.get_invoices_blocked_on_vendor()}


class TestManualCreateVendorLinking:
    def test_links_explicit_vendor_id_and_uses_canonical_name(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Globex Inc")

        invoice = InvoiceService(db).create_manual_invoice(
            _invoice_payload(vendor_id=vendor.id, vendor_name="typed wrong"), admin
        )
        assert invoice.vendor_id == vendor.id
        # The vendor's legal_name overrides whatever name was typed.
        assert invoice.vendor_name == "Globex Inc"

    def test_unknown_vendor_id_raises(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            InvoiceService(db).create_manual_invoice(
                _invoice_payload(vendor_id=uuid.uuid4()), admin
            )

    def test_matches_existing_vendor_by_name(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Initech LLC")

        invoice = InvoiceService(db).create_manual_invoice(
            _invoice_payload(vendor_name="initech llc"), admin
        )
        assert invoice.vendor_id == vendor.id

    def test_blocked_vendor_rejected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Sanctioned Co", status=VendorStatus.BLOCKED)

        with pytest.raises(ValueError):
            InvoiceService(db).create_manual_invoice(
                _invoice_payload(vendor_id=vendor.id), admin
            )

    def test_unmatched_name_auto_creates_pending_vendor(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        invoice = InvoiceService(db).create_manual_invoice(
            _invoice_payload(vendor_name="Never Seen Vendor"), admin
        )
        # Manual create now auto-creates a vendor master record so vendor_id is
        # always set; the new vendor starts PENDING_VERIFICATION.
        assert invoice.vendor_id is not None
        assert invoice.vendor_name == "Never Seen Vendor"
        vendor = InvoiceService(db).vendor_repository.get_by_id(invoice.vendor_id)
        assert vendor.status == VendorStatus.PENDING_VERIFICATION


class TestUploadVendorAutoCreate:
    """Covers the _resolve_vendor auto-create path used by the upload flow,
    without exercising OCR."""

    def test_auto_creates_pending_vendor_for_any_uploader(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)

        vendor_id, name, status = InvoiceService(db)._resolve_vendor(
            clerk, vendor_name="Brand New Supplier", auto_create=True
        )
        assert vendor_id is not None
        assert name == "Brand New Supplier"
        assert status == VendorStatus.PENDING_VERIFICATION.value

    def test_blank_name_does_not_auto_create(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)

        vendor_id, name, status = InvoiceService(db)._resolve_vendor(
            clerk, vendor_name="   ", auto_create=True
        )
        # No name to anchor a master record on, so nothing is created.
        assert vendor_id is None
        assert status is None

    def test_links_existing_vendor_instead_of_creating(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _make_vendor(db, tenant.id, "Existing Supplier")

        vendor_id, name, status = InvoiceService(db)._resolve_vendor(
            clerk, vendor_name="existing supplier", auto_create=True
        )
        assert vendor_id == vendor.id
        assert status == VendorStatus.ACTIVE.value


class TestVendorIdDeduplication:
    def test_exact_duplicate_caught_across_name_spelling(self, db, tenant, make_user):
        """The same invoice number for the same vendor is caught even when the
        second entry types the vendor name differently — the name-based check
        (exact string ==) would have missed 'ACME LTD' vs 'Acme Ltd'."""
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Acme Ltd")
        svc = InvoiceService(db)

        first = svc.create_manual_invoice(
            _invoice_payload(invoice_number="INV-100", vendor_id=vendor.id), admin
        )
        assert first.vendor_id == vendor.id
        assert first.vendor_name == "Acme Ltd"

        # Second entry: no vendor_id, different casing — resolves to the same
        # vendor by case-insensitive name match, so dedup by vendor_id fires.
        with pytest.raises(ValueError):
            svc.create_manual_invoice(
                _invoice_payload(invoice_number="INV-100", vendor_name="ACME LTD"), admin
            )

    def test_different_number_same_vendor_is_allowed(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Initech LLC")
        svc = InvoiceService(db)

        svc.create_manual_invoice(
            _invoice_payload(invoice_number="A-1", vendor_id=vendor.id), admin
        )
        # A genuinely different invoice number for the same vendor is fine.
        second = svc.create_manual_invoice(
            _invoice_payload(invoice_number="A-2", vendor_id=vendor.id), admin
        )
        assert second.invoice_number == "A-2"

    def test_find_similar_by_vendor_id_ignores_name_variation(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _make_vendor(db, tenant.id, "Globex Inc")
        repo = InvoiceService(db).repository

        seeded = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number="G-1",
            vendor_id=vendor.id, vendor_name="Globex Inc",
            invoice_date=date.today(), total_amount=Decimal("1000"),
            current_state=InvoiceState.DRAFT.value, created_by=admin["id"],
        )
        db.add(seeded)
        db.flush()

        # Same vendor_id, near amount/date — found regardless of the name string.
        found = repo.find_similar_by_vendor_id(
            vendor_id=vendor.id, invoice_date=date.today(), total_amount=1000.0
        )
        assert found is not None
        assert found.id == seeded.id
