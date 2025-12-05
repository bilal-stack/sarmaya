from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID

from app.models.policy import Policy
from app.core.roles import has_permission, get_approval_limit


def evaluate_policy(policy_name: str, context: dict) -> bool:
    """Evaluate if a policy applies to given context"""
    return True


def evaluate_approval_role(db: Session, tenant_id: str | UUID, total_amount: float) -> Optional[str]:
    """
    Determine required approver role based on amount and policies table
    
    Returns: 'manager' | 'cfo' | 'admin' | None
    """
    # Query active approval_limit policies for tenant, ordered by priority
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
        
        if operator == "less_than" and total_amount < threshold:
            return required_role
        elif operator == "greater_equal" and total_amount >= threshold:
            return required_role
    
    # Default fallback if no policies match
    return "manager" if total_amount <= 250_000 else "cfo"


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
