from sqlalchemy.orm import Session
from typing import Dict

from app.repositories.workflow_repository import WorkflowRepository
from app.repositories.policy_repository import PolicyRepository
from app.models.workflow_state import WorkflowState
from app.models.policy import Policy
from app.services.audit import log_audit
from app.services.config_defaults import DEFAULT_INVOICE_STATES, DEFAULT_APPROVAL_POLICIES
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
        created_policies = self._seed_policies(tenant_id)

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

        return {"created_states": created_states, "created_policies": created_policies}

    def _seed_states(self, tenant_id) -> int:
        if self.workflow_repo.count_states(WORKFLOW_TYPE) > 0:
            return 0
        count = 0
        for name, display, order, is_initial, is_final, transitions, color in DEFAULT_INVOICE_STATES:
            self.workflow_repo.create(WorkflowState(
                tenant_id=tenant_id,
                workflow_type=WORKFLOW_TYPE,
                state_name=name,
                display_name=display,
                state_order=order,
                is_initial=is_initial,
                is_final=is_final,
                allowed_transitions=transitions,
                color=color,
            ))
            count += 1
        self.workflow_repo.commit()
        return count

    def _seed_policies(self, tenant_id) -> int:
        if self.policy_repo.list_by_type(POLICY_TYPE):
            return 0
        count = 0
        for name, priority, rule in DEFAULT_APPROVAL_POLICIES:
            self.policy_repo.create(Policy(
                tenant_id=tenant_id,
                policy_type=POLICY_TYPE,
                policy_name=name,
                description="Default approval routing rule (configuration-first).",
                rule_config=rule,
                applies_to=WORKFLOW_TYPE,
                is_active=True,
                priority=priority,
            ))
            count += 1
        self.policy_repo.commit()
        return count

    @staticmethod
    def _require_admin(current_user: dict) -> None:
        role = current_user["role"]
        if not (has_permission(role, PERM_MANAGE_POLICIES) and has_permission(role, PERM_MANAGE_WORKFLOW)):
            raise PermissionError("You do not have permission to initialize tenant configuration")
