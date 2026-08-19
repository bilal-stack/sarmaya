"""The Decision Inbox: everything waiting on you, from every module.

Build Book core differentiator — *one inbox across all departments for
approvals, exceptions, missing evidence, mismatches, reconciliation breaks, and
AI reviews* — and a Definition of Done item: the inbox supports every work item
type in the variant.

It did not. This service read invoices and nothing else, because that is all
there was when it was written; requisitions, tenders, orders, payment runs,
reconciliation breaks and vendor bank changes were added afterwards and none of
them ever appeared here. A manager with four approvals waiting saw an empty
inbox, and the product's central claim — one decision surface — was true of one
module out of eight.

Two ideas hold the design together:

  * **One item per thing, under its most blocking next step.** An invoice with
    a flagged duplicate is a duplicate problem, not an approval problem; saying
    both would make the reader decide which matters, which is the job the inbox
    exists to do for them.
  * **Only what the caller can act on.** An item somebody cannot resolve is
    noise that teaches them to skim, and skimming is how the one item that
    mattered gets missed.

Each module contributes a collector. They return the same neutral shape, so a
new module joins by adding one function rather than by changing the reader.
"""
from datetime import timedelta
from sqlalchemy.orm import Session
from typing import Callable, Dict, List, Optional, Tuple

from app.repositories.invoice_repository import InvoiceRepository
from app.models.workflow_state import WorkflowState
from app.services.policy import explain_approval_routing
from app.core.enums import (
    VendorStatus, RequisitionState, RFQState, PurchaseOrderState, PaymentState,
    BankChangeState,
)
from app.core.roles import (
    has_permission, ADMIN, PERM_APPROVE_INVOICE, PERM_MANAGE_VENDORS,
    PERM_APPROVE_REQUISITION, PERM_AWARD_SOURCING, PERM_APPROVE_PO,
    PERM_RELEASE_PAYMENT, PERM_RECONCILE_PAYMENT, PERM_APPROVE_BANK_CHANGE,
)
from app.services import sod
from app.utils.money import money_to_float
from app.utils.datetime_helpers import utc_now, to_utc

# --- work item types, as the Build Book names them ---------------------------
WORK_APPROVAL = "approval"          # approve or reject with rationale
WORK_EXCEPTION = "exception"        # a mismatch, missing evidence, or violation
WORK_REVIEW = "review"              # HITL review of an AI extraction or suggestion
WORK_RECONCILIATION = "reconciliation"  # a bank or ledger break
WORK_ADMIN = "admin"                # configuration or master-data change approval

# --- finer categories, which drive precedence and the UI label ---------------
CAT_DUPLICATE = "duplicate_review"
CAT_VENDOR = "vendor_verification"
CAT_APPROVAL = "approval"
CAT_REQUISITION = "requisition_approval"
CAT_AWARD = "sourcing_award"
CAT_PO = "purchase_order_approval"
CAT_PAYMENT = "payment_release"
CAT_BANK_CHANGE = "vendor_bank_change"
CAT_UNEXPLAINED_DEBIT = "unexplained_debit"

#: Lower sorts first within the same overdue bucket. The ordering is a claim
#: about what is most costly to leave: money that left without an instruction
#: beats money about to leave, which beats a commitment, which beats a request.
_PRIORITY = {
    CAT_UNEXPLAINED_DEBIT: 0,
    CAT_BANK_CHANGE: 1,
    CAT_DUPLICATE: 2,
    CAT_VENDOR: 3,
    CAT_PAYMENT: 4,
    CAT_APPROVAL: 5,
    CAT_PO: 6,
    CAT_AWARD: 7,
    CAT_REQUISITION: 8,
}


def sla_status(obj, sla_map: Dict[str, dict]) -> Tuple[Optional[object], bool, dict]:
    """(sla_due_at, overdue, sla_cfg) for any workflow object.

    Deadlines are computed at read time from state_entered_at, so an SLA config
    change re-prices every open timer immediately rather than only new ones.
    """
    state = getattr(obj.current_state, "value", obj.current_state)
    cfg = sla_map.get(str(state)) or {}
    hours = cfg.get("hours")
    if not hours:
        return None, False, cfg
    entered = getattr(obj, "state_entered_at", None) or obj.updated_at or obj.created_at
    if entered is None:
        return None, False, cfg
    due = to_utc(entered) + timedelta(hours=hours)
    return due, utc_now() > due, cfg


class DecisionInboxService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = InvoiceRepository(db)

    def get_inbox(
        self, current_user: dict, limit: int = 100, overdue_only: bool = False
    ) -> Dict:
        collectors: List[Callable[[dict], List[Dict]]] = [
            self._invoices,
            self._requisitions,
            self._tenders,
            self._purchase_orders,
            self._payments,
            self._bank_changes,
            self._reconciliation_breaks,
        ]

        items: List[Dict] = []
        for collect in collectors:
            items.extend(collect(current_user))

        if overdue_only:
            items = [i for i in items if i["overdue"]]

        # Breached SLAs first, then how costly the thing is to leave, then value.
        items.sort(key=lambda x: (0 if x["overdue"] else 1, x["priority"], -x["amount"]))
        items = items[:limit]

        counts: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for item in items:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
            by_type[item["work_item_type"]] = by_type.get(item["work_item_type"], 0) + 1

        return {
            "total": len(items),
            "counts": counts,
            "by_work_item_type": by_type,
            "overdue_count": sum(1 for i in items if i["overdue"]),
            "items": items,
        }

    # --- collectors ----------------------------------------------------------

    def _invoices(self, current_user: dict) -> List[Dict]:
        """Each pending invoice under its single most blocking next step.

        Precedence is deliberate: a flagged duplicate must be cleared before the
        vendor matters, and the vendor must be active before approval means
        anything.
        """
        role = current_user["role"]
        tenant_id = current_user["tenant_id"]
        is_admin = role == ADMIN
        can_approve = has_permission(role, PERM_APPROVE_INVOICE)
        can_manage_vendors = has_permission(role, PERM_MANAGE_VENDORS)
        sla_map = self._sla_map("invoice")

        items: List[Dict] = []
        # Pull extra so capability filtering still fills the page.
        for invoice, vendor in self.repository.get_pending_with_vendor(300):
            amount = money_to_float(invoice.total_amount)
            sla_due_at, overdue, sla_cfg = sla_status(invoice, sla_map)
            base = self._base(
                "invoice", invoice.id, invoice.invoice_number,
                invoice.vendor_name, amount, invoice.current_state,
                sla_due_at, overdue, f"/ai-tools/invoices/{invoice.id}",
            )

            if invoice.potential_duplicate_id and not invoice.duplicate_acknowledged:
                if not can_approve:
                    continue
                items.append({
                    **base,
                    "work_item_type": WORK_EXCEPTION,
                    "category": CAT_DUPLICATE,
                    "priority": _PRIORITY[CAT_DUPLICATE],
                    "action": "Review potential duplicate",
                    "reason": "Flagged as a potential duplicate; override with a logged reason or reject.",
                })
            elif vendor is None or vendor.status != VendorStatus.ACTIVE:
                if not can_manage_vendors:
                    continue
                vstatus = vendor.status.value if vendor else "missing"
                items.append({
                    **base,
                    "work_item_type": WORK_EXCEPTION,
                    "category": CAT_VENDOR,
                    "priority": _PRIORITY[CAT_VENDOR],
                    "action": "Verify vendor",
                    "reason": f"Vendor is {vstatus}; activate it to release the invoice for approval.",
                })
            else:
                routing = explain_approval_routing(self.db, tenant_id, amount)
                required = routing["required_role"]
                # Once the SLA is breached the configured escalation role can
                # also act (Build Book: escalation reassigns while preserving
                # the original chain — audited by the runner).
                escalate_to = (sla_cfg.get("escalate_to") or "").lower()
                via_escalation = bool(
                    overdue and escalate_to and role.lower() == escalate_to
                    and role.lower() != required.lower()
                )
                if not (can_approve and (
                    is_admin or role.lower() == required.lower() or via_escalation
                )):
                    continue
                reason = routing["reason"]
                if via_escalation:
                    reason += " Escalated to you after the SLA was breached."
                items.append({
                    **base,
                    "work_item_type": WORK_APPROVAL,
                    "category": CAT_APPROVAL,
                    "priority": _PRIORITY[CAT_APPROVAL],
                    "action": "Approve or reject",
                    "reason": reason,
                    "required_role": required,
                    "escalated": via_escalation,
                })
        return items

    def _requisitions(self, current_user: dict) -> List[Dict]:
        """Requests waiting for someone to authorise the need.

        Nothing outside the system chases these: a requisition nobody actions
        simply sits, while whoever raised it waits for equipment.
        """
        if not has_permission(current_user["role"], PERM_APPROVE_REQUISITION):
            return []
        from app.models.requisition import PurchaseRequisition

        sla_map = self._sla_map("requisition")
        items = []
        for req in (
            self.db.query(PurchaseRequisition)
            .filter(PurchaseRequisition.current_state
                    == RequisitionState.PENDING_APPROVAL.value)
            .limit(200).all()
        ):
            # You cannot approve what you raised, so it is not your work item.
            # Ask the rule rather than restating it: it exempts admins so that a
            # one-person tenant still functions, and an item hidden from the only
            # person who may act on it stalls with nothing to show why.
            if sod.violates_self_approval(req, current_user):
                continue
            sla_due_at, overdue, _ = sla_status(req, sla_map)
            items.append({
                **self._base(
                    "requisition", req.id, req.requisition_number, req.title,
                    money_to_float(req.estimated_amount), req.current_state,
                    sla_due_at, overdue, f"/ai-tools/requisitions/{req.id}",
                ),
                "work_item_type": WORK_APPROVAL,
                "category": CAT_REQUISITION,
                "priority": _PRIORITY[CAT_REQUISITION],
                "action": "Approve or reject the request",
                "reason": req.justification or "A request is waiting on your decision.",
            })
        return items

    def _tenders(self, current_user: dict) -> List[Dict]:
        """Closed tenders with no winner picked.

        Quoting has finished and the vendors are waiting. There is no SLA on an
        RFQ today, so nothing else in the system chases this at all.
        """
        if not has_permission(current_user["role"], PERM_AWARD_SOURCING):
            return []
        from app.models.rfq import RFQ

        items = []
        for rfq in (
            self.db.query(RFQ)
            .filter(RFQ.current_state == RFQState.CLOSED.value)
            .limit(200).all()
        ):
            quotes = rfq.quotes or []
            compliant = [q for q in quotes if q.is_compliant]
            lowest = min(
                (money_to_float(q.total_amount) for q in compliant), default=0.0
            )
            items.append({
                **self._base(
                    "rfq", rfq.id, rfq.rfq_number, rfq.title, lowest,
                    rfq.current_state, None, False, f"/ai-tools/rfqs/{rfq.id}",
                ),
                "work_item_type": WORK_APPROVAL,
                "category": CAT_AWARD,
                "priority": _PRIORITY[CAT_AWARD],
                "action": "Award the tender",
                "reason": (
                    f"{len(quotes)} quote(s) received and quoting is closed. "
                    "Anything but the lowest compliant quote needs a written reason."
                ),
            })
        return items

    def _purchase_orders(self, current_user: dict) -> List[Dict]:
        if not has_permission(current_user["role"], PERM_APPROVE_PO):
            return []
        from app.models.purchase_order import PurchaseOrder

        sla_map = self._sla_map("purchase_order")
        items = []
        for order in (
            self.db.query(PurchaseOrder)
            .filter(PurchaseOrder.current_state
                    == PurchaseOrderState.PENDING_APPROVAL.value)
            .limit(200).all()
        ):
            if sod.violates_self_approval(order, current_user):
                continue
            sla_due_at, overdue, _ = sla_status(order, sla_map)
            items.append({
                **self._base(
                    "purchase_order", order.id, order.po_number, order.vendor_name,
                    money_to_float(order.total_amount), order.current_state,
                    sla_due_at, overdue, f"/ai-tools/purchase-orders/{order.id}",
                ),
                "work_item_type": WORK_APPROVAL,
                "category": CAT_PO,
                "priority": _PRIORITY[CAT_PO],
                "action": "Approve or reject the order",
                "reason": "Committing the company to this spend before it reaches the vendor.",
            })
        return items

    def _payments(self, current_user: dict) -> List[Dict]:
        """Runs prepared but not released — money waiting on a second person."""
        if not has_permission(current_user["role"], PERM_RELEASE_PAYMENT):
            return []
        from app.models.payment import Payment

        sla_map = self._sla_map("payment")
        items = []
        for payment in (
            self.db.query(Payment)
            .filter(Payment.current_state == PaymentState.PENDING_RELEASE.value)
            .limit(200).all()
        ):
            # Maker-checker refuses this at release, so it is not your work item.
            # This rule has no admin exemption; the shared helper is what says so.
            if sod.violates_self_release(payment.prepared_by, current_user):
                continue
            sla_due_at, overdue, _ = sla_status(payment, sla_map)
            items.append({
                **self._base(
                    "payment", payment.id, payment.payment_number,
                    f"{len(payment.lines)} invoice(s)",
                    money_to_float(payment.total_amount), payment.current_state,
                    sla_due_at, overdue, f"/ai-tools/payments/{payment.id}",
                ),
                "work_item_type": WORK_APPROVAL,
                "category": CAT_PAYMENT,
                "priority": _PRIORITY[CAT_PAYMENT],
                "action": "Release or reject the run",
                "reason": (
                    "Prepared by someone else and awaiting authorisation. This is "
                    "the last gate before an instruction reaches a bank."
                ),
            })
        return items

    def _bank_changes(self, current_user: dict) -> List[Dict]:
        """Proposed changes to where a vendor's money goes.

        First in priority among approvals: while one is open every payment to
        that vendor is held, and the cooling period only helps if somebody looks
        during it.
        """
        if not has_permission(current_user["role"], PERM_APPROVE_BANK_CHANGE):
            return []
        from app.models.vendor_bank_change import VendorBankChange

        items = []
        for change in (
            self.db.query(VendorBankChange)
            .filter(VendorBankChange.current_state
                    == BankChangeState.PENDING_APPROVAL.value)
            .limit(200).all()
        ):
            # No admin exemption on this SoD rule, so a requester never sees
            # their own change here either.
            if sod.violates_self_bank_change_approval(change.requested_by, current_user):
                continue
            vendor_name = change.vendor.legal_name if change.vendor else "a vendor"
            items.append({
                **self._base(
                    "vendor_bank_change", change.id, vendor_name,
                    "bank details", 0.0, change.current_state,
                    None, False, "/ai-tools/vendors/bank-changes",
                ),
                "work_item_type": WORK_ADMIN,
                "category": CAT_BANK_CHANGE,
                "priority": _PRIORITY[CAT_BANK_CHANGE],
                "action": "Verify and approve, or reject",
                "reason": (
                    f"Reason given: {change.reason} — payments to {vendor_name} "
                    "are held until this is resolved. Confirm on a number you "
                    "already had, not one in the request."
                ),
            })
        return items

    def _reconciliation_breaks(self, current_user: dict) -> List[Dict]:
        """Bank debits no instruction explains.

        The highest priority in the inbox. Everything else here is money about
        to move; this is money that already has, with nothing in the system
        accounting for it.
        """
        if not has_permission(current_user["role"], PERM_RECONCILE_PAYMENT):
            return []
        from app.models.bank_statement import BankStatementLine

        items = []
        for line in (
            self.db.query(BankStatementLine)
            .filter(
                BankStatementLine.matched_payment_id.is_(None),
                BankStatementLine.is_debit.is_(True),
            )
            .order_by(BankStatementLine.value_date.desc())
            .limit(200).all()
        ):
            items.append({
                **self._base(
                    "bank_statement_line", line.id,
                    line.bank_reference or "Unreferenced debit",
                    line.counterparty or line.description or "unknown counterparty",
                    money_to_float(line.amount), "unreconciled",
                    None, False, "/ai-tools/reconciliation",
                ),
                "work_item_type": WORK_RECONCILIATION,
                "category": CAT_UNEXPLAINED_DEBIT,
                "priority": _PRIORITY[CAT_UNEXPLAINED_DEBIT],
                "action": "Match it, or investigate",
                "reason": (
                    "Money left the account and no payment run is matched to it. "
                    "A debit nothing explains cannot be produced by any mistake "
                    "inside the workflow."
                ),
            })
        return items

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _base(
        object_type: str, object_id, reference: str, subtitle: str,
        amount: float, current_state, sla_due_at, overdue: bool, detail_url: str,
    ) -> Dict:
        """The shape every collector returns.

        Neutral on purpose: the reader renders a work item without knowing
        which module produced it, so a new module joins by adding a collector
        rather than by changing the screen.
        """
        return {
            "object_type": object_type,
            "object_id": object_id,
            "reference": reference,
            "subtitle": subtitle,
            "amount": amount,
            "current_state": str(getattr(current_state, "value", current_state)),
            "sla_due_at": sla_due_at,
            "overdue": overdue,
            "escalated": False,
            "required_role": None,
            "detail_url": detail_url,
            "timeline_url": f"/api/v1/audit/timeline/{object_type}/{object_id}",
        }

    def _sla_map(self, workflow_type: str) -> Dict[str, dict]:
        """Per-state SLA config for one workflow (tenant-scoped by RLS)."""
        states = (
            self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == workflow_type)
            .all()
        )
        return {s.state_name: dict(s.sla or {}) for s in states if s.sla}
