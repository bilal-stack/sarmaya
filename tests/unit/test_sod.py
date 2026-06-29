"""Unit tests for Segregation-of-Duties checks (app/services/sod.py)."""
from types import SimpleNamespace

from app.services import sod


def _obj(created_by):
    return SimpleNamespace(created_by=created_by)


class TestSelfInvoiceApproval:
    def test_self_blocked_for_non_admin(self):
        assert sod.violates_self_invoice_approval(_obj("u1"), {"id": "u1", "role": "manager"}) is True

    def test_different_person_ok(self):
        assert sod.violates_self_invoice_approval(_obj("u2"), {"id": "u1", "role": "manager"}) is False

    def test_admin_is_exempt(self):
        assert sod.violates_self_invoice_approval(_obj("u1"), {"id": "u1", "role": "admin"}) is False

    def test_missing_creator_is_not_a_violation(self):
        assert sod.violates_self_invoice_approval(_obj(None), {"id": "u1", "role": "manager"}) is False


class TestSelfVendorActivation:
    def test_self_blocked_for_non_admin(self):
        assert sod.violates_self_vendor_activation(_obj("u1"), {"id": "u1", "role": "ap_clerk"}) is True

    def test_different_person_ok(self):
        assert sod.violates_self_vendor_activation(_obj("u2"), {"id": "u1", "role": "ap_clerk"}) is False

    def test_admin_is_exempt(self):
        assert sod.violates_self_vendor_activation(_obj("u1"), {"id": "u1", "role": "admin"}) is False
