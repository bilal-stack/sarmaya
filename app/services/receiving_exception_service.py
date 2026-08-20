"""Explaining a delivery that did not match its order.

Build Book, Variant D1 AI assists: "exception explanations for shortages,
damages, delays" and "suggest likely root causes and required follow-up tasks".

The exception itself is computed, not guessed. Short, over, late — those are
arithmetic on the order and the receipt, and they are always available. The AI
adds a sentence about the likely cause and what to do next, and if it is
unavailable or unconvincing the deterministic explanation stands on its own.

That is the shape every AI path in this system takes, and here it matters more
than usual: a receiving clerk deciding whether to reject a delivery needs an
answer now, and a screen that shows nothing because a provider is down would
send them to the phone instead.

**A suggested reason code is validated, never trusted.** A model inventing a
code would quietly create a category nothing counts, which is the exact failure
a fixed vocabulary exists to prevent — so anything outside the list is dropped
and the explanation keeps its prose.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import has_permission, PERM_VIEW_INVENTORY
from app.models.goods_receipt import GoodsReceipt, GoodsReceiptLine
from app.models.inventory import (
    REASON_CODES, REASON_SHORTAGE, REASON_OVERAGE,
    VENDOR_ATTRIBUTABLE_REASONS,
)
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.services.ai_action_log import (
    log_ai_action, STATUS_COMPLETED, STATUS_ERROR,
)

logger = logging.getLogger(__name__)

ACTION = "receiving_exception"

#: Late by more than this and the delay is worth explaining. A day or two on a
#: promised date is normal commercial slippage; a week is a supply problem.
LATE_DAYS_THRESHOLD = 2


class ReceivingExceptionService:
    def __init__(self, db: Session):
        self.db = db

    def explain(self, receipt_line_id: UUID, current_user: dict) -> Dict:
        """What went wrong with this delivery line, and what to do.

        Always returns an explanation. The AI section is added when it is
        available and confident, and its absence is stated rather than hidden —
        a blank where an explanation should be reads as a broken screen.
        """
        if not has_permission(current_user["role"], PERM_VIEW_INVENTORY):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view receiving exceptions"
            )

        facts = self._facts(receipt_line_id)
        if facts is None:
            raise ValueError("Goods receipt line not found")

        explanation = {
            **facts,
            "ai": None,
            "ai_note": None,
        }

        if not facts["exceptions"]:
            explanation["ai_note"] = (
                "Nothing to explain: this line arrived complete and on time."
            )
            return explanation

        try:
            from app.services.ai.router import AIRouter, AIUnavailable
        except ImportError:  # pragma: no cover - the router is always present
            return explanation

        try:
            result = AIRouter(self.db).run(
                "receiving_exception",
                {
                    "ordered": facts["quantity_ordered"],
                    "received": facts["quantity_received"],
                    "uom": facts["uom"],
                    "item": facts["item"],
                    "expected_date": facts["expected_date"] or "not stated",
                    "received_date": facts["received_date"],
                    "vendor": facts["vendor"],
                    "notes": facts["notes"] or "none",
                    "reason_codes": ", ".join(REASON_CODES),
                },
            )
        except AIUnavailable as exc:
            log_ai_action(
                self.db, current_user["tenant_id"], current_user.get("id"),
                action=ACTION, status=STATUS_ERROR,
                object_type="goods_receipt_line", object_id=receipt_line_id,
                output_summary=str(exc)[:300],
            )
            self.db.commit()
            explanation["ai_note"] = (
                "No AI explanation available right now. The figures above are "
                "computed and stand on their own."
            )
            return explanation

        output = result.output

        # Validated, not trusted. Anything outside the vocabulary is dropped
        # rather than stored, so a hallucinated code never becomes a category.
        suggested = output.suggested_reason_code
        if suggested is not None and suggested not in REASON_CODES:
            logger.warning(
                "Discarding unknown reason code %r suggested for receipt line %s",
                suggested, receipt_line_id,
            )
            suggested = None

        explanation["ai"] = {
            "likely_cause": output.likely_cause,
            "suggested_reason_code": suggested,
            "follow_up_actions": list(output.follow_up_actions),
            "vendor_attributable": bool(output.vendor_attributable),
            "confidence": output.confidence,
            "reasoning": output.reasoning,
            "model": result.model,
            "prompt_version": result.prompt_version,
        }

        log_ai_action(
            self.db, current_user["tenant_id"], current_user.get("id"),
            action=ACTION, status=STATUS_COMPLETED,
            ai_provider=result.provider, ai_model=result.model,
            prompt_version=result.prompt_version,
            confidence=output.confidence,
            object_type="goods_receipt_line", object_id=receipt_line_id,
            input_summary=f"{facts['item']}: ordered {facts['quantity_ordered']}, "
                          f"received {facts['quantity_received']}",
            output_summary=output.likely_cause[:300],
        )
        self.db.commit()
        return explanation

    # --- the computed half ---------------------------------------------------

    def _facts(self, receipt_line_id: UUID) -> Optional[Dict]:
        """What is true about this delivery, from the records.

        Everything here is arithmetic. It is what the screen shows when the AI
        is unavailable, so it has to be complete enough to act on by itself.
        """
        row = (
            self.db.query(GoodsReceiptLine, GoodsReceipt, PurchaseOrderLine, PurchaseOrder)
            .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
            .join(
                PurchaseOrderLine,
                PurchaseOrderLine.id == GoodsReceiptLine.purchase_order_line_id,
            )
            .join(
                PurchaseOrder,
                PurchaseOrder.id == PurchaseOrderLine.purchase_order_id,
            )
            .filter(GoodsReceiptLine.id == receipt_line_id)
            .first()
        )
        if row is None:
            return None

        receipt_line, receipt, po_line, order = row

        ordered = Decimal(po_line.quantity or 0)
        received_to_date = Decimal(po_line.received_quantity or 0)
        this_delivery = Decimal(receipt_line.quantity_received or 0)

        exceptions: List[Dict] = []

        if received_to_date < ordered:
            exceptions.append({
                "type": REASON_SHORTAGE,
                "detail": (
                    f"{ordered - received_to_date} of {ordered} still "
                    "outstanding across all deliveries."
                ),
            })
        elif received_to_date > ordered:
            exceptions.append({
                "type": REASON_OVERAGE,
                "detail": (
                    f"{received_to_date - ordered} more than ordered has been "
                    "received."
                ),
            })

        days_late = None
        if order.expected_date and receipt.received_date:
            days_late = (receipt.received_date - order.expected_date).days
            if days_late > LATE_DAYS_THRESHOLD:
                exceptions.append({
                    "type": "delay",
                    "detail": f"Arrived {days_late} days after the promised date.",
                })

        rejected = sum(
            Decimal(check.quantity_rejected or 0)
            for check in getattr(receipt_line, "quality_checks", [])
        )
        notes = " ".join(
            check.notes for check in getattr(receipt_line, "quality_checks", [])
            if check.notes
        ) or None
        if rejected:
            exceptions.append({
                "type": "quality",
                "detail": f"{rejected} rejected at inspection.",
            })

        return {
            "receipt_line_id": receipt_line.id,
            "grn_number": receipt.grn_number,
            "item": po_line.description,
            "uom": "units",
            "vendor": order.vendor_name,
            "quantity_ordered": float(ordered),
            "quantity_received": float(this_delivery),
            "quantity_received_to_date": float(received_to_date),
            "quantity_rejected": float(rejected),
            "expected_date": order.expected_date,
            "received_date": receipt.received_date,
            "days_late": days_late,
            "notes": notes,
            "exceptions": exceptions,
            # The deterministic answer, always present. Whether the vendor is
            # at fault is a judgement, so the computed half only says what the
            # agreed vocabulary already treats as their responsibility.
            "vendor_attributable_by_reason": any(
                e["type"] in VENDOR_ATTRIBUTABLE_REASONS for e in exceptions
            ),
        }
