"""Append-only versioning for editable tenant configuration.

`record_version` is called by the config services after every change to an
approval policy, the autopilot settings, or a workflow's transitions. It stores
the full post-change snapshot under a monotonic version number, so the history
of any config object is reconstructable and auditable (configuration-first +
workflow-first per the Build Book). The snapshot builders here are the single
source of truth for what a given config type's JSON looks like.
"""
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.config_version import ConfigVersion
from app.models.policy import Policy
from app.models.workflow_state import WorkflowState
from app.core.roles import (
    has_permission,
    PERM_MANAGE_POLICIES,
    PERM_MANAGE_WORKFLOW,
)

# config_type values
TYPE_APPROVAL_POLICY = "approval_policy"
TYPE_AUTOPILOT = "autopilot"
TYPE_WORKFLOW = "workflow"

# Live-table identifiers (kept local to avoid importing the config services,
# which import this module). Must match policy_service / autopilot_service /
# config_provisioning.
_APPROVAL_POLICY_TYPE = "approval_limit"
_APPROVAL_APPLIES_TO = "invoice"
_AUTOPILOT_POLICY_TYPE = "autopilot"
_AUTOPILOT_POLICY_NAME = "Autopilot settings"

# Which permission may read a config type's history.
_VIEW_PERMISSION = {
    TYPE_APPROVAL_POLICY: PERM_MANAGE_POLICIES,
    TYPE_AUTOPILOT: PERM_MANAGE_POLICIES,
    TYPE_WORKFLOW: PERM_MANAGE_WORKFLOW,
}


# --- snapshot builders ------------------------------------------------------

def policy_snapshot(policy: Policy) -> dict:
    return {
        "policy_name": policy.policy_name,
        "description": policy.description,
        "rule_config": policy.rule_config,
        "applies_to": policy.applies_to,
        "is_active": policy.is_active,
        "priority": policy.priority,
    }


def workflow_snapshot(states: List[WorkflowState]) -> dict:
    """The whole workflow as one document: its states and their transitions,
    ordered — 'versioned JSON per workflow'."""
    return {
        "states": [
            {
                "state_name": s.state_name,
                "display_name": s.display_name,
                "state_order": s.state_order,
                "is_initial": bool(s.is_initial),
                "is_final": bool(s.is_final),
                "allowed_transitions": list(s.allowed_transitions or []),
                "guards": dict(s.guards or {}),
                "color": s.color,
            }
            for s in sorted(states, key=lambda s: s.state_order or 0)
        ]
    }


# --- write ------------------------------------------------------------------

def record_version(
    db: Session,
    tenant_id,
    config_type: str,
    config_key: str,
    snapshot: dict,
    change_action: str,
    changed_by,
    change_reason: Optional[str] = None,
) -> ConfigVersion:
    """Append the next version for a config object. Call before the surrounding
    transaction commits so the version persists atomically with the change."""
    next_version = (
        db.query(func.coalesce(func.max(ConfigVersion.version), 0))
        .filter(
            ConfigVersion.tenant_id == tenant_id,
            ConfigVersion.config_type == config_type,
            ConfigVersion.config_key == str(config_key),
        )
        .scalar()
    ) + 1

    row = ConfigVersion(
        tenant_id=tenant_id,
        config_type=config_type,
        config_key=str(config_key),
        version=next_version,
        snapshot=snapshot,
        change_action=change_action,
        change_reason=change_reason,
        changed_by=changed_by,
    )
    db.add(row)
    db.flush()
    return row


# --- read -------------------------------------------------------------------

class ConfigVersionService:
    """Read access to config history, gated by the same permission that lets a
    user edit that config type."""

    def __init__(self, db: Session):
        self.db = db

    def list_versions(
        self, config_type: str, config_key: str, current_user: dict
    ) -> List[ConfigVersion]:
        self._require_view(config_type, current_user)
        return (
            self.db.query(ConfigVersion)
            .filter(
                ConfigVersion.config_type == config_type,
                ConfigVersion.config_key == str(config_key),
            )
            .order_by(ConfigVersion.version.desc())
            .all()
        )

    def get_version(
        self, config_type: str, config_key: str, version: int, current_user: dict
    ) -> ConfigVersion:
        self._require_view(config_type, current_user)
        row = (
            self.db.query(ConfigVersion)
            .filter(
                ConfigVersion.config_type == config_type,
                ConfigVersion.config_key == str(config_key),
                ConfigVersion.version == version,
            )
            .first()
        )
        if not row:
            raise ValueError("Config version not found")
        return row

    # --- rollback -----------------------------------------------------------

    def restore_version(
        self, config_type: str, config_key: str, version: int, current_user: dict
    ) -> ConfigVersion:
        """Re-apply a historical snapshot as the current config. The restore is
        itself appended as a new version (change_action="restored"), so history
        stays append-only and the rollback is auditable. Viewing history already
        requires the manage permission, so the same check authorizes restore."""
        target = self.get_version(config_type, config_key, version, current_user)
        snapshot = target.snapshot or {}
        tenant_id = current_user["tenant_id"]

        if config_type == TYPE_APPROVAL_POLICY:
            new_snapshot = self._restore_policy(config_key, snapshot, tenant_id)
        elif config_type == TYPE_AUTOPILOT:
            new_snapshot = self._restore_autopilot(snapshot, tenant_id)
        elif config_type == TYPE_WORKFLOW:
            new_snapshot = self._restore_workflow(config_key, snapshot, tenant_id)
        else:
            raise ValueError(f"Unknown config type: {config_type}")

        row = record_version(
            self.db, tenant_id, config_type, config_key, new_snapshot,
            "restored", current_user["id"],
            change_reason=f"Restored from version {version}",
        )
        self.db.commit()
        self.db.refresh(row)
        return row

    def _restore_policy(self, config_key: str, snapshot: dict, tenant_id) -> dict:
        policy = self.db.query(Policy).filter(Policy.id == UUID(str(config_key))).first()
        if policy is None:
            # The policy was deleted; recreate it under its original id.
            policy = Policy(id=UUID(str(config_key)), tenant_id=tenant_id,
                            policy_type=_APPROVAL_POLICY_TYPE)
            self.db.add(policy)
        policy.policy_name = snapshot.get("policy_name")
        policy.description = snapshot.get("description")
        policy.rule_config = snapshot.get("rule_config") or {}
        policy.applies_to = snapshot.get("applies_to") or _APPROVAL_APPLIES_TO
        policy.is_active = snapshot.get("is_active", True)
        policy.priority = snapshot.get("priority", 0)
        self.db.add(policy)
        self.db.flush()
        return policy_snapshot(policy)

    def _restore_autopilot(self, snapshot: dict, tenant_id) -> dict:
        policy = (
            self.db.query(Policy)
            .filter(
                Policy.tenant_id == tenant_id,
                Policy.policy_type == _AUTOPILOT_POLICY_TYPE,
                Policy.policy_name == _AUTOPILOT_POLICY_NAME,
            )
            .first()
        )
        if policy is None:
            policy = Policy(tenant_id=tenant_id, policy_type=_AUTOPILOT_POLICY_TYPE,
                            policy_name=_AUTOPILOT_POLICY_NAME,
                            description="Restricted Autopilot settings.",
                            applies_to="invoice", priority=0)
            self.db.add(policy)
        policy.rule_config = snapshot or {}
        policy.is_active = True
        self.db.add(policy)
        self.db.flush()
        return dict(policy.rule_config or {})

    def _restore_workflow(self, workflow_type: str, snapshot: dict, tenant_id) -> dict:
        states = (
            self.db.query(WorkflowState)
            .filter(
                WorkflowState.tenant_id == tenant_id,
                WorkflowState.workflow_type == workflow_type,
            )
            .all()
        )
        by_name = {s.state_name: s for s in states}
        for snap in snapshot.get("states", []):
            state = by_name.get(snap.get("state_name"))
            if state is not None:
                state.allowed_transitions = list(snap.get("allowed_transitions") or [])
                state.guards = dict(snap.get("guards") or {})
                self.db.add(state)
        self.db.flush()
        return workflow_snapshot(list(by_name.values()))

    @staticmethod
    def _require_view(config_type: str, current_user: dict) -> None:
        permission = _VIEW_PERMISSION.get(config_type)
        if permission is None:
            raise ValueError(f"Unknown config type: {config_type}")
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                "You do not have permission to view this configuration history"
            )
