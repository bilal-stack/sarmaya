from app.core.enums import UserRole

# Canonical roles and simple permission/limit mappings for the MVP
ADMIN = "admin"
AP_CLERK = "ap_clerk"
MANAGER = "manager"
CFO = "cfo"
APPROVER = "approver"  # generic approver role (can map to manager/CFO)
AUDITOR = "auditor"
SYSTEM = "system"

# All allowed roles (for validation)
ALL_ROLES = [ADMIN, AP_CLERK, MANAGER, CFO, APPROVER, AUDITOR, SYSTEM]

# Simple permission keys -- use these in policies/permission checks
PERM_CREATE_INVOICE = "invoices.create"
PERM_VIEW_INVOICE = "invoices.view"
PERM_UPDATE_INVOICE = "invoices.update"
PERM_DELETE_INVOICE = "invoices.delete"
PERM_APPROVE_INVOICE = "invoices.approve"
PERM_REJECT_INVOICE = "invoices.reject"
PERM_MARK_PAID_INVOICE = "invoices.mark_paid"

PERM_MANAGE_VENDORS = "vendors.manage"
PERM_VIEW_VENDORS = "vendors.view"

PERM_MANAGE_USERS = "users.manage"
PERM_VIEW_USERS = "users.view"

PERM_VIEW_AUDIT = "audit.view"
PERM_MANAGE_POLICIES = "policies.manage"

PERM_MANAGE_WORKFLOW = "workflow.manage"

# Role -> permission keys (admin has ALL permissions)
ROLE_PERMISSIONS = {
    ADMIN: [
        # Invoices - ALL
        PERM_CREATE_INVOICE,
        PERM_VIEW_INVOICE,
        PERM_UPDATE_INVOICE,
        PERM_DELETE_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        PERM_MARK_PAID_INVOICE,
        # Vendors - ALL
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
        # Users - ALL
        PERM_MANAGE_USERS,
        PERM_VIEW_USERS,
        # Audit - ALL
        PERM_VIEW_AUDIT,
        # Policies - ALL
        PERM_MANAGE_POLICIES,
        # Workflow - ALL
        PERM_MANAGE_WORKFLOW,
    ],
    AP_CLERK: [
        PERM_CREATE_INVOICE,
        PERM_VIEW_INVOICE,
        PERM_UPDATE_INVOICE,
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
    ],
    MANAGER: [
        PERM_VIEW_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
    ],
    CFO: [
        PERM_VIEW_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        PERM_MARK_PAID_INVOICE,
        PERM_VIEW_VENDORS,
        PERM_VIEW_AUDIT,
    ],
    APPROVER: [
        PERM_VIEW_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
    ],
    AUDITOR: [
        PERM_VIEW_INVOICE,
        PERM_VIEW_VENDORS,
        PERM_VIEW_USERS,
        PERM_VIEW_AUDIT,
    ],
    SYSTEM: [
        PERM_VIEW_INVOICE,
    ],
}

# Approval limits (currency amounts in smallest unit or base currency)
# Use these in policy evaluation: Manager approves <= 250k, CFO > 250k
APPROVAL_LIMITS = {
    ADMIN: None,  # Unlimited
    MANAGER: 250_000,
    CFO: None,  # Unlimited
}

# Canonical roles (extend anytime)
ROLES = {
    "admin": {
        "display_name": "Administrator",
        "description": "Full system access - all permissions",
        "permissions": ["*"],  # Wildcard = all
    },
    "ap_clerk": {
        "display_name": "AP Clerk",
        "description": "Create and manage invoices and vendors",
        "permissions": ["invoices.create", "invoices.view", "invoices.update", "vendors.manage", "vendors.view"],
    },
    "manager": {
        "display_name": "Manager",
        "description": "Approve invoices up to 250k; manage vendors",
        "permissions": ["invoices.view", "invoices.approve", "invoices.reject", "vendors.manage", "vendors.view"],
    },
    "cfo": {
        "display_name": "CFO",
        "description": "Approve all invoices and view financials",
        "permissions": ["invoices.view", "invoices.approve", "invoices.reject", "invoices.mark_paid", "audit.view"],
    },
    "approver": {
        "display_name": "Approver",
        "description": "Generic approver role",
        "permissions": ["invoices.view", "invoices.approve", "invoices.reject"],
    },
    "auditor": {
        "display_name": "Auditor",
        "description": "Read-only access to all audit logs",
        "permissions": ["invoices.view", "vendors.view", "users.view", "audit.view"],
    },
    "user": {
        "display_name": "User",
        "description": "Basic read access",
        "permissions": ["invoices.view"],
    },
    "system": {
        "display_name": "System",
        "description": "System-level operations",
        "permissions": [],
    },
}

DEFAULT_ROLE = UserRole.AP_CLERK.value

def is_valid_role(role: str) -> bool:
    return role in [r.value for r in UserRole]

def list_roles() -> list[str]:
    return [r.value for r in UserRole]

def _normalize_role(role: str) -> str:
    """Roles are stored/compared as their lowercase enum values. Normalize
    defensively so permission checks never hinge on the casing of the role
    string carried in a token or passed by a caller."""
    return (role or "").strip().lower()


def get_role_permissions(role: str) -> list[str]:
    """Get all permissions for a given role"""
    return ROLE_PERMISSIONS.get(_normalize_role(role), [])

def has_permission(role: str, permission: str) -> bool:
    """Check if role has specific permission"""
    role = _normalize_role(role)
    if role == ADMIN:
        return True  # Admin has all permissions
    return permission in ROLE_PERMISSIONS.get(role, [])

def get_approval_limit(role: str) -> int | None:
    """Get approval limit for role (None = unlimited, 0 = no approval permission)"""
    role = _normalize_role(role)
    if role == ADMIN:
        return None  # Unlimited

    # Only roles with explicit approval permission should have limits
    if role not in APPROVAL_LIMITS:
        return 0  # No approval permission

    return APPROVAL_LIMITS.get(role)

def can_approve_invoices(role: str) -> bool:
    """Check if role has invoice approval permission"""
    return has_permission(role, PERM_APPROVE_INVOICE)

def can_approve_amount(role: str, amount: float) -> tuple[bool, str]:
    """
    Check if role can approve given amount
    Returns (can_approve, error_message)
    """
    # First check if role has approval permission at all
    if not can_approve_invoices(role):
        return False, f"Role '{role}' does not have permission to approve invoices"
    
    # Then check approval limit
    limit = get_approval_limit(role)
    
    if limit is None:  # Unlimited
        return True, ""
    
    if limit == 0:  # No approval permission
        return False, f"Role '{role}' does not have permission to approve invoices"
    
    if amount > limit:
        return False, f"Role '{role}' can only approve invoices up to {limit}, but invoice amount is {amount}"
    
    return True, ""
