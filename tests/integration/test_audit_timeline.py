"""Integration tests for Live Audit Mode (app/services/audit_service.py).

Verifies an object opens as a chronological timeline where each event has a
derived plain-English reason, invoices carry the current policy routing reason,
and access follows object-view permissions (not auditor-only).
"""
import uuid
from datetime import date, timedelta

import pytest

from app.core.enums import UserRole
from app.models.invoice import Invoice
from app.models.audit_log import AuditLog
from app.services.audit_service import AuditService
from app.services.config_provisioning import ConfigProvisioningService
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


def _make_invoice(db, tenant_id, created_by, amount=300_000, state="approved"):
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="Acme Co",
        invoice_date=date(2026, 1, 1),
        total_amount=amount,
        current_state=state,
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


def _log(db, tenant_id, object_id, action, *, offset_s=0, user_email="clerk@x.io",
         user_role="AP_CLERK", after=None, comment=None, ai_assisted=False, ai_confidence=None):
    db.add(AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        object_type="invoice",
        object_id=object_id,
        action=action,
        user_email=user_email,
        user_role=user_role,
        after_value=after,
        comment=comment,
        ai_assisted=ai_assisted,
        ai_confidence=ai_confidence,
        timestamp=utc_now() + timedelta(seconds=offset_s),
    ))
    db.flush()


class TestTimeline:
    def test_summaries_order_and_policy_reason(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)

        inv = _make_invoice(db, tenant.id, admin["id"], amount=300_000, state="approved")
        _log(db, tenant.id, inv.id, "created", offset_s=1, ai_assisted=True, ai_confidence=95)
        _log(db, tenant.id, inv.id, "validated", offset_s=2)
        _log(db, tenant.id, inv.id, "submitted_for_approval", offset_s=3, after={"required_role": "cfo"})
        _log(db, tenant.id, inv.id, "approved", offset_s=4, user_role="CFO", user_email="cfo@x.io")

        tl = AuditService(db).get_timeline("invoice", inv.id, admin)

        assert tl["total_events"] == 4
        assert [e["action"] for e in tl["events"]] == [
            "created", "validated", "submitted_for_approval", "approved",
        ]
        assert "OCR confidence" in tl["events"][0]["summary"]
        assert "CFO" in tl["events"][2]["summary"]
        assert tl["current_state"] == "approved"
        # 300k > 250k -> CFO, sourced from the seeded policy.
        assert "CFO" in tl["policy_reason"]
        assert "250,000" in tl["policy_reason"]

    def test_rejected_event_includes_reason(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        inv = _make_invoice(db, tenant.id, admin["id"], amount=100_000, state="rejected")
        _log(db, tenant.id, inv.id, "rejected", comment="Missing PO number", user_role="MANAGER")

        tl = AuditService(db).get_timeline("invoice", inv.id, admin)
        assert tl["events"][0]["summary"] == "Rejected: Missing PO number"
        # No policy configured for this tenant -> default-routing explanation.
        assert "default routing" in tl["policy_reason"]

    def test_policy_reason_uses_snapshot_not_recomputed(self, db, tenant, make_user):
        # Current policy routes 300k to CFO, but the decision was recorded earlier
        # under a different rule. The timeline must show the snapshot, not drift.
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)  # 300k -> CFO now

        inv = _make_invoice(db, tenant.id, admin["id"], amount=300_000, state="approved")
        _log(
            db, tenant.id, inv.id, "submitted_for_approval", offset_s=1,
            after={
                "required_role": "manager",
                "policy_name": "Old rule",
                "routing_reason": "Amount 300,000 routed to MANAGER under the policy in effect then.",
            },
        )
        _log(db, tenant.id, inv.id, "approved", offset_s=2, after={"state": "approved"})

        tl = AuditService(db).get_timeline("invoice", inv.id, admin)
        assert "MANAGER" in tl["policy_reason"]
        assert "in effect then" in tl["policy_reason"]
        submitted = next(e for e in tl["events"] if e["action"] == "submitted_for_approval")
        assert "MANAGER" in submitted["summary"]

    def test_empty_timeline_for_object_without_events(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        inv = _make_invoice(db, tenant.id, admin["id"], state="draft")
        tl = AuditService(db).get_timeline("invoice", inv.id, admin)
        assert tl["total_events"] == 0
        assert tl["events"] == []
        assert tl["current_state"] == "draft"


class TestTimelinePermissions:
    def test_invoice_viewer_allowed(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        inv = _make_invoice(db, tenant.id, clerk["id"], state="draft")
        tl = AuditService(db).get_timeline("invoice", inv.id, clerk)
        assert tl["object_type"] == "invoice"

    def test_role_without_vendor_view_is_forbidden(self, db, make_user):
        # APPROVER lacks vendors.view, so cannot open a vendor timeline.
        approver = make_user(UserRole.APPROVER)
        with pytest.raises(PermissionError):
            AuditService(db).get_timeline("vendor", uuid.uuid4(), approver)
