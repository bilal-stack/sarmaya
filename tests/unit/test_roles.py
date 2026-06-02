"""Unit tests for role permissions and approval limits (app/core/roles.py).

These encode the MVP spec's governance rules:
- AP Clerk: create/edit invoices, no approval.
- Manager: approve <= 250,000 PKR.
- CFO / Admin: approve any amount.
- Auditor: read-only.
Pure functions, no database required.
"""
import pytest

from app.core import roles
from app.core.roles import (
    ADMIN, AP_CLERK, MANAGER, CFO, APPROVER, AUDITOR, SYSTEM,
    has_permission,
    can_approve_amount,
    can_approve_invoices,
    get_approval_limit,
    PERM_CREATE_INVOICE,
    PERM_APPROVE_INVOICE,
    PERM_MARK_PAID_INVOICE,
    PERM_REJECT_INVOICE,
    PERM_VIEW_INVOICE,
)


class TestHasPermission:
    def test_admin_has_every_permission(self):
        for perm in [
            PERM_CREATE_INVOICE, PERM_APPROVE_INVOICE,
            PERM_MARK_PAID_INVOICE, PERM_REJECT_INVOICE, PERM_VIEW_INVOICE,
        ]:
            assert has_permission(ADMIN, perm) is True

    def test_ap_clerk_can_create_but_not_approve(self):
        assert has_permission(AP_CLERK, PERM_CREATE_INVOICE) is True
        assert has_permission(AP_CLERK, PERM_APPROVE_INVOICE) is False
        assert has_permission(AP_CLERK, PERM_MARK_PAID_INVOICE) is False

    def test_manager_can_approve_and_reject_but_not_create(self):
        assert has_permission(MANAGER, PERM_APPROVE_INVOICE) is True
        assert has_permission(MANAGER, PERM_REJECT_INVOICE) is True
        assert has_permission(MANAGER, PERM_CREATE_INVOICE) is False

    def test_cfo_can_mark_paid(self):
        assert has_permission(CFO, PERM_MARK_PAID_INVOICE) is True

    def test_manager_cannot_mark_paid(self):
        assert has_permission(MANAGER, PERM_MARK_PAID_INVOICE) is False

    def test_auditor_is_read_only(self):
        assert has_permission(AUDITOR, PERM_VIEW_INVOICE) is True
        assert has_permission(AUDITOR, PERM_APPROVE_INVOICE) is False
        assert has_permission(AUDITOR, PERM_CREATE_INVOICE) is False

    def test_unknown_role_has_no_permission(self):
        assert has_permission("nonexistent_role", PERM_VIEW_INVOICE) is False


class TestApprovalLimit:
    def test_admin_unlimited(self):
        assert get_approval_limit(ADMIN) is None

    def test_cfo_unlimited(self):
        assert get_approval_limit(CFO) is None

    def test_manager_capped_at_250k(self):
        assert get_approval_limit(MANAGER) == 250_000

    def test_role_without_approval_returns_zero(self):
        # AP clerk has no entry in APPROVAL_LIMITS -> 0 (no approval authority)
        assert get_approval_limit(AP_CLERK) == 0


class TestCanApproveAmount:
    def test_manager_within_limit(self):
        ok, msg = can_approve_amount(MANAGER, 250_000)
        assert ok is True
        assert msg == ""

    def test_manager_over_limit_blocked(self):
        ok, msg = can_approve_amount(MANAGER, 250_000.01)
        assert ok is False
        assert "250000" in msg.replace(",", "")

    def test_manager_just_under_limit(self):
        ok, _ = can_approve_amount(MANAGER, 249_999)
        assert ok is True

    def test_cfo_can_approve_large_amount(self):
        ok, msg = can_approve_amount(CFO, 10_000_000)
        assert ok is True
        assert msg == ""

    def test_admin_can_approve_any_amount(self):
        ok, _ = can_approve_amount(ADMIN, 999_999_999)
        assert ok is True

    def test_ap_clerk_cannot_approve(self):
        ok, msg = can_approve_amount(AP_CLERK, 100)
        assert ok is False
        assert "permission" in msg.lower()

    def test_auditor_cannot_approve(self):
        ok, _ = can_approve_amount(AUDITOR, 100)
        assert ok is False


class TestCanApproveInvoices:
    @pytest.mark.parametrize("role,expected", [
        (ADMIN, True),
        (MANAGER, True),
        (CFO, True),
        (APPROVER, True),
        (AP_CLERK, False),
        (AUDITOR, False),
        (SYSTEM, False),
    ])
    def test_approval_capability_by_role(self, role, expected):
        assert can_approve_invoices(role) is expected
