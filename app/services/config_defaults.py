"""Canonical default configuration for a freshly provisioned tenant.

A new tenant has no workflow_states or approval policies, so the engine would
silently fall back to the hardcoded rules. This module is the single source of
truth for the out-of-the-box invoice workflow and approval matrix, and seeds
them so the tenant is configuration-first from day one (and can then edit the
rows without a code deploy).

These mirror migration 008's demo-tenant seed; the migration is a historical
snapshot, this module is what runtime provisioning uses.
"""
from app.core.enums import InvoiceState

DRAFT = InvoiceState.DRAFT.value
VALIDATED = InvoiceState.VALIDATED.value
PENDING = InvoiceState.PENDING_APPROVAL.value
APPROVED = InvoiceState.APPROVED.value
REJECTED = InvoiceState.REJECTED.value
PAID = InvoiceState.PAID.value
CANCELLED = InvoiceState.CANCELLED.value

# (state_name, display_name, order, is_initial, is_final, allowed_transitions, color)
DEFAULT_INVOICE_STATES = [
    (DRAFT, "Draft", 1, True, False, [VALIDATED, CANCELLED], "#gray"),
    (VALIDATED, "Validated", 2, False, False, [PENDING, CANCELLED], "#blue"),
    (PENDING, "Pending Approval", 3, False, False, [APPROVED, REJECTED], "#yellow"),
    (APPROVED, "Approved", 4, False, False, [PAID, CANCELLED], "#green"),
    (REJECTED, "Rejected", 5, False, True, [DRAFT], "#red"),
    (PAID, "Paid", 6, False, True, [], "#purple"),
    (CANCELLED, "Cancelled", 7, False, True, [], "#orange"),
]

# (policy_name, priority, rule_config) — highest priority matching rule wins.
DEFAULT_APPROVAL_POLICIES = [
    (
        "CFO approval over 250k",
        100,
        {"amount_threshold": 250_000, "operator": "greater_than", "required_role": "cfo"},
    ),
    (
        "Manager approval up to 250k",
        0,
        {"amount_threshold": 0, "operator": "greater_equal", "required_role": "manager"},
    ),
]
