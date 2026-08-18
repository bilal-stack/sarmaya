"""Canonical default configuration for a freshly provisioned tenant.

A new tenant has no workflow_states or approval policies, so the engine would
silently fall back to the hardcoded rules. This module is the single source of
truth for the out-of-the-box invoice workflow and approval matrix, and seeds
them so the tenant is configuration-first from day one (and can then edit the
rows without a code deploy).

These mirror migration 008's demo-tenant seed; the migration is a historical
snapshot, this module is what runtime provisioning uses.
"""
from app.core.enums import (
    InvoiceState, RequisitionState, RFQState, PurchaseOrderState, PaymentState,
)

DRAFT = InvoiceState.DRAFT.value
VALIDATED = InvoiceState.VALIDATED.value
PENDING = InvoiceState.PENDING_APPROVAL.value
APPROVED = InvoiceState.APPROVED.value
REJECTED = InvoiceState.REJECTED.value
PAID = InvoiceState.PAID.value
CANCELLED = InvoiceState.CANCELLED.value

# (state_name, display_name, order, is_initial, is_final, allowed_transitions, color, guards, sla)
# guards: {target_state: [guard_name, ...]} resolved by app/services/workflow_guards.py
# sla:    {"hours": n, "escalate_to": role} — timer starts on state entry; empty = no SLA
DEFAULT_INVOICE_STATES = [
    (DRAFT, "Draft", 1, True, False, [VALIDATED, CANCELLED], "#gray",
        {VALIDATED: ["required_fields_present"]}, {}),
    (VALIDATED, "Validated", 2, False, False, [PENDING, CANCELLED], "#blue", {}, {}),
    (PENDING, "Pending Approval", 3, False, False, [APPROVED, REJECTED], "#yellow",
        {APPROVED: ["vendor_active", "duplicate_resolved"]},
        {"hours": 48, "escalate_to": "cfo"}),
    (APPROVED, "Approved", 4, False, False, [PAID, CANCELLED], "#green", {}, {}),
    (REJECTED, "Rejected", 5, False, True, [DRAFT], "#red", {}, {}),
    (PAID, "Paid", 6, False, True, [], "#purple", {}, {}),
    (CANCELLED, "Cancelled", 7, False, True, [], "#orange", {}, {}),
]

PO_DRAFT = PurchaseOrderState.DRAFT.value
PO_PENDING = PurchaseOrderState.PENDING_APPROVAL.value
PO_APPROVED = PurchaseOrderState.APPROVED.value
PO_ISSUED = PurchaseOrderState.ISSUED.value
PO_REJECTED = PurchaseOrderState.REJECTED.value
PO_CLOSED = PurchaseOrderState.CLOSED.value
PO_CANCELLED = PurchaseOrderState.CANCELLED.value

# The purchase order workflow, same tuple shape as the invoice one.
#
# A PO commits the company to spend, so approval comes before it is issued to
# the vendor. `issued` is the point of no return — once the vendor has it, goods
# may arrive — which is why the guard on that transition checks the vendor is
# verified: the invoice-side check comes too late to prevent an order being
# placed with an unverified party.
REQ_DRAFT = RequisitionState.DRAFT.value
REQ_PENDING = RequisitionState.PENDING_APPROVAL.value
REQ_APPROVED = RequisitionState.APPROVED.value
REQ_CONVERTED = RequisitionState.CONVERTED.value
REQ_REJECTED = RequisitionState.REJECTED.value
REQ_CANCELLED = RequisitionState.CANCELLED.value

# A requisition is the first record in the chain, so its approval is what every
# later control is protecting. `converted` is terminal: once an order exists
# against it the request has been acted on, and re-using it would let one
# approval cover two orders.
DEFAULT_REQUISITION_STATES = [
    (REQ_DRAFT, "Draft", 1, True, False, [REQ_PENDING, REQ_CANCELLED], "#gray",
        {REQ_PENDING: ["requisition_has_lines", "requisition_justified"]}, {}),
    (REQ_PENDING, "Pending Approval", 2, False, False, [REQ_APPROVED, REQ_REJECTED], "#yellow",
        {}, {"hours": 24, "escalate_to": "manager"}),
    (REQ_APPROVED, "Approved", 3, False, False, [REQ_CONVERTED, REQ_CANCELLED], "#green",
        {}, {}),
    (REQ_CONVERTED, "Converted to Order", 4, False, True, [], "#blue", {}, {}),
    (REQ_REJECTED, "Rejected", 5, False, True, [REQ_DRAFT], "#red", {}, {}),
    (REQ_CANCELLED, "Cancelled", 6, False, True, [], "#orange", {}, {}),
]

RFQ_DRAFT = RFQState.DRAFT.value
RFQ_ISSUED = RFQState.ISSUED.value
RFQ_CLOSED = RFQState.CLOSED.value
RFQ_AWARDED = RFQState.AWARDED.value
RFQ_CANCELLED = RFQState.CANCELLED.value

# Closing is the point of no return for the bidders: no quote may be added or
# altered afterwards, so an RFQ cannot be issued without vendors to ask, and
# cannot be awarded without having closed.
DEFAULT_RFQ_STATES = [
    (RFQ_DRAFT, "Draft", 1, True, False, [RFQ_ISSUED, RFQ_CANCELLED], "#gray",
        {RFQ_ISSUED: ["rfq_has_invited_vendors"]}, {}),
    (RFQ_ISSUED, "Issued", 2, False, False, [RFQ_CLOSED, RFQ_CANCELLED], "#yellow", {}, {}),
    # The only state in this workflow with a deadline, and the one that needed
    # it: quoting has ended, the vendors are waiting on an answer, and nothing
    # else chases it. Every other workflow had an SLA on its waiting state while
    # a closed tender could sit unawarded indefinitely without ever becoming
    # overdue — invisible to the escalation runner and to the "overdue only"
    # view, because a timer that does not exist never breaches.
    (RFQ_CLOSED, "Closed", 3, False, False, [RFQ_AWARDED, RFQ_CANCELLED], "#blue",
        {RFQ_AWARDED: ["rfq_has_quotes"]},
        {"hours": 48, "escalate_to": "manager"}),
    (RFQ_AWARDED, "Awarded", 4, False, True, [], "#green", {}, {}),
    (RFQ_CANCELLED, "Cancelled", 5, False, True, [], "#orange", {}, {}),
]

DEFAULT_PURCHASE_ORDER_STATES = [
    (PO_DRAFT, "Draft", 1, True, False, [PO_PENDING, PO_CANCELLED], "#gray",
        {PO_PENDING: ["po_has_lines"]}, {}),
    (PO_PENDING, "Pending Approval", 2, False, False, [PO_APPROVED, PO_REJECTED], "#yellow",
        {}, {"hours": 24, "escalate_to": "cfo"}),
    (PO_APPROVED, "Approved", 3, False, False, [PO_ISSUED, PO_CANCELLED], "#green",
        {PO_ISSUED: ["vendor_active"]}, {}),
    (PO_ISSUED, "Issued", 4, False, False, [PO_CLOSED, PO_CANCELLED], "#blue", {}, {}),
    (PO_REJECTED, "Rejected", 5, False, True, [PO_DRAFT], "#red", {}, {}),
    (PO_CLOSED, "Closed", 6, False, True, [], "#purple", {}, {}),
    (PO_CANCELLED, "Cancelled", 7, False, True, [], "#orange", {}, {}),
]

PAY_DRAFT = PaymentState.DRAFT.value
PAY_PENDING = PaymentState.PENDING_RELEASE.value
PAY_RELEASED = PaymentState.RELEASED.value
PAY_REJECTED = PaymentState.REJECTED.value
PAY_CANCELLED = PaymentState.CANCELLED.value

# The payment workflow. Short on purpose: a run is prepared, then released by
# someone else, and release is terminal — the instruction has been authorised
# and the invoices are settled, so there is nothing to edit afterwards.
#
# The guard on release is the one that matters: it re-checks that every line is
# still an approved, unpaid invoice against an active vendor. A run can sit
# pending for days, and the world can change under it.
DEFAULT_PAYMENT_STATES = [
    (PAY_DRAFT, "Draft", 1, True, False, [PAY_PENDING, PAY_CANCELLED], "#gray",
        {PAY_PENDING: ["payment_has_lines"]}, {}),
    (PAY_PENDING, "Pending Release", 2, False, False, [PAY_RELEASED, PAY_REJECTED], "#yellow",
        {PAY_RELEASED: ["payment_lines_still_payable"]},
        {"hours": 24, "escalate_to": "cfo"}),
    (PAY_RELEASED, "Released", 3, False, True, [], "#green", {}, {}),
    (PAY_REJECTED, "Rejected", 4, False, True, [PAY_DRAFT], "#red", {}, {}),
    (PAY_CANCELLED, "Cancelled", 5, False, True, [], "#orange", {}, {}),
]

#: {workflow_type: states} — provisioning seeds each independently, so a tenant
#: created before a workflow existed gains it on the next run.
DEFAULT_WORKFLOWS = {
    "requisition": DEFAULT_REQUISITION_STATES,
    "rfq": DEFAULT_RFQ_STATES,
    "invoice": DEFAULT_INVOICE_STATES,
    "purchase_order": DEFAULT_PURCHASE_ORDER_STATES,
    "payment": DEFAULT_PAYMENT_STATES,
}

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
