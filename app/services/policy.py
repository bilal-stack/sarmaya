from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID

from app.models.policy import Policy
from app.core.roles import has_permission, get_approval_limit


def evaluate_policy(policy_name: str, context: dict) -> bool:
    """Evaluate if a policy applies to given context"""
    return True


def _operator_matches(operator: str, amount: float, threshold: float) -> bool:
    """Compare an invoice amount against a policy threshold.

    Supported operators let an approval matrix be expressed entirely as config
    rows (e.g. cfo for ``greater_than`` 250k, manager for ``greater_equal`` 0).
    An unknown operator never matches, so a malformed rule fails closed.
    """
    if operator == "greater_than":
        return amount > threshold
    if operator == "greater_equal":
        return amount >= threshold
    if operator == "less_than":
        return amount < threshold
    if operator == "less_equal":
        return amount <= threshold
    if operator == "equal":
        return amount == threshold
    return False


def evaluate_approval_role(db: Session, tenant_id: str | UUID, total_amount: float) -> Optional[str]:
    """
    Determine required approver role based on amount and the policies table.

    The approval matrix is configuration-first: active ``approval_limit``
    policies are evaluated in descending priority and the first matching rule
    wins. The hardcoded split is only a fallback for tenants with no rules.

    Returns: 'manager' | 'cfo' | 'admin' | None
    """
    policies = db.query(Policy).filter(
        Policy.tenant_id == tenant_id,
        Policy.policy_type == "approval_limit",
        Policy.is_active == True
    ).order_by(Policy.priority.desc()).all()

    for policy in policies:
        rule = policy.rule_config or {}
        threshold = rule.get("amount_threshold", 0)
        operator = rule.get("operator", "greater_equal")
        required_role = rule.get("required_role", "manager")

        if _operator_matches(operator, total_amount, threshold):
            return required_role

    # Default fallback if no policies are configured for this tenant.
    return "manager" if total_amount <= 250_000 else "cfo"


_OPERATOR_PHRASE = {
    "greater_than": "exceeds",
    "greater_equal": "is at least",
    "less_than": "is below",
    "less_equal": "is at most",
    "equal": "equals",
}


def explain_approval_routing(db: Session, tenant_id: str | UUID, total_amount: float) -> dict:
    """Explain *why* an amount routes to a given approver — the policy reason
    surfaced in Live Audit Mode.

    Returns {required_role, reason, policy_name, policy_id, matched_rule}.
    Mirrors evaluate_approval_role so the explanation always matches the
    decision. policy_id/matched_rule let callers record a PolicyEval snapshot
    (see app/services/policy_eval.py); they are None when no configured rule
    matched and the hardcoded default applied.
    """
    policies = db.query(Policy).filter(
        Policy.tenant_id == tenant_id,
        Policy.policy_type == "approval_limit",
        Policy.is_active == True
    ).order_by(Policy.priority.desc()).all()

    for policy in policies:
        rule = policy.rule_config or {}
        threshold = rule.get("amount_threshold", 0)
        operator = rule.get("operator", "greater_equal")
        required_role = rule.get("required_role", "manager")

        if _operator_matches(operator, total_amount, threshold):
            phrase = _OPERATOR_PHRASE.get(operator, operator)
            return {
                "required_role": required_role,
                "policy_name": policy.policy_name,
                "policy_id": policy.id,
                "matched_rule": dict(rule),
                "reason": (
                    f"Amount {total_amount:,.0f} {phrase} {threshold:,.0f} → "
                    f"requires {required_role.upper()} approval "
                    f"(policy '{policy.policy_name}')."
                ),
            }

    required_role = "manager" if total_amount <= 250_000 else "cfo"
    return {
        "required_role": required_role,
        "policy_name": None,
        "policy_id": None,
        "matched_rule": None,
        "reason": (
            f"No approval policy configured; default routing sends "
            f"{total_amount:,.0f} to {required_role.upper()}."
        ),
    }


def check_user_can_approve(user_role: str, total_amount: float) -> bool:
    """
    Check if user role has permission to approve invoice of given amount
    
    Admin can approve any amount
    Manager can approve <= 250k
    CFO can approve any amount
    """
    if not has_permission(user_role, "invoices.approve"):
        return False
    
    approval_limit = get_approval_limit(user_role)
    
    # None = unlimited
    if approval_limit is None:
        return True
    
    # Check against limit
    return total_amount <= approval_limit


def check_user_can_reject(user_role: str) -> bool:
    """Check if user role can reject invoices"""
    return has_permission(user_role, "invoices.reject")


def check_user_can_view(user_role: str) -> bool:
    """Check if user role can view invoices"""
    return has_permission(user_role, "invoices.view")
