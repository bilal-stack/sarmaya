from datetime import timedelta
from sqlalchemy.orm import Session
from typing import Dict, List, Optional, Tuple

from app.repositories.invoice_repository import InvoiceRepository
from app.models.workflow_state import WorkflowState
from app.services.policy import explain_approval_routing
from app.core.enums import VendorStatus
from app.core.roles import has_permission, ADMIN, PERM_APPROVE_INVOICE, PERM_MANAGE_VENDORS
from app.utils.money import money_to_float
from app.utils.datetime_helpers import utc_now, to_utc

CAT_DUPLICATE = "duplicate_review"
CAT_VENDOR = "vendor_verification"
CAT_APPROVAL = "approval"

_PRIORITY = {CAT_DUPLICATE: 1, CAT_VENDOR: 2, CAT_APPROVAL: 3}


def sla_status(invoice, sla_map: Dict[str, dict]) -> Tuple[Optional[object], bool, dict]:
    """(sla_due_at, overdue, sla_cfg) for an invoice against per-state SLA
    config. Deadlines are computed at read time from state_entered_at, so an
    SLA config change re-prices every open timer immediately."""
    state = getattr(invoice.current_state, "value", invoice.current_state)
    cfg = sla_map.get(str(state)) or {}
    hours = cfg.get("hours")
    if not hours:
        return None, False, cfg
    entered = invoice.state_entered_at or invoice.updated_at or invoice.created_at
    if entered is None:
        return None, False, cfg
    due = to_utc(entered) + timedelta(hours=hours)
    return due, utc_now() > due, cfg


class DecisionInboxService:
    """One cross-task surface: every pending invoice reduced to its single most
    blocking next action, filtered to what the caller can actually do.

    Precedence per invoice: a flagged duplicate must be cleared before the vendor
    matters, and the vendor must be active before approval is meaningful — so
    each invoice appears once, under its top blocker.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repository = InvoiceRepository(db)

    def get_inbox(self, current_user: dict, limit: int = 100, overdue_only: bool = False) -> Dict:
        role = current_user["role"]
        tenant_id = current_user["tenant_id"]
        is_admin = role == ADMIN
        can_approve = has_permission(role, PERM_APPROVE_INVOICE)
        can_manage_vendors = has_permission(role, PERM_MANAGE_VENDORS)
        sla_map = self._sla_map()

        items: List[Dict] = []
        # Pull extra so capability filtering still fills the page.
        for invoice, vendor in self.repository.get_pending_with_vendor(limit * 3):
            amount = money_to_float(invoice.total_amount)
            sla_due_at, overdue, sla_cfg = sla_status(invoice, sla_map)
            if overdue_only and not overdue:
                continue
            base = {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
                "amount": amount,
                "current_state": invoice.current_state,
                "timeline_url": f"/api/v1/audit/timeline/invoice/{invoice.id}",
                "sla_due_at": sla_due_at,
                "overdue": overdue,
                "escalated": False,
            }

            if invoice.potential_duplicate_id and not invoice.duplicate_acknowledged:
                if not can_approve:
                    continue
                items.append({
                    **base,
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
                    "category": CAT_VENDOR,
                    "priority": _PRIORITY[CAT_VENDOR],
                    "action": "Verify vendor",
                    "reason": f"Vendor is {vstatus}; activate it to release the invoice for approval.",
                })
            else:
                routing = explain_approval_routing(self.db, tenant_id, amount)
                required = routing["required_role"]
                # Once the SLA is breached, the configured escalation role can
                # also see and act on the item (Build Book: escalation reassigns
                # while preserving the original chain — audited by the runner).
                escalate_to = (sla_cfg.get("escalate_to") or "").lower()
                via_escalation = bool(
                    overdue and escalate_to and role.lower() == escalate_to
                    and role.lower() != required.lower()
                )
                if not (can_approve and (is_admin or role.lower() == required.lower() or via_escalation)):
                    continue
                reason = routing["reason"]
                if via_escalation:
                    reason += " Escalated to you after the SLA was breached."
                items.append({
                    **base,
                    "category": CAT_APPROVAL,
                    "priority": _PRIORITY[CAT_APPROVAL],
                    "action": "Approve or reject",
                    "reason": reason,
                    "required_role": required,
                    "escalated": via_escalation,
                })

        # Breached SLAs surface first, then blocker priority, then amount.
        items.sort(key=lambda x: (0 if x["overdue"] else 1, x["priority"], -x["amount"]))
        items = items[:limit]

        counts: Dict[str, int] = {}
        for it in items:
            counts[it["category"]] = counts.get(it["category"], 0) + 1
        overdue_count = sum(1 for it in items if it["overdue"])

        return {"total": len(items), "counts": counts, "overdue_count": overdue_count, "items": items}

    def _sla_map(self) -> Dict[str, dict]:
        """Per-state SLA config for the invoice workflow (tenant-scoped by RLS)."""
        states = (
            self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == "invoice")
            .all()
        )
        return {s.state_name: dict(s.sla or {}) for s in states if s.sla}
