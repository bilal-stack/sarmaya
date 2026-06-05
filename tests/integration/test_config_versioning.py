"""Append-only version history for editable config
(app/services/config_versioning.py).

Every change to an approval policy, the autopilot settings, or a workflow's
transitions appends a full JSON snapshot under a monotonic version number, so
the history of each config object is reconstructable.
"""
import pytest

from app.core.enums import UserRole
from app.schemas.policy import ApprovalPolicyCreate, ApprovalPolicyUpdate, ApprovalRule
from app.schemas.autopilot import AutopilotConfig
from app.services.policy_service import ApprovalPolicyService
from app.services.workflow_config_service import WorkflowConfigService
from app.services.autopilot_service import AutopilotService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.config_versioning import (
    ConfigVersionService,
    TYPE_APPROVAL_POLICY,
    TYPE_AUTOPILOT,
    TYPE_WORKFLOW,
)

pytestmark = pytest.mark.integration


def _rule(threshold, role="cfo"):
    return ApprovalRule(amount_threshold=threshold, operator="greater_than", required_role=role)


class TestPolicyVersioning:
    def test_create_then_update_increments_versions(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)

        policy = svc.create_policy(
            ApprovalPolicyCreate(policy_name="CFO rule", rule=_rule(250_000), priority=10),
            admin,
        )
        svc.update_policy(
            policy.id, ApprovalPolicyUpdate(rule=_rule(500_000)), admin
        )

        versions = ConfigVersionService(db).list_versions(
            TYPE_APPROVAL_POLICY, str(policy.id), admin
        )
        # Newest first.
        assert [v.version for v in versions] == [2, 1]
        assert [v.change_action for v in versions] == ["updated", "created"]
        assert versions[0].snapshot["rule_config"]["amount_threshold"] == 500_000
        assert versions[1].snapshot["rule_config"]["amount_threshold"] == 250_000

    def test_versions_are_independent_per_policy(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)
        a = svc.create_policy(ApprovalPolicyCreate(policy_name="A", rule=_rule(100)), admin)
        b = svc.create_policy(ApprovalPolicyCreate(policy_name="B", rule=_rule(200)), admin)

        va = ConfigVersionService(db).list_versions(TYPE_APPROVAL_POLICY, str(a.id), admin)
        vb = ConfigVersionService(db).list_versions(TYPE_APPROVAL_POLICY, str(b.id), admin)
        assert [v.version for v in va] == [1]
        assert [v.version for v in vb] == [1]

    def test_delete_records_a_final_version(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = ApprovalPolicyService(db)
        policy = svc.create_policy(ApprovalPolicyCreate(policy_name="Doomed", rule=_rule(1)), admin)
        svc.delete_policy(policy.id, admin)

        versions = ConfigVersionService(db).list_versions(TYPE_APPROVAL_POLICY, str(policy.id), admin)
        assert versions[0].version == 2
        assert versions[0].change_action == "deleted"


class TestWorkflowVersioning:
    def test_transition_edit_versions_whole_workflow(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)  # seed states

        WorkflowConfigService(db).update_transitions("invoice", "draft", ["validated"], admin)

        versions = ConfigVersionService(db).list_versions(TYPE_WORKFLOW, "invoice", admin)
        assert versions[0].version == 1
        snapshot = versions[0].snapshot
        # The snapshot is the whole workflow document, not just the edited state.
        names = {s["state_name"] for s in snapshot["states"]}
        assert "draft" in names and "validated" in names
        draft = next(s for s in snapshot["states"] if s["state_name"] == "draft")
        assert draft["allowed_transitions"] == ["validated"]


class TestAutopilotVersioning:
    def test_set_config_versions_singleton(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = AutopilotService(db)
        svc.set_config(AutopilotConfig(enabled=True, max_auto_approve_amount=1000), admin)
        svc.set_config(AutopilotConfig(enabled=True, max_auto_approve_amount=5000), admin)

        versions = ConfigVersionService(db).list_versions(TYPE_AUTOPILOT, TYPE_AUTOPILOT, admin)
        assert [v.version for v in versions] == [2, 1]
        assert versions[0].snapshot["max_auto_approve_amount"] == 5000


class TestVersionReadAccess:
    def test_get_specific_version(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        policy = ApprovalPolicyService(db).create_policy(
            ApprovalPolicyCreate(policy_name="X", rule=_rule(7)), admin
        )
        v1 = ConfigVersionService(db).get_version(TYPE_APPROVAL_POLICY, str(policy.id), 1, admin)
        assert v1.version == 1
        assert v1.snapshot["policy_name"] == "X"

    def test_missing_version_raises(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            ConfigVersionService(db).get_version(TYPE_APPROVAL_POLICY, "nope", 99, admin)

    def test_unknown_config_type_raises(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            ConfigVersionService(db).list_versions("bogus", "x", admin)

    def test_role_without_manage_is_forbidden(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)  # lacks policies.manage
        with pytest.raises(PermissionError):
            ConfigVersionService(db).list_versions(TYPE_APPROVAL_POLICY, "x", clerk)
