from sqlalchemy.orm import Session
from typing import List

from app.repositories.workflow_repository import WorkflowRepository
from app.models.workflow_state import WorkflowState
from app.services.audit import log_audit
from app.services.config_versioning import record_version, workflow_snapshot, TYPE_WORKFLOW
from app.core.roles import has_permission, PERM_MANAGE_WORKFLOW


class WorkflowConfigService:
    """Admin read/edit of workflow state transitions. allowed_transitions is
    read by transition_state, so editing it reshapes the state machine without a
    code deploy (configuration-first)."""

    def __init__(self, db: Session):
        self.db = db
        self.repository = WorkflowRepository(db)

    def list_states(self, workflow_type: str, current_user: dict) -> List[WorkflowState]:
        self._require_manage(current_user)
        return self.repository.list_states(workflow_type)

    def update_transitions(
        self,
        workflow_type: str,
        state_name: str,
        allowed_transitions: List[str],
        current_user: dict,
    ) -> WorkflowState:
        self._require_manage(current_user)

        state = self.repository.get_state(workflow_type, state_name)
        if not state:
            raise ValueError(f"No '{state_name}' state configured for workflow '{workflow_type}'")

        valid_targets = {
            s.state_name for s in self.repository.list_states(workflow_type)
        }
        unknown = [t for t in allowed_transitions if t not in valid_targets]
        if unknown:
            raise ValueError(
                f"Unknown target state(s): {', '.join(unknown)}. "
                f"Valid states: {', '.join(sorted(valid_targets))}"
            )

        before = list(state.allowed_transitions or [])
        state.allowed_transitions = allowed_transitions
        state = self.repository.update(state)

        # Version the whole workflow (all states + transitions) as one document.
        record_version(
            self.db, current_user["tenant_id"], TYPE_WORKFLOW, workflow_type,
            workflow_snapshot(self.repository.list_states(workflow_type)),
            "updated", current_user["id"],
        )
        self.repository.commit()
        state = self.repository.refresh(state)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="workflow_state",
            object_id=state.id,
            action="transitions_updated",
            workflow_type=workflow_type,
            workflow_step=state.state_name,
            before_value={"allowed_transitions": before},
            after_value={"allowed_transitions": allowed_transitions},
        )
        return state

    @staticmethod
    def _require_manage(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to manage workflow configuration")
