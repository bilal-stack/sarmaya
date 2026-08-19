"""Integration tests for workflow SLAs + escalation.

Timers start on state entry (state_entered_at), deadlines are computed at read
time from per-state SLA config, breaches surface as overdue in the Decision
Inbox (and expand visibility to the escalation role), and the escalation runner
records one audited escalation per state entry.
"""
import uuid
from datetime import date, timedelta

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.workflow_state import WorkflowState
from app.models.audit_log import AuditLog
from app.services.workflow import transition_state
from app.services.decision_inbox_service import DecisionInboxService
from app.services.sla_service import SlaService
from app.services.workflow_config_service import WorkflowConfigService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.config_versioning import ConfigVersionService, TYPE_WORKFLOW
from app.services.notification_service import NotificationService
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


def _seed_pending_state(db, tenant_id, sla=None):
    db.add(WorkflowState(
        tenant_id=tenant_id, workflow_type="invoice", state_name="pending_approval",
        state_order=3, allowed_transitions=["approved", "rejected"], sla=sla or {},
    ))
    db.flush()


def _vendor(db, tenant_id, user_id):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=VendorStatus.ACTIVE, created_by=user_id)
    db.add(v)
    db.flush()
    return v


def _pending_invoice(db, tenant_id, user_id, vendor_id, entered_hours_ago=0, amount=100_000):
    inv = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="V", vendor_id=vendor_id, invoice_date=date(2026, 1, 1),
        total_amount=amount, current_state="pending_approval", created_by=user_id,
        state_entered_at=utc_now() - timedelta(hours=entered_hours_ago),
    )
    db.add(inv)
    db.flush()
    return inv


class TestSlaTimer:
    def test_transition_restarts_the_timer(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        db.add(WorkflowState(
            tenant_id=tenant.id, workflow_type="invoice", state_name="draft",
            state_order=1, allowed_transitions=["validated"],
        ))
        db.flush()
        vendor = _vendor(db, tenant.id, admin["id"])
        old = utc_now() - timedelta(days=2)
        inv = Invoice(
            tenant_id=tenant.id, invoice_number="INV-1", vendor_name="V",
            vendor_id=vendor.id, invoice_date=date(2026, 1, 1), total_amount=100,
            current_state="draft", created_by=admin["id"], state_entered_at=old,
        )
        db.add(inv)
        db.flush()

        transition_state(db, inv, "validated", admin["id"])
        assert inv.state_entered_at > old + timedelta(days=1)


class TestInboxOverdue:
    def test_breached_item_is_overdue_and_sorts_first(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        _seed_pending_state(db, tenant.id, sla={"hours": 48, "escalate_to": "cfo"})
        vendor = _vendor(db, tenant.id, manager["id"])
        _pending_invoice(db, tenant.id, manager["id"], vendor.id, entered_hours_ago=72, amount=10_000)
        _pending_invoice(db, tenant.id, manager["id"], vendor.id, entered_hours_ago=1, amount=900_000)

        # 900k routes to CFO under the default fallback, so give the manager the
        # small overdue one and check ordering among what an admin sees.
        admin = make_user(UserRole.ADMIN)
        inbox = DecisionInboxService(db).get_inbox(admin)
        assert inbox["overdue_count"] == 1
        assert inbox["items"][0]["overdue"] is True       # overdue first despite smaller amount
        assert inbox["items"][0]["sla_due_at"] is not None

    def test_overdue_only_filter(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _seed_pending_state(db, tenant.id, sla={"hours": 48})
        vendor = _vendor(db, tenant.id, admin["id"])
        _pending_invoice(db, tenant.id, admin["id"], vendor.id, entered_hours_ago=72)
        _pending_invoice(db, tenant.id, admin["id"], vendor.id, entered_hours_ago=1)

        inbox = DecisionInboxService(db).get_inbox(admin, overdue_only=True)
        assert inbox["total"] == 1
        assert all(it["overdue"] for it in inbox["items"])

    def test_escalation_role_sees_breached_item(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        _seed_pending_state(db, tenant.id, sla={"hours": 48, "escalate_to": "cfo"})
        vendor = _vendor(db, tenant.id, clerk["id"])
        # 100k requires MANAGER, so the CFO would normally not see it...
        _pending_invoice(db, tenant.id, clerk["id"], vendor.id, entered_hours_ago=72)

        inbox = DecisionInboxService(db).get_inbox(cfo)
        assert inbox["total"] == 1
        assert inbox["items"][0]["escalated"] is True
        assert "Escalated to you" in inbox["items"][0]["reason"]

    def test_escalation_role_does_not_see_unbreached_item(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        _seed_pending_state(db, tenant.id, sla={"hours": 48, "escalate_to": "cfo"})
        vendor = _vendor(db, tenant.id, clerk["id"])
        _pending_invoice(db, tenant.id, clerk["id"], vendor.id, entered_hours_ago=1)

        assert DecisionInboxService(db).get_inbox(cfo)["total"] == 0


class TestEscalationRunner:
    def test_escalates_once_per_state_entry(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _seed_pending_state(db, tenant.id, sla={"hours": 48, "escalate_to": "cfo"})
        vendor = _vendor(db, tenant.id, admin["id"])
        inv = _pending_invoice(db, tenant.id, admin["id"], vendor.id, entered_hours_ago=72)

        first = SlaService(db).run_escalations(admin)
        assert first["escalated_count"] == 1
        assert first["items"][0]["escalated_to"] == "cfo"

        events = db.query(AuditLog).filter(
            AuditLog.object_id == inv.id, AuditLog.action == "sla_escalated"
        ).all()
        assert len(events) == 1

        # Idempotent: nothing new on a second run.
        assert SlaService(db).run_escalations(admin)["escalated_count"] == 0

    def test_escalates_a_requisition_once_too(self, db, tenant, make_user):
        """The runner scans every declared workflow, but the once-per-state-entry
        check queried audit rows with object_type "invoice" hardcoded. The
        escalation writes the workflow type instead, so for every non-invoice
        workflow the check never matched: each run escalated the same overdue
        record again and re-notified the approver, indefinitely.
        """
        from decimal import Decimal
        from app.core.enums import RequisitionState
        from app.models.requisition import PurchaseRequisition

        admin = make_user(UserRole.ADMIN)
        db.add(WorkflowState(
            tenant_id=tenant.id, workflow_type="requisition",
            state_name="pending_approval", state_order=2,
            allowed_transitions=["approved", "rejected"],
            sla={"hours": 24, "escalate_to": "cfo"},
        ))
        req = PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-SLA",
            title="Laptops", justification="Overdue for approval.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("1000"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=admin["id"],
            state_entered_at=utc_now() - timedelta(hours=72),
        )
        db.add(req)
        db.flush()

        assert SlaService(db).run_escalations(admin)["escalated_count"] == 1
        assert SlaService(db).run_escalations(admin)["escalated_count"] == 0

        events = db.query(AuditLog).filter(
            AuditLog.object_id == req.id, AuditLog.action == "sla_escalated"
        ).all()
        assert len(events) == 1

    def test_a_requisition_escalation_actually_reaches_the_approver(
        self, db, tenant, make_user, monkeypatch
    ):
        """`notify_sla_escalation` read `invoice_number` off whatever it was
        given, so for a requisition it raised — and its own `except` logged the
        failure and moved on. The audit trail said the breach was escalated to
        the CFO while no message was ever sent. Assert on what was sent, since
        no exception will ever surface this.
        """
        from decimal import Decimal
        from app.core.enums import RequisitionState
        from app.models.requisition import PurchaseRequisition

        sent: list = []
        monkeypatch.setattr(
            NotificationService, "_send",
            lambda self, tenant_id, recipients, subject, body, category=None, link=None: sent.append((recipients, subject)),
        )

        admin = make_user(UserRole.ADMIN)
        cfo = make_user(UserRole.CFO)
        db.add(WorkflowState(
            tenant_id=tenant.id, workflow_type="requisition",
            state_name="pending_approval", state_order=2,
            allowed_transitions=["approved", "rejected"],
            sla={"hours": 24, "escalate_to": "cfo"},
        ))
        db.add(PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-NOTIFY",
            title="Laptops", justification="Overdue for approval.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("1000"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=admin["id"],
            state_entered_at=utc_now() - timedelta(hours=72),
        ))
        db.flush()

        SlaService(db).run_escalations(admin)

        assert sent, "the breach was escalated but nobody was notified"
        recipients, subject = sent[0]
        assert cfo["email"] in recipients
        assert "REQ-NOTIFY" in subject

    def test_requires_manage_workflow_permission(self, db, make_user):
        manager = make_user(UserRole.MANAGER)
        with pytest.raises(PermissionError):
            SlaService(db).run_escalations(manager)


class TestSlaConfig:
    def test_update_sla_versions_the_workflow(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)

        state = WorkflowConfigService(db).update_sla("invoice", "pending_approval", 24, "cfo", admin)
        assert state.sla == {"hours": 24, "escalate_to": "cfo"}

        versions = ConfigVersionService(db).list_versions(TYPE_WORKFLOW, "invoice", admin)
        snap_state = next(
            s for s in versions[0].snapshot["states"] if s["state_name"] == "pending_approval"
        )
        assert snap_state["sla"] == {"hours": 24, "escalate_to": "cfo"}

    def test_escalate_to_without_hours_rejected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        with pytest.raises(ValueError):
            WorkflowConfigService(db).update_sla("invoice", "pending_approval", None, "cfo", admin)
