"""Integration tests for policy-evaluation snapshots.

Build Book: every policy evaluation is stored with policy_version, inputs
snapshot, output decision, and reasons — so a routing decision stays
reproducible after the policy is edited, rolled back, or deleted.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.policy_eval import PolicyEval
from app.schemas.policy import ApprovalPolicyCreate, ApprovalPolicyUpdate, ApprovalRule
from app.services.policy import explain_approval_routing
from app.services.policy_eval import record_approval_routing_eval, PolicyEvalService
from app.services.policy_service import ApprovalPolicyService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.invoice_service import InvoiceService
from app.services.notification_service import NotificationService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


def _vendor(db, tenant_id, user_id):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=VendorStatus.ACTIVE, created_by=user_id)
    db.add(v)
    db.flush()
    return v


def _invoice(db, tenant_id, user_id, vendor_id, amount, state="validated"):
    inv = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="V", vendor_id=vendor_id, invoice_date=date(2026, 1, 1),
        total_amount=amount, current_state=state, created_by=user_id,
    )
    db.add(inv)
    db.flush()
    return inv


class TestRecording:
    def test_snapshot_captures_rule_version_inputs_and_decision(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        policy = ApprovalPolicyService(db).create_policy(
            ApprovalPolicyCreate(
                policy_name="CFO over 250k",
                rule=ApprovalRule(amount_threshold=250_000, operator="greater_than",
                                  required_role="cfo"),
                priority=100,
            ),
            admin,
        )
        routing = explain_approval_routing(db, tenant.id, 300_000)
        row = record_approval_routing_eval(
            db, tenant.id, routing, 300_000, "invoice", uuid.uuid4(), admin["id"]
        )

        assert row.policy_id == policy.id
        assert row.policy_name == "CFO over 250k"
        assert row.policy_version == 1           # from config_versions
        assert row.inputs["amount"] == 300_000
        assert row.inputs["matched_rule"]["required_role"] == "cfo"
        assert row.output["required_role"] == "cfo"
        assert "CFO" in row.reasons[0]

    def test_default_routing_records_no_policy(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)  # no policies configured
        routing = explain_approval_routing(db, tenant.id, 50_000)
        row = record_approval_routing_eval(
            db, tenant.id, routing, 50_000, "invoice", uuid.uuid4(), admin["id"]
        )
        assert row.policy_id is None
        assert row.policy_version is None
        assert row.output["required_role"] == "manager"
        assert "default routing" in row.reasons[0]

    def test_snapshot_survives_policy_edit(self, db, tenant, make_user):
        """The whole point: the recorded decision must still show the rule that
        actually applied, even after the policy is changed."""
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)
        policy = svc.create_policy(
            ApprovalPolicyCreate(
                policy_name="Threshold",
                rule=ApprovalRule(amount_threshold=250_000, operator="greater_than",
                                  required_role="cfo"),
                priority=100,
            ),
            admin,
        )
        routing = explain_approval_routing(db, tenant.id, 300_000)
        row = record_approval_routing_eval(
            db, tenant.id, routing, 300_000, "invoice", uuid.uuid4(), admin["id"]
        )
        recorded_version = row.policy_version

        # Policy is later edited: threshold raised so 300k would now route to manager.
        svc.update_policy(
            policy.id,
            ApprovalPolicyUpdate(rule=ApprovalRule(amount_threshold=900_000,
                                                   operator="greater_than",
                                                   required_role="cfo")),
            admin,
        )
        db.refresh(row)
        assert row.output["required_role"] == "cfo"          # unchanged
        assert row.inputs["matched_rule"]["amount_threshold"] == 250_000
        assert row.policy_version == recorded_version        # points at the old version


class TestWorkflowIntegration:
    def test_submitting_an_invoice_records_an_eval(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        vendor = _vendor(db, tenant.id, admin["id"])
        inv = _invoice(db, tenant.id, admin["id"], vendor.id, 300_000)

        InvoiceService(db).submit_for_approval(inv.id, admin)

        evals = db.query(PolicyEval).filter(PolicyEval.object_id == inv.id).all()
        assert len(evals) == 1
        assert evals[0].output["required_role"] == "cfo"     # 300k > 250k
        assert evals[0].policy_name is not None

    def test_approving_records_a_second_eval(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        vendor = _vendor(db, tenant.id, admin["id"])
        inv = _invoice(db, tenant.id, admin["id"], vendor.id, 100_000)

        svc = InvoiceService(db)
        svc.submit_for_approval(inv.id, admin)
        svc.approve_invoice(inv.id, admin)

        evals = db.query(PolicyEval).filter(PolicyEval.object_id == inv.id).all()
        assert len(evals) == 2                                # submit + approve


class TestReadAccess:
    def test_auditor_can_list_and_filter(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        auditor = make_user(UserRole.AUDITOR)
        target = uuid.uuid4()
        routing = explain_approval_routing(db, tenant.id, 10_000)
        record_approval_routing_eval(db, tenant.id, routing, 10_000, "invoice", target, admin["id"])
        record_approval_routing_eval(db, tenant.id, routing, 10_000, "invoice", uuid.uuid4(), admin["id"])
        db.flush()

        rows, total = PolicyEvalService(db).list_evals(auditor)
        assert total == 2
        rows, total = PolicyEvalService(db).list_evals(auditor, object_type="invoice", object_id=target)
        assert total == 1

    def test_role_without_audit_or_policy_rights_is_forbidden(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            PolicyEvalService(db).list_evals(clerk)
