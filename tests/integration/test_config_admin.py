"""Integration tests for configuration-first behaviour.

Proves the approval matrix and workflow transitions are driven by editable DB
config rather than hardcode:
  * evaluate_approval_role reads approval_limit policies; editing one changes
    routing.
  * transition_state reads workflow_states.allowed_transitions; a configured
    transition the hardcoded machine would forbid is allowed.
  * the admin config services enforce manage permissions.

These run as the privileged role (RLS bypassed); they exercise service/business
logic, not tenant isolation.
"""
import uuid

import pytest

from app.core.enums import UserRole, InvoiceState
from app.models.invoice import Invoice
from app.models.workflow_state import WorkflowState
from app.services.policy import evaluate_approval_role
from app.services.workflow import transition_state
from app.services.policy_service import ApprovalPolicyService
from app.services.workflow_config_service import WorkflowConfigService
from app.schemas.policy import ApprovalPolicyCreate, ApprovalPolicyUpdate, ApprovalRule

pytestmark = pytest.mark.integration


def _seed_states(db, tenant_id, states):
    """states: list of (state_name, order, allowed_transitions)."""
    for name, order, allowed in states:
        db.add(WorkflowState(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            workflow_type="invoice",
            state_name=name,
            display_name=name.title(),
            state_order=order,
            allowed_transitions=allowed,
        ))
    db.flush()


# --- approval policy CRUD ----------------------------------------------------

class TestApprovalPolicyCrud:
    def test_admin_can_create_list_update_delete(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)

        created = svc.create_policy(
            ApprovalPolicyCreate(
                policy_name="High value",
                rule=ApprovalRule(amount_threshold=500_000, operator="greater_than", required_role="cfo"),
                priority=100,
            ),
            admin,
        )
        assert created.rule_config["required_role"] == "cfo"
        assert created.policy_type == "approval_limit"

        assert any(p.id == created.id for p in svc.list_policies(admin))

        updated = svc.update_policy(
            created.id, ApprovalPolicyUpdate(priority=5, is_active=False), admin
        )
        assert updated.priority == 5
        assert updated.is_active is False

        svc.delete_policy(created.id, admin)
        with pytest.raises(ValueError):
            svc.get_policy(created.id, admin)

    def test_duplicate_name_rejected(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)
        payload = ApprovalPolicyCreate(
            policy_name="Dup",
            rule=ApprovalRule(amount_threshold=0, operator="greater_equal", required_role="manager"),
        )
        svc.create_policy(payload, admin)
        with pytest.raises(ValueError):
            svc.create_policy(payload, admin)

    def test_non_admin_cannot_manage(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        svc = ApprovalPolicyService(db)
        with pytest.raises(PermissionError):
            svc.list_policies(clerk)
        with pytest.raises(PermissionError):
            svc.create_policy(
                ApprovalPolicyCreate(
                    policy_name="X",
                    rule=ApprovalRule(amount_threshold=0, operator="greater_equal", required_role="manager"),
                ),
                clerk,
            )


# --- config actually drives routing ------------------------------------------

class TestPolicyDrivesRouting:
    def test_editing_policy_changes_required_role(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        tenant_id = admin["tenant_id"]

        # No policies yet -> hardcoded fallback (<=250k manager).
        assert evaluate_approval_role(db, tenant_id, 100_000) == "manager"

        # Configure a rule routing anything over 50k to the CFO.
        ApprovalPolicyService(db).create_policy(
            ApprovalPolicyCreate(
                policy_name="Over 50k to CFO",
                rule=ApprovalRule(amount_threshold=50_000, operator="greater_than", required_role="cfo"),
                priority=100,
            ),
            admin,
        )

        # Same 100k invoice now routes to CFO purely from config.
        assert evaluate_approval_role(db, tenant_id, 100_000) == "cfo"


# --- workflow transitions config ---------------------------------------------

class TestWorkflowConfigDrivesTransitions:
    def test_configured_transition_overrides_hardcoded_machine(self, db, tenant, make_user):
        # draft -> pending_approval is forbidden by the hardcoded state machine,
        # but allowed here via config; transition_state must honour the DB.
        _seed_states(db, tenant.id, [
            ("draft", 1, ["pending_approval"]),
            ("pending_approval", 3, []),
        ])
        user = make_user(UserRole.AP_CLERK)

        inv = Invoice(tenant_id=tenant.id, current_state=InvoiceState.DRAFT.value)
        assert transition_state(db, inv, InvoiceState.PENDING_APPROVAL.value, user["id"]) is True
        assert inv.current_state == InvoiceState.PENDING_APPROVAL.value

    def test_update_transitions_reshapes_machine(self, db, tenant, make_user):
        _seed_states(db, tenant.id, [
            ("draft", 1, ["validated"]),
            ("validated", 2, []),
            ("cancelled", 7, []),
        ])
        admin = make_user(UserRole.ADMIN)
        svc = WorkflowConfigService(db)

        # Initially draft -> validated is allowed, draft -> cancelled is not.
        inv = Invoice(tenant_id=tenant.id, current_state="draft")
        assert transition_state(db, inv, "validated", admin["id"]) is True
        # transition_state db.add()s the in-memory invoice; drop it so the
        # service commit below doesn't try to flush a half-built row.
        db.expunge(inv)

        svc.update_transitions("invoice", "draft", ["cancelled"], admin)

        inv2 = Invoice(tenant_id=tenant.id, current_state="draft")
        with pytest.raises(ValueError):
            transition_state(db, inv2, "validated", admin["id"])
        inv3 = Invoice(tenant_id=tenant.id, current_state="draft")
        assert transition_state(db, inv3, "cancelled", admin["id"]) is True

    def test_update_unknown_target_rejected(self, db, tenant, make_user):
        _seed_states(db, tenant.id, [("draft", 1, ["validated"]), ("validated", 2, [])])
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            WorkflowConfigService(db).update_transitions("invoice", "draft", ["nonexistent"], admin)

    def test_non_admin_cannot_manage_workflow(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            WorkflowConfigService(db).list_states("invoice", clerk)
