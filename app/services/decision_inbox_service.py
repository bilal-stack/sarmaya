from sqlalchemy.orm import Session
from typing import Dict, List

from app.repositories.invoice_repository import InvoiceRepository
from app.services.policy import explain_approval_routing
from app.core.enums import VendorStatus
from app.core.roles import has_permission, ADMIN, PERM_APPROVE_INVOICE, PERM_MANAGE_VENDORS
from app.utils.money import money_to_float

CAT_DUPLICATE = "duplicate_review"
CAT_VENDOR = "vendor_verification"
CAT_APPROVAL = "approval"

_PRIORITY = {CAT_DUPLICATE: 1, CAT_VENDOR: 2, CAT_APPROVAL: 3}


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

    def get_inbox(self, current_user: dict, limit: int = 100) -> Dict:
        role = current_user["role"]
        tenant_id = current_user["tenant_id"]
        is_admin = role == ADMIN
        can_approve = has_permission(role, PERM_APPROVE_INVOICE)
        can_manage_vendors = has_permission(role, PERM_MANAGE_VENDORS)

        items: List[Dict] = []
        # Pull extra so capability filtering still fills the page.
        for invoice, vendor in self.repository.get_pending_with_vendor(limit * 3):
            amount = money_to_float(invoice.total_amount)
            base = {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
                "amount": amount,
                "current_state": invoice.current_state,
                "timeline_url": f"/api/v1/audit/timeline/invoice/{invoice.id}",
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
                if not (can_approve and (is_admin or role.lower() == required.lower())):
                    continue
                items.append({
                    **base,
                    "category": CAT_APPROVAL,
                    "priority": _PRIORITY[CAT_APPROVAL],
                    "action": "Approve or reject",
                    "reason": routing["reason"],
                    "required_role": required,
                })

        items.sort(key=lambda x: (x["priority"], -x["amount"]))
        items = items[:limit]

        counts: Dict[str, int] = {}
        for it in items:
            counts[it["category"]] = counts.get(it["category"], 0) + 1

        return {"total": len(items), "counts": counts, "items": items}
