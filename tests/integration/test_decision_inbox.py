"""Integration tests for the Decision Inbox (decision_inbox_service.py).

Verifies each pending invoice surfaces once under its most-blocking action
(duplicate > vendor > approval), that items are filtered to what the caller can
do, and that ordering/links are correct.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.services.decision_inbox_service import DecisionInboxService
from app.services.config_provisioning import ConfigProvisioningService

pytestmark = pytest.mark.integration


def _vendor(db, tenant_id, status=VendorStatus.ACTIVE):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id,
               legal_name=f"V-{uuid.uuid4().hex[:6]}", status=status)
    db.add(v)
    db.flush()
    return v


def _invoice(db, tenant_id, created_by, amount, *, vendor=None, state="pending_approval",
             potential_dup=None, dup_ack=False):
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name if vendor else "Acme",
        vendor_id=vendor.id if vendor else None,
        invoice_date=date(2026, 1, 1),
        total_amount=amount,
        current_state=state,
        created_by=created_by,
        potential_duplicate_id=potential_dup,
        duplicate_acknowledged=dup_ack,
    )
    db.add(inv)
    db.flush()
    return inv


def _scenario(db, tenant, created_by):
    """One invoice of each blocker type, plus a manager-level approval."""
    active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
    unverified = _vendor(db, tenant.id, VendorStatus.PENDING_VERIFICATION)

    original = _invoice(db, tenant.id, created_by, 100_000, vendor=active, state="approved")
    flagged = _invoice(db, tenant.id, created_by, 100_000, vendor=active, potential_dup=original.id)
    vendor_blocked = _invoice(db, tenant.id, created_by, 50_000, vendor=unverified)
    cfo_approval = _invoice(db, tenant.id, created_by, 300_000, vendor=active)
    mgr_approval = _invoice(db, tenant.id, created_by, 80_000, vendor=active)
    return {
        "flagged": flagged, "vendor_blocked": vendor_blocked,
        "cfo_approval": cfo_approval, "mgr_approval": mgr_approval,
    }


class TestDecisionInbox:
    def test_admin_sees_all_categories_prioritized(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        s = _scenario(db, tenant, admin["id"])

        inbox = DecisionInboxService(db).get_inbox(admin)

        assert inbox["counts"]["duplicate_review"] == 1
        assert inbox["counts"]["vendor_verification"] == 1
        assert inbox["counts"]["approval"] == 2  # admin sees both mgr + cfo approvals
        # Highest priority first.
        assert inbox["items"][0]["category"] == "duplicate_review"
        assert [i["priority"] for i in inbox["items"]] == sorted(i["priority"] for i in inbox["items"])
        # Timeline link points at the invoice.
        dup_item = next(i for i in inbox["items"] if i["category"] == "duplicate_review")
        assert dup_item["timeline_url"].endswith(f"/invoice/{s['flagged'].id}")

    def test_clerk_sees_only_vendor_verification(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        clerk = make_user(UserRole.AP_CLERK)
        _scenario(db, tenant, clerk["id"])

        inbox = DecisionInboxService(db).get_inbox(clerk)

        # Clerk can manage vendors but not approve.
        assert set(inbox["counts"]) == {"vendor_verification"}
        assert inbox["total"] == 1

    def test_cfo_sees_duplicate_and_cfo_approval_only(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        cfo = make_user(UserRole.CFO)
        _scenario(db, tenant, cfo["id"])

        inbox = DecisionInboxService(db).get_inbox(cfo)

        # CFO can approve + review duplicates, but not manage vendors, and only
        # the CFO-routed approval matches their role.
        assert inbox["counts"].get("duplicate_review") == 1
        assert inbox["counts"].get("approval") == 1
        assert "vendor_verification" not in inbox["counts"]
        approval = next(i for i in inbox["items"] if i["category"] == "approval")
        assert approval["required_role"] == "cfo"
        assert approval["amount"] == 300_000

    def test_auditor_inbox_is_empty(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        auditor = make_user(UserRole.AUDITOR)
        _scenario(db, tenant, auditor["id"])

        inbox = DecisionInboxService(db).get_inbox(auditor)
        assert inbox["total"] == 0
        assert inbox["items"] == []
