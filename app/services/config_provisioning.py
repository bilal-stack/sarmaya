from sqlalchemy.orm import Session
from typing import Dict

from app.repositories.workflow_repository import WorkflowRepository
from app.repositories.policy_repository import PolicyRepository
from app.models.workflow_state import WorkflowState
from app.models.policy import Policy
from app.services.audit import log_audit
from app.services.config_versioning import (
    record_version, policy_snapshot, TYPE_APPROVAL_POLICY,
)
from app.services.config_defaults import DEFAULT_WORKFLOWS, DEFAULT_APPROVAL_POLICIES
from app.core.roles import has_permission, PERM_MANAGE_POLICIES, PERM_MANAGE_WORKFLOW

POLICY_TYPE = "approval_limit"
WORKFLOW_TYPE = "invoice"


class ConfigProvisioningService:
    """Seeds a tenant's default workflow + approval configuration so it is
    configuration-first from day one instead of falling back to hardcoded rules.
    Idempotent: existing config is left untouched."""

    def __init__(self, db: Session):
        self.db = db
        self.workflow_repo = WorkflowRepository(db)
        self.policy_repo = PolicyRepository(db)

    def initialize_defaults(self, current_user: dict) -> Dict[str, int]:
        """Seed default invoice states and approval policies for the caller's
        tenant if absent. Returns counts of what was created (0 when already
        configured)."""
        self._require_admin(current_user)
        tenant_id = current_user["tenant_id"]

        created_states = self._seed_states(tenant_id)
        created_policies = self._seed_policies(tenant_id, current_user["id"])

        if created_states or created_policies:
            log_audit(
                db=self.db,
                tenant_id=tenant_id,
                user_id=current_user["id"],
                object_type="tenant_config",
                object_id=tenant_id,
                action="defaults_initialized",
                after_value={
                    "created_states": created_states,
                    "created_policies": created_policies,
                },
            )
            # The seeders commit their own rows; without this the entry saying
            # who provisioned the tenant is flushed and then discarded.
            self.db.commit()

        return {"created_states": created_states, "created_policies": created_policies}

    def _seed_states(self, tenant_id) -> int:
        """Seed each workflow independently.

        Checked per workflow_type rather than once overall, so a tenant
        provisioned before a workflow existed picks it up on the next run
        instead of being permanently stuck without it. Existing states are
        never touched — a tenant that has edited its own workflow keeps it.
        """
        return sum(
            self._seed_workflow(tenant_id, workflow_type, states)
            for workflow_type, states in DEFAULT_WORKFLOWS.items()
        )

    def _seed_workflow(self, tenant_id, workflow_type: str, states) -> int:
        # Named explicitly rather than left to the session's bound tenant. This
        # is a provisioning path, and provisioning runs in places where nothing
        # is bound — a setup script, an onboarding job, a migration. On such a
        # session the check saw the *previous* tenant's states, concluded the
        # workflow was already configured, and seeded nothing: the second tenant
        # came up with no states and no approval matrix, every routing decision
        # silently falling back to the hardcoded defaults. Found while
        # provisioning two tenants in one script.
        if self._existing_states(tenant_id, workflow_type) > 0:
            return 0
        count = 0
        for name, display, order, is_initial, is_final, transitions, color, guards, sla in states:
            self.workflow_repo.create(WorkflowState(
                tenant_id=tenant_id,
                workflow_type=workflow_type,
                state_name=name,
                display_name=display,
                state_order=order,
                is_initial=is_initial,
                is_final=is_final,
                allowed_transitions=transitions,
                guards=guards,
                sla=sla,
                color=color,
            ))
            count += 1
        self.workflow_repo.commit()
        return count

    def _seed_policies(self, tenant_id, changed_by) -> int:
        # Tenant named explicitly, for the same reason as _seed_workflow above.
        if self._existing_policies(tenant_id) > 0:
            return 0
        count = 0
        for name, priority, rule in DEFAULT_APPROVAL_POLICIES:
            policy = self.policy_repo.create(Policy(
                tenant_id=tenant_id,
                policy_type=POLICY_TYPE,
                policy_name=name,
                description="Default approval routing rule (configuration-first).",
                rule_config=rule,
                applies_to=WORKFLOW_TYPE,
                is_active=True,
                priority=priority,
            ))
            # Version the seeded rule the same way an edited one is versioned.
            # Every policy evaluation records the policy_version that decided it
            # (DR-010), and that number only means something if it points at a
            # restorable snapshot. Without this the defaults had no history, so
            # every routing decision made by them recorded a null version and
            # could not be reproduced once the rule was edited.
            self.db.flush()
            record_version(
                self.db, tenant_id, TYPE_APPROVAL_POLICY, policy.id,
                policy_snapshot(policy), "created", changed_by,
            )
            count += 1
        self.policy_repo.commit()
        return count

    @staticmethod
    def _require_admin(current_user: dict) -> None:
        role = current_user["role"]
        if not (has_permission(role, PERM_MANAGE_POLICIES) and has_permission(role, PERM_MANAGE_WORKFLOW)):
            raise PermissionError("You do not have permission to initialize tenant configuration")

    # --- explicit tenant checks -------------------------------------------

    def _existing_states(self, tenant_id, workflow_type: str) -> int:
        """States this tenant already has for a workflow.

        Deliberately not the repository's `count_states`, which names no tenant
        and so answers for whichever tenant the session happens to be bound to
        — or, on an unbound session, for all of them at once.
        """
        return (
            self.db.query(WorkflowState)
            .filter(
                WorkflowState.tenant_id == tenant_id,
                WorkflowState.workflow_type == workflow_type,
            )
            .count()
        )

    def _existing_policies(self, tenant_id) -> int:
        return (
            self.db.query(Policy)
            .filter(Policy.tenant_id == tenant_id, Policy.policy_type == POLICY_TYPE)
            .count()
        )
