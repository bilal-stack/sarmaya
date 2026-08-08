"""Three-way matching: order versus delivery versus bill.

The control that stops an organisation paying for what it never ordered and
never received. Two-way matching (invoice against order) catches a supplier
billing the wrong amount; only the third leg — what actually arrived — catches
billing for goods that never came.

The comparison is per line, because the interesting failures are partial: half
a delivery invoiced in full, or one line quietly inflated inside an otherwise
correct invoice. A header-only total check passes all of those.

Tolerances are configuration, not constants. Real deliveries are short by a
box and real invoices differ by rounding, so a match that fails on every
trivial discrepancy gets switched off — and a control that is switched off
protects nothing.

The result is advisory here and enforced by the invoice approval gate. It
never blocks recording an invoice, only approving one, so AP can keep working
while a discrepancy is resolved.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.services.policy import get_match_tolerance

logger = logging.getLogger(__name__)

#: Outcomes, worst first. `unmatched` means the invoice names no purchase
#: order at all — not a failure, just nothing to match against.
MATCHED = "matched"
WITHIN_TOLERANCE = "within_tolerance"
MISMATCHED = "mismatched"
UNMATCHED = "unmatched"


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


class ThreeWayMatchService:
    def __init__(self, db: Session):
        self.db = db

    def match_invoice(self, invoice: Invoice, tenant_id) -> Dict:
        """Compare an invoice against its purchase order and what was received.

        Returns a verdict plus the discrepancies behind it, so a reviewer sees
        which line disagrees rather than only that something did.
        """
        if not invoice.purchase_order_id:
            return {
                "result": UNMATCHED,
                "reason": "This invoice is not linked to a purchase order.",
                "purchase_order_id": None,
                "discrepancies": [],
                "tolerance": None,
            }

        order = (
            self.db.query(PurchaseOrder)
            .filter(PurchaseOrder.id == invoice.purchase_order_id)
            .first()
        )
        if not order:
            return {
                "result": MISMATCHED,
                "reason": "The linked purchase order no longer exists.",
                "purchase_order_id": str(invoice.purchase_order_id),
                "discrepancies": [],
                "tolerance": None,
            }

        tolerance = get_match_tolerance(self.db, tenant_id)
        discrepancies: List[Dict] = []

        # --- the money leg: invoice against order -----------------------------
        invoice_total = _d(invoice.total_amount)
        order_total = _d(order.total_amount)
        over_billed = invoice_total - order_total
        allowed_amount = order_total * _d(tolerance["amount_percent"]) / _d(100)

        if over_billed > allowed_amount:
            discrepancies.append({
                "kind": "amount",
                "detail": (
                    f"Invoiced {invoice_total:,.2f} against an order of "
                    f"{order_total:,.2f} — over by {over_billed:,.2f}."
                ),
                "ordered": float(order_total),
                "invoiced": float(invoice_total),
                "variance": float(over_billed),
            })

        # --- the delivery leg: invoice against what arrived --------------------
        lines = (
            self.db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.purchase_order_id == order.id)
            .order_by(PurchaseOrderLine.line_number)
            .all()
        )
        received_value = Decimal("0")
        nothing_received = True

        for line in lines:
            ordered_qty = _d(line.quantity)
            received_qty = _d(line.received_quantity)
            if received_qty > 0:
                nothing_received = False
            received_value += received_qty * _d(line.unit_price)

            shortfall = ordered_qty - received_qty
            allowed_qty = ordered_qty * _d(tolerance["quantity_percent"]) / _d(100)
            if shortfall > allowed_qty:
                discrepancies.append({
                    "kind": "quantity",
                    "detail": (
                        f"Line {line.line_number} ({line.description}): ordered "
                        f"{ordered_qty:g}, received {received_qty:g}."
                    ),
                    "line_number": line.line_number,
                    "ordered": float(ordered_qty),
                    "received": float(received_qty),
                    "variance": float(shortfall),
                })

        if nothing_received:
            discrepancies.append({
                "kind": "receipt",
                "detail": (
                    "Nothing has been received against this order. An invoice "
                    "for an undelivered order is the case three-way matching "
                    "exists to catch."
                ),
            })
        elif invoice_total - received_value > allowed_amount:
            discrepancies.append({
                "kind": "value",
                "detail": (
                    f"Invoiced {invoice_total:,.2f} but only "
                    f"{received_value:,.2f} has been received."
                ),
                "received_value": float(received_value),
                "invoiced": float(invoice_total),
                "variance": float(invoice_total - received_value),
            })

        if not discrepancies:
            # Distinguish exact from merely acceptable: an auditor reviewing a
            # run wants to know which invoices needed the tolerance.
            exact = invoice_total == order_total and not nothing_received
            return {
                "result": MATCHED if exact else WITHIN_TOLERANCE,
                "reason": (
                    "Invoice, order and receipt agree."
                    if exact else
                    "Invoice, order and receipt agree within the configured tolerance."
                ),
                "purchase_order_id": str(order.id),
                "po_number": order.po_number,
                "discrepancies": [],
                "tolerance": tolerance,
            }

        return {
            "result": MISMATCHED,
            "reason": discrepancies[0]["detail"],
            "purchase_order_id": str(order.id),
            "po_number": order.po_number,
            "discrepancies": discrepancies,
            "tolerance": tolerance,
        }
