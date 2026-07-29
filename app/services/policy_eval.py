"""Recording and reading policy-evaluation snapshots.

`record_approval_routing_eval` is called wherever approval routing is decided,
capturing the rule that matched, its config version, the inputs, and the
decision — so the routing can be reproduced later even if the policy has since
been edited, rolled back, or deleted (Build Book: policy evaluation snapshots).
"""
import logging
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.policy_eval import PolicyEval
from app.models.config_version import ConfigVersion
from app.services.config_versioning import TYPE_APPROVAL_POLICY
from app.services.correlation import resolve_correlation_id
from app.core.roles import has_permission, PERM_VIEW_AUDIT, PERM_MANAGE_POLICIES

logger = logging.getLogger(__name__)

POLICY_KEY_APPROVAL = "finance.approval.thresholds"


def _current_policy_version(db: Session, tenant_id, policy_id) -> Optional[int]:
    """The config_versions number for this policy right now, so the snapshot
    points at a real, restorable version of the rule."""
    if not policy_id:
        return None
    return (
        db.query(func.max(ConfigVersion.version))
        .filter(
            ConfigVersion.tenant_id == tenant_id,
            ConfigVersion.config_type == TYPE_APPROVAL_POLICY,
            ConfigVersion.config_key == str(policy_id),
        )
        .scalar()
    )


def record_approval_routing_eval(
    db: Session,
    tenant_id,
    routing: dict,
    amount: float,
    object_type: str,
    object_id,
    evaluated_by,
) -> Optional[PolicyEval]:
    """Persist one approval-routing decision. Best-effort: a failure here must
    never block the workflow action it describes."""
    try:
        policy_id = routing.get("policy_id")
        row = PolicyEval(
            tenant_id=tenant_id,
            policy_key=POLICY_KEY_APPROVAL,
            policy_id=policy_id,
            policy_name=routing.get("policy_name"),
            policy_version=_current_policy_version(db, tenant_id, policy_id),
            inputs={"amount": float(amount or 0), "matched_rule": routing.get("matched_rule")},
            output={"required_role": routing.get("required_role")},
            reasons=[routing.get("reason")] if routing.get("reason") else [],
            correlation_id=resolve_correlation_id(db, object_type, object_id),
            object_type=object_type,
            object_id=object_id,
            evaluated_by=evaluated_by,
        )
        db.add(row)
        db.flush()
        return row
    except Exception:
        logger.exception("Failed to record policy evaluation for %s %s", object_type, object_id)
        return None


class PolicyEvalService:
    """Read access to policy-evaluation snapshots."""

    def __init__(self, db: Session):
        self.db = db

    def list_evals(
        self,
        current_user: dict,
        object_type: Optional[str] = None,
        object_id=None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[PolicyEval], int]:
        role = current_user["role"]
        if not (has_permission(role, PERM_VIEW_AUDIT) or has_permission(role, PERM_MANAGE_POLICIES)):
            raise PermissionError("You do not have permission to view policy evaluations")

        query = self.db.query(PolicyEval)
        if object_type:
            query = query.filter(PolicyEval.object_type == object_type)
        if object_id:
            query = query.filter(PolicyEval.object_id == object_id)

        total = query.count()
        rows = (
            query.order_by(PolicyEval.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total
