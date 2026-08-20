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

# --- Variant D: inventory ---------------------------------------------------
from app.models.inventory_control import (  # noqa: E402
    ADJ_DRAFT, ADJ_PENDING_APPROVAL, ADJ_APPROVED, ADJ_POSTED, ADJ_REJECTED,
    ADJ_CANCELLED, RET_DRAFT, RET_PENDING_APPROVAL, RET_APPROVED,
    RET_DISPATCHED, RET_CREDITED, RET_REJECTED, RET_CANCELLED,
)

# `posted` is where an adjustment ends, and it is separate from `approved`
# because approving is a decision while posting is what moves the ledger. An
# approval that failed to post shows up as a record stuck in `approved` rather
# than as stock that silently never changed.
#
# The SLA sits on `pending_approval` at 24 hours: an unapproved write-off is a
# discrepancy nobody has accounted for, and the longer it waits the harder the
# count is to reconstruct.
DEFAULT_INVENTORY_ADJUSTMENT_STATES = [
    (ADJ_DRAFT, "Draft", 1, True, False, [ADJ_PENDING_APPROVAL, ADJ_CANCELLED],
        "#gray", {}, {}),
    (ADJ_PENDING_APPROVAL, "Pending Approval", 2, False, False,
        [ADJ_APPROVED, ADJ_REJECTED], "#yellow", {},
        {"hours": 24, "escalate_to": "manager"}),
    (ADJ_APPROVED, "Approved", 3, False, False, [ADJ_POSTED], "#blue", {}, {}),
    (ADJ_POSTED, "Posted", 4, False, True, [], "#green", {}, {}),
    (ADJ_REJECTED, "Rejected", 5, False, True, [ADJ_DRAFT], "#red", {}, {}),
    (ADJ_CANCELLED, "Cancelled", 6, False, True, [], "#orange", {}, {}),
]

# The clock that matters here is on `dispatched`, not on approval: once goods
# have gone back, the vendor owes a credit, and a return that is never credited
# is money quietly written off. Nothing else in the system chases it, which is
# exactly the gap DR-009 found for tenders.
DEFAULT_VENDOR_RETURN_STATES = [
    (RET_DRAFT, "Draft", 1, True, False, [RET_PENDING_APPROVAL, RET_CANCELLED],
        "#gray", {}, {}),
    (RET_PENDING_APPROVAL, "Pending Approval", 2, False, False,
        [RET_APPROVED, RET_REJECTED], "#yellow", {},
        {"hours": 24, "escalate_to": "manager"}),
    (RET_APPROVED, "Approved", 3, False, False, [RET_DISPATCHED, RET_CANCELLED],
        "#blue", {}, {"hours": 72, "escalate_to": "manager"}),
    (RET_DISPATCHED, "Dispatched", 4, False, False, [RET_CREDITED], "#purple",
        {}, {"hours": 720, "escalate_to": "manager"}),
    (RET_CREDITED, "Credited", 5, False, True, [], "#green", {}, {}),
    (RET_REJECTED, "Rejected", 6, False, True, [RET_DRAFT], "#red", {}, {}),
    (RET_CANCELLED, "Cancelled", 7, False, True, [], "#orange", {}, {}),
]



# --- Variant C: HR ----------------------------------------------------------
from app.models.hr import (  # noqa: E402
    HC_DRAFT, HC_PENDING_APPROVAL, HC_APPROVED, HC_FILLED, HC_REJECTED,
    HC_CANCELLED, PAY_DRAFT, PAY_PENDING_APPROVAL, PAY_APPROVED, PAY_APPLIED,
    PAY_REJECTED, PAY_CANCELLED, EXP_DRAFT, EXP_PENDING_APPROVAL, EXP_APPROVED,
    EXP_PAID, EXP_REJECTED, EXP_CANCELLED,
)

# A hire waits on a budget holder, so 48 hours rather than 24: this is a
# decision somebody should think about, and chasing it the next morning trains
# people to approve without reading. `approved` carries a clock of its own —
# an approved role nobody fills is committed cost sitting on the budget, and
# two weeks is long enough to notice the recruitment never started.
DEFAULT_HEADCOUNT_STATES = [
    (HC_DRAFT, "Draft", 1, True, False, [HC_PENDING_APPROVAL, HC_CANCELLED],
        "#gray", {}, {}),
    (HC_PENDING_APPROVAL, "Pending Approval", 2, False, False,
        [HC_APPROVED, HC_REJECTED], "#yellow", {},
        {"hours": 48, "escalate_to": "cfo"}),
    (HC_APPROVED, "Approved", 3, False, False, [HC_FILLED, HC_CANCELLED],
        "#blue", {}, {"hours": 336, "escalate_to": "manager"}),
    (HC_FILLED, "Filled", 4, False, True, [], "#green", {}, {}),
    (HC_REJECTED, "Rejected", 5, False, True, [HC_DRAFT], "#red", {}, {}),
    (HC_CANCELLED, "Cancelled", 6, False, True, [], "#orange", {}, {}),
]

# Pay changes have a date they take effect from, and missing it means somebody
# is paid the wrong amount for a month and then owed arrears. 24 hours.
# `applied` is terminal: reversing a change means raising another, so the
# record shows both what happened and what undid it.
DEFAULT_PAYROLL_CHANGE_STATES = [
    (PAY_DRAFT, "Draft", 1, True, False, [PAY_PENDING_APPROVAL, PAY_CANCELLED],
        "#gray", {}, {}),
    (PAY_PENDING_APPROVAL, "Pending Approval", 2, False, False,
        [PAY_APPROVED, PAY_REJECTED], "#yellow", {},
        {"hours": 24, "escalate_to": "cfo"}),
    (PAY_APPROVED, "Approved", 3, False, False, [PAY_APPLIED], "#blue", {}, {}),
    (PAY_APPLIED, "Applied", 4, False, True, [], "#green", {}, {}),
    (PAY_REJECTED, "Rejected", 5, False, True, [PAY_DRAFT], "#red", {}, {}),
    (PAY_CANCELLED, "Cancelled", 6, False, True, [], "#orange", {}, {}),
]

# An expense claim is somebody's own money that the company is holding. The
# clock on `approved` matters as much as the one on approval: a claim approved
# and never paid is the version of this that quietly damages trust, and nothing
# else in the system would chase it.
DEFAULT_EXPENSE_STATES = [
    (EXP_DRAFT, "Draft", 1, True, False, [EXP_PENDING_APPROVAL, EXP_CANCELLED],
        "#gray", {}, {}),
    (EXP_PENDING_APPROVAL, "Pending Approval", 2, False, False,
        [EXP_APPROVED, EXP_REJECTED], "#yellow", {},
        {"hours": 72, "escalate_to": "manager"}),
    (EXP_APPROVED, "Approved", 3, False, False, [EXP_PAID], "#blue", {},
        {"hours": 168, "escalate_to": "cfo"}),
    (EXP_PAID, "Paid", 4, False, True, [], "#green", {}, {}),
    (EXP_REJECTED, "Rejected", 5, False, True, [EXP_DRAFT], "#red", {}, {}),
    (EXP_CANCELLED, "Cancelled", 6, False, True, [], "#orange", {}, {}),
]

#: {workflow_type: states} — provisioning seeds each independently, so a tenant
#: created before a workflow existed gains it on the next run.
DEFAULT_WORKFLOWS = {
    "requisition": DEFAULT_REQUISITION_STATES,
    "rfq": DEFAULT_RFQ_STATES,
    "invoice": DEFAULT_INVOICE_STATES,
    "purchase_order": DEFAULT_PURCHASE_ORDER_STATES,
    "payment": DEFAULT_PAYMENT_STATES,
    "inventory_adjustment": DEFAULT_INVENTORY_ADJUSTMENT_STATES,
    "vendor_return": DEFAULT_VENDOR_RETURN_STATES,
    "headcount_request": DEFAULT_HEADCOUNT_STATES,
    "payroll_change_request": DEFAULT_PAYROLL_CHANGE_STATES,
    "expense_reimbursement": DEFAULT_EXPENSE_STATES,
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
