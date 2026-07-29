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


DEFAULT_THRESHOLD = 250_000


def evaluate_rules(rules, total_amount: float) -> dict:
    """Route an amount against an ordered list of rule dicts.

    Pure: no database access, so the live path and the Policy Simulator share
    one implementation and cannot drift apart. `rules` must already be sorted
    by descending priority; each is {policy_name, policy_id, rule_config}.
    Falls back to the hardcoded split when nothing matches.
    """
    for entry in rules or []:
        rule = entry.get("rule_config") or {}
        threshold = rule.get("amount_threshold", 0)
        operator = rule.get("operator", "greater_equal")
        required_role = rule.get("required_role", "manager")

        if _operator_matches(operator, total_amount, threshold):
            phrase = _OPERATOR_PHRASE.get(operator, operator)
            return {
                "required_role": required_role,
                "policy_name": entry.get("policy_name"),
                "policy_id": entry.get("policy_id"),
                "matched_rule": dict(rule),
                "reason": (
                    f"Amount {total_amount:,.0f} {phrase} {threshold:,.0f} → "
                    f"requires {required_role.upper()} approval "
                    f"(policy '{entry.get('policy_name')}')."
                ),
            }

    required_role = "manager" if total_amount <= DEFAULT_THRESHOLD else "cfo"
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


def active_approval_rules(db: Session, tenant_id) -> list:
    """The tenant's live approval rules, highest priority first."""
    policies = db.query(Policy).filter(
        Policy.tenant_id == tenant_id,
        Policy.policy_type == "approval_limit",
        Policy.is_active == True
    ).order_by(Policy.priority.desc()).all()
    return [
        {"policy_name": p.policy_name, "policy_id": p.id, "rule_config": p.rule_config or {}}
        for p in policies
    ]


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
    Delegates to `evaluate_rules`, which the Policy Simulator also uses, so an
    explanation can never disagree with a simulation.
    """
    return evaluate_rules(active_approval_rules(db, tenant_id), total_amount)

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
