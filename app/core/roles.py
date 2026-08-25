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

# Requisitions and sourcing. Asking for something, choosing who supplies it,
# and committing the company to buy are three different authorities. Anyone may
# raise a requisition — that is the point, it is how a need enters the system —
# but approving one, running a tender and awarding it are each held separately,
# so no single person can carry a purchase from "I want this" to "this vendor
# wins" unobserved.
PERM_CREATE_REQUISITION = "requisitions.create"
PERM_VIEW_REQUISITION = "requisitions.view"
PERM_APPROVE_REQUISITION = "requisitions.approve"
PERM_MANAGE_SOURCING = "sourcing.manage"      # run an RFQ, capture quotes
PERM_AWARD_SOURCING = "sourcing.award"        # pick the winner

# Purchase orders. Separate from invoice permissions on purpose: buying and
# paying are different authorities, and letting one imply the other would
# undo segregation of duties across the two modules.
PERM_CREATE_PO = "purchase_orders.create"
PERM_VIEW_PO = "purchase_orders.view"
PERM_UPDATE_PO = "purchase_orders.update"
PERM_APPROVE_PO = "purchase_orders.approve"
PERM_RECEIVE_GOODS = "purchase_orders.receive"

# Payments. Preparing and releasing are separate permissions because a single
# person holding both defeats maker-checker — the control that matters most at
# the step where money actually leaves.
PERM_VIEW_PAYMENT = "payments.view"
PERM_PREPARE_PAYMENT = "payments.prepare"
PERM_RELEASE_PAYMENT = "payments.release"

# Bank statements. Reconciliation is a control over payments, not a part of
# them: whoever confirms that money cleared correctly is checking the people
# who sent it, so the permission is separate from prepare and release.
PERM_VIEW_BANK_STATEMENT = "bank_statements.view"
PERM_IMPORT_BANK_STATEMENT = "bank_statements.import"
PERM_RECONCILE_PAYMENT = "bank_statements.reconcile"

PERM_MANAGE_VENDORS = "vendors.manage"
# Approving a change of a vendor's bank account is separate from managing
# vendors, because whoever maintains vendor records is exactly who would
# make this change — and the control is that somebody else agrees to it.
PERM_APPROVE_BANK_CHANGE = "vendors.approve_bank_change"
PERM_VIEW_VENDORS = "vendors.view"
# Reading a vendor's account number in full is separate from reading the vendor.
# Five roles hold vendors.view, including the read-only auditor, and the full
# IBAN is exactly the reconnaissance a payment redirection needs — so the
# credential follows "do you act on payment details", not "may you see vendors".
PERM_VIEW_BANK_DETAILS = "vendors.view_bank_details"

PERM_MANAGE_USERS = "users.manage"
PERM_VIEW_USERS = "users.view"

PERM_VIEW_AUDIT = "audit.view"
# The Build Book's "watchlist role": who is told, in real time, when a vendor's
# bank details move, master data is edited, or an approval policy changes.
# Oversight rather than operations — the people who hold it are deliberately not
# the ones making these changes, or the alert would be addressed to its author.
PERM_RECEIVE_WATCHLIST = "watchlist.receive"
PERM_VIEW_WATCHLIST = "watchlist.view"
PERM_MANAGE_POLICIES = "policies.manage"

PERM_MANAGE_WORKFLOW = "workflow.manage"

# Inventory (Build Book Variant D1). Receiving already had its own permission
# on the purchase order; these cover what happens to goods once they are here.
#
# `inventory.adjust` and `inventory.approve_adjustment` are deliberately
# separate, and separately granted: an adjustment is the only way stock changes
# with nothing physical behind it, which makes it how a theft is covered up.
# The person who counts the shelf must not be the person who signs off the
# write-off, and one permission covering both would make that impossible to
# enforce however the roles were arranged.
# HR (Build Book Variant C). The splits here are the Build Book's "SoD for HR
# actions and payroll approvals", and each exists because the two halves must
# be able to sit with different people:
#
#   * `view` vs `view_compensation` — most of HR is ordinary directory data.
#     Salary, national ID and bank details are not, and an HR list that renders
#     them to whoever opens it is a data breach with a UI.
#   * `request_payroll_change` vs `approve_payroll_change` — raising your own
#     rise is the obvious failure; a manager approving a rise for the person
#     who approves theirs is the subtler one, and only separate permissions
#     make either preventable.
#   * `approve_expense` is separate again, because a claim is a payment
#     request with a person attached and the claimant must never sign it.
PERM_VIEW_HR = "hr.view"
PERM_MANAGE_EMPLOYEES = "hr.manage_employees"
PERM_VIEW_COMPENSATION = "hr.view_compensation"
PERM_REQUEST_HEADCOUNT = "hr.request_headcount"
PERM_APPROVE_HEADCOUNT = "hr.approve_headcount"
PERM_REQUEST_PAYROLL_CHANGE = "hr.request_payroll_change"
PERM_APPROVE_PAYROLL_CHANGE = "hr.approve_payroll_change"
PERM_MANAGE_ONBOARDING = "hr.manage_onboarding"
PERM_CLAIM_EXPENSE = "hr.claim_expense"
PERM_APPROVE_EXPENSE = "hr.approve_expense"

PERM_VIEW_INVENTORY = "inventory.view"
PERM_MANAGE_ITEMS = "inventory.manage_items"
PERM_ADJUST_INVENTORY = "inventory.adjust"
PERM_APPROVE_ADJUSTMENT = "inventory.approve_adjustment"
PERM_INSPECT_GOODS = "inventory.inspect"
PERM_MANAGE_RETURNS = "inventory.manage_returns"
PERM_APPROVE_RETURN = "inventory.approve_return"

# Integration Hub. Connecting or disconnecting touches OAuth credentials for
# the tenant's own external accounting system, so this stays admin-only —
# view is a separate, wider grant for the roles that already read financial
# reconciliation (CFO, Auditor), matching how bank-statement reconciliation
# and hr.view_compensation are already split between "reads" and "acts".
PERM_VIEW_INTEGRATIONS = "integrations.view"
PERM_MANAGE_INTEGRATIONS = "integrations.manage"

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
        # Requisitions and sourcing - ALL
        PERM_CREATE_REQUISITION,
        PERM_VIEW_REQUISITION,
        PERM_APPROVE_REQUISITION,
        PERM_MANAGE_SOURCING,
        PERM_AWARD_SOURCING,
        # Purchase orders - ALL
        PERM_CREATE_PO,
        PERM_VIEW_PO,
        PERM_UPDATE_PO,
        PERM_APPROVE_PO,
        PERM_RECEIVE_GOODS,
        PERM_VIEW_HR,
        PERM_MANAGE_EMPLOYEES,
        PERM_VIEW_COMPENSATION,
        PERM_REQUEST_HEADCOUNT,
        PERM_APPROVE_HEADCOUNT,
        PERM_REQUEST_PAYROLL_CHANGE,
        PERM_APPROVE_PAYROLL_CHANGE,
        PERM_MANAGE_ONBOARDING,
        PERM_CLAIM_EXPENSE,
        PERM_APPROVE_EXPENSE,
        PERM_VIEW_INVENTORY,
        PERM_MANAGE_ITEMS,
        PERM_ADJUST_INVENTORY,
        PERM_APPROVE_ADJUSTMENT,
        PERM_INSPECT_GOODS,
        PERM_MANAGE_RETURNS,
        PERM_APPROVE_RETURN,
        PERM_VIEW_INTEGRATIONS,
        PERM_MANAGE_INTEGRATIONS,
        # Payments - ALL (self-release is still refused by SoD)
        PERM_VIEW_PAYMENT,
        PERM_PREPARE_PAYMENT,
        PERM_RELEASE_PAYMENT,
        # Bank statements - ALL (self-reconciliation is still refused by SoD)
        PERM_VIEW_BANK_STATEMENT,
        PERM_IMPORT_BANK_STATEMENT,
        PERM_RECONCILE_PAYMENT,
        # Vendors - ALL (self-approval is still refused by SoD)
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
        PERM_APPROVE_BANK_CHANGE,
        PERM_VIEW_BANK_DETAILS,
        # Users - ALL
        PERM_MANAGE_USERS,
        PERM_VIEW_USERS,
        # Audit - ALL
        PERM_VIEW_AUDIT,
        PERM_RECEIVE_WATCHLIST,
        PERM_VIEW_WATCHLIST,
        # Policies - ALL
        PERM_MANAGE_POLICIES,
        # Workflow - ALL
        PERM_MANAGE_WORKFLOW,
    ],
    AP_CLERK: [
        PERM_CREATE_INVOICE,
        PERM_VIEW_INVOICE,
        PERM_UPDATE_INVOICE,
        # Raises the request and runs the tender, but neither approves the
        # need nor picks the winner — the buyer who collects the quotes must
        # not also be the one who decides which of them wins.
        PERM_CREATE_REQUISITION,
        PERM_VIEW_REQUISITION,
        PERM_MANAGE_SOURCING,
        # Raises orders and records what arrives, but never approves the spend.
        PERM_CREATE_PO,
        PERM_VIEW_PO,
        PERM_UPDATE_PO,
        PERM_RECEIVE_GOODS,
        PERM_VIEW_PAYMENT,
        PERM_PREPARE_PAYMENT,
        # Gets the file from the bank and matches it. Confirming a run they
        # prepared is allowed — that work was already checked at release — but
        # SoD still refuses anyone reconciling a run they released themselves.
        PERM_VIEW_BANK_STATEMENT,
        PERM_IMPORT_BANK_STATEMENT,
        PERM_RECONCILE_PAYMENT,
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
        PERM_VIEW_BANK_DETAILS,
        # Does the physical work and records it. Notably absent:
        # approve_adjustment and approve_return. The person who counts the
        # shelf must not be the person who signs off the write-off.
        PERM_VIEW_INVENTORY,
        PERM_MANAGE_ITEMS,
        PERM_ADJUST_INVENTORY,
        PERM_INSPECT_GOODS,
        PERM_MANAGE_RETURNS,
        # Claims expenses like any employee. Deliberately no hr.view: an AP
        # clerk has no business reading the staff directory to do their job.
        PERM_CLAIM_EXPENSE,
    ],
    MANAGER: [
        PERM_VIEW_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        # Approves the need and picks the winner; does not run the tender.
        PERM_CREATE_REQUISITION,
        PERM_VIEW_REQUISITION,
        PERM_APPROVE_REQUISITION,
        PERM_AWARD_SOURCING,
        PERM_VIEW_PO,
        PERM_APPROVE_PO,
        PERM_MANAGE_VENDORS,
        PERM_VIEW_VENDORS,
        PERM_APPROVE_BANK_CHANGE,
        PERM_VIEW_BANK_DETAILS,
        # The checker side of inventory: approves what the clerk raised, and
        # cannot raise it themselves without also being able to approve it,
        # which is what the SoD rule catches at the moment of approval.
        PERM_VIEW_INVENTORY,
        PERM_APPROVE_ADJUSTMENT,
        PERM_APPROVE_RETURN,
        # Runs a team: asks for people and for their pay to change, and
        # signs off their expenses. Cannot approve a pay change — that is
        # the separation the Build Book asks for, and a manager approving
        # a rise for whoever approves theirs is exactly what it prevents.
        PERM_VIEW_HR,
        PERM_REQUEST_HEADCOUNT,
        PERM_REQUEST_PAYROLL_CHANGE,
        PERM_MANAGE_ONBOARDING,
        PERM_CLAIM_EXPENSE,
        PERM_APPROVE_EXPENSE,
    ],
    CFO: [
        PERM_VIEW_INVOICE,
        PERM_VIEW_REQUISITION,
        PERM_APPROVE_REQUISITION,
        PERM_AWARD_SOURCING,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        PERM_MARK_PAID_INVOICE,
        PERM_VIEW_PO,
        PERM_APPROVE_PO,
        PERM_VIEW_PAYMENT,
        PERM_RELEASE_PAYMENT,
        # Sees the reconciliation but does not confirm matches: the CFO
        # releases runs, and SoD would refuse those anyway.
        PERM_VIEW_BANK_STATEMENT,
        PERM_VIEW_VENDORS,
        PERM_APPROVE_BANK_CHANGE,
        PERM_VIEW_BANK_DETAILS,
        PERM_VIEW_AUDIT,
        PERM_RECEIVE_WATCHLIST,
        PERM_VIEW_WATCHLIST,
        # Second approval on a large write-off.
        PERM_VIEW_INVENTORY,
        PERM_APPROVE_ADJUSTMENT,
        PERM_APPROVE_RETURN,
        # The money side of HR: approves what a hire and a rise cost, and
        # is one of the two roles trusted with compensation figures.
        PERM_VIEW_HR,
        PERM_VIEW_COMPENSATION,
        PERM_APPROVE_HEADCOUNT,
        PERM_APPROVE_PAYROLL_CHANGE,
        PERM_APPROVE_EXPENSE,
        PERM_CLAIM_EXPENSE,
        # Sees whether the connected accounting system is actually receiving
        # postings; does not connect or disconnect it.
        PERM_VIEW_INTEGRATIONS,
    ],
    APPROVER: [
        PERM_VIEW_INVOICE,
        PERM_APPROVE_INVOICE,
        PERM_REJECT_INVOICE,
        PERM_VIEW_REQUISITION,
        PERM_APPROVE_REQUISITION,
        PERM_VIEW_PO,
        PERM_APPROVE_PO,
    ],
    AUDITOR: [
        PERM_VIEW_INVOICE,
        PERM_VIEW_REQUISITION,
        PERM_VIEW_PO,
        PERM_VIEW_PAYMENT,
        PERM_VIEW_BANK_STATEMENT,
        PERM_VIEW_VENDORS,
        PERM_VIEW_USERS,
        PERM_VIEW_AUDIT,
        PERM_RECEIVE_WATCHLIST,
        PERM_VIEW_WATCHLIST,
        PERM_VIEW_INVENTORY,
        # Reads everything, changes nothing. Compensation included, because
        # payroll variance and ghost-employee checks are audit questions
        # that cannot be asked against masked figures.
        PERM_VIEW_HR,
        PERM_VIEW_COMPENSATION,
        # Whether the client's own ledger is actually receiving postings is
        # an audit-relevant question.
        PERM_VIEW_INTEGRATIONS,
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
