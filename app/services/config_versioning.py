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

    @staticmethod
    def _require_view(config_type: str, current_user: dict) -> None:
        permission = _VIEW_PERMISSION.get(config_type)
        if permission is None:
            raise ValueError(f"Unknown config type: {config_type}")
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                "You do not have permission to view this configuration history"
            )
