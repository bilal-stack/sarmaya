"""Quality checks on goods that arrived.

Build Book, Variant D1: "receiving, GRN, quality checks, putaway and stock
updates", with "damage and shortage evidence requirements with photos and QC
notes".

A check is a decision about a specific delivery of a specific item, so it hangs
off a goods receipt line. Failing one does **not** undo the receipt — what
arrived still arrived, and deleting that would be editing away a fact. Instead
the rejected quantity moves to quarantine, where it cannot be picked while
somebody decides whether it goes back to the vendor.

The rule with teeth here is that **a rejection must be explained**. A reason
code and a note are required before stock can be quarantined, because "27 units
rejected" with no reason is unusable three months later when somebody asks
which supplier keeps sending damaged goods — and that question is the entire
point of recording reason codes rather than free text.

Putaway is the other half: accepted goods move out of the receiving bay to
where they are actually kept. Modelled as a transfer rather than a flag,
because it is a real movement of real things between two places and the ledger
should say so.
"""
import logging
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_INSPECT_GOODS, PERM_VIEW_INVENTORY,
)
from app.models.goods_receipt import GoodsReceipt, GoodsReceiptLine
from app.models.inventory import (
    Item, StockLocation, MOVE_TRANSFER, REASON_CODES,
)
from app.models.inventory_control import (
    QualityCheck, QC_PASSED, QC_FAILED, QC_PARTIAL,
)
from app.models.purchase_order import PurchaseOrderLine
from app.services.audit import log_audit
from app.services.stock_service import StockService, _as_decimal
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "quality_check"


def _now():
    return make_naive(to_utc(utc_now()))


class QualityCheckService:
    def __init__(self, db: Session):
        self.db = db
        self.stock = StockService(db)

    def record(
        self, receipt_line_id: UUID, current_user: dict, *,
        quantity_accepted, quantity_rejected, reason_code: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> QualityCheck:
        """Inspect one receipt line.

        Rejected stock moves to quarantine in the same transaction as the
        check that rejected it: a check recording a rejection while the goods
        stayed available for picking is worse than no check at all, because it
        reads as a control that was applied.
        """
        self._require(current_user, PERM_INSPECT_GOODS, "inspect goods")

        line = (
            self.db.query(GoodsReceiptLine)
            .filter(GoodsReceiptLine.id == receipt_line_id)
            .first()
        )
        if not line:
            raise ValueError("Goods receipt line not found")

        accepted = _as_decimal(quantity_accepted or 0)
        rejected = _as_decimal(quantity_rejected or 0)

        if accepted < 0 or rejected < 0:
            raise ValueError("Inspected quantities cannot be negative")
        if accepted + rejected == 0:
            raise ValueError("A check that inspected nothing records nothing")

        received = _as_decimal(line.quantity_received)
        if accepted + rejected > received:
            raise ValueError(
                f"Inspected {accepted + rejected} but only {received} arrived on "
                "this line. A check cannot cover more than was delivered."
            )

        if rejected > 0:
            # Build Book: evidence requirements on damage and shortage. This is
            # the enforceable half — "27 rejected" with no reason is unusable
            # when somebody later asks which supplier keeps sending damaged
            # goods, which is why reason codes exist at all.
            if not reason_code:
                raise ValueError(
                    "A rejection needs a reason code. Without one the rejection "
                    "cannot be counted against a supplier later."
                )
            if reason_code not in REASON_CODES:
                raise ValueError(
                    f"{reason_code!r} is not a reason code. One of: "
                    f"{', '.join(REASON_CODES)}"
                )
            if not notes or not notes.strip():
                raise ValueError(
                    "A rejection needs a note describing what was wrong. The "
                    "reason code says the category; the note is the evidence."
                )

        outcome = (
            QC_PASSED if rejected == 0
            else QC_FAILED if accepted == 0
            else QC_PARTIAL
        )

        check = QualityCheck(
            tenant_id=current_user["tenant_id"],
            goods_receipt_line_id=line.id,
            outcome=outcome,
            quantity_accepted=accepted,
            quantity_rejected=rejected,
            reason_code=reason_code,
            notes=notes.strip() if notes else None,
            inspected_by=current_user["id"],
            inspected_at=_now(),
            correlation_id=self._correlation_of(line),
        )
        self.db.add(check)
        self.db.flush()

        if rejected > 0:
            self._quarantine(line, rejected, check, current_user)

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=check.id, action=f"quality_{outcome}",
            comment=notes.strip() if notes else None,
            after_value={
                "outcome": outcome,
                "accepted": float(accepted),
                "rejected": float(rejected),
                "reason_code": reason_code,
            },
        )
        self.db.commit()
        self.db.refresh(check)
        return check

    def _quarantine(
        self, line: GoodsReceiptLine, quantity: Decimal, check: QualityCheck,
        current_user: dict,
    ) -> None:
        """Move rejected goods somewhere they cannot be picked.

        Silent when there is nowhere to move from or to. That is deliberate:
        a tenant with no quarantine location configured, or a receipt that
        never landed stock, still gets its check recorded — losing the
        inspection because the warehouse setup is incomplete would be the
        wrong trade.
        """
        item_id, source_location = self._item_and_location(line)
        if item_id is None or source_location is None:
            return

        quarantine = (
            self.db.query(StockLocation)
            .filter(
                StockLocation.is_quarantine.is_(True),
                StockLocation.is_active.is_(True),
            )
            .order_by(StockLocation.code)
            .first()
        )
        if quarantine is None or quarantine.id == source_location:
            return

        self.stock.post_movement(
            tenant_id=check.tenant_id, item_id=item_id,
            location_id=source_location, quantity=-quantity,
            movement_type=MOVE_TRANSFER, current_user=current_user,
            reason_code=check.reason_code, source_type=OBJECT_TYPE,
            source_id=check.id, note="Failed quality check",
            correlation_id=check.correlation_id,
        )
        self.stock.post_movement(
            tenant_id=check.tenant_id, item_id=item_id,
            location_id=quarantine.id, quantity=quantity,
            movement_type=MOVE_TRANSFER, current_user=current_user,
            reason_code=check.reason_code, source_type=OBJECT_TYPE,
            source_id=check.id, note="Quarantined pending return",
            correlation_id=check.correlation_id,
        )

    def putaway(
        self, receipt_line_id: UUID, destination_id: UUID, quantity,
        current_user: dict,
    ) -> None:
        """Move accepted goods from the receiving bay to where they are kept.

        A transfer rather than a flag, because it is a real movement of real
        things between two places and the ledger is what people read to find
        out where something is.
        """
        self._require(current_user, PERM_INSPECT_GOODS, "put goods away")

        line = (
            self.db.query(GoodsReceiptLine)
            .filter(GoodsReceiptLine.id == receipt_line_id)
            .first()
        )
        if not line:
            raise ValueError("Goods receipt line not found")

        item_id, source_location = self._item_and_location(line)
        if item_id is None:
            raise ValueError(
                "This line has no stocked item, so there is nothing to put away"
            )
        if source_location is None:
            raise ValueError("This receipt did not land anywhere to move from")
        if source_location == destination_id:
            raise ValueError("The goods are already there")

        destination = (
            self.db.query(StockLocation)
            .filter(StockLocation.id == destination_id)
            .first()
        )
        if not destination:
            raise ValueError("Destination location not found")

        quantity = _as_decimal(quantity)
        if quantity <= 0:
            raise ValueError("Putaway quantity must be positive")

        correlation = self._correlation_of(line)
        self.stock.post_movement(
            tenant_id=line.tenant_id, item_id=item_id,
            location_id=source_location, quantity=-quantity,
            movement_type=MOVE_TRANSFER, current_user=current_user,
            source_type="putaway", source_id=line.id,
            note=f"Putaway to {destination.code}", correlation_id=correlation,
        )
        self.stock.post_movement(
            tenant_id=line.tenant_id, item_id=item_id,
            location_id=destination_id, quantity=quantity,
            movement_type=MOVE_TRANSFER, current_user=current_user,
            source_type="putaway", source_id=line.id,
            note="Putaway", correlation_id=correlation,
        )

        log_audit(
            db=self.db, tenant_id=line.tenant_id, user_id=current_user["id"],
            object_type="goods_receipt_line", object_id=line.id,
            action="put_away",
            after_value={
                "destination": destination.code, "quantity": float(quantity),
            },
        )
        self.db.commit()

    # --- reading -------------------------------------------------------------

    def checks_for_receipt(
        self, receipt_id: UUID, current_user: dict
    ) -> List[QualityCheck]:
        self._require(current_user, PERM_VIEW_INVENTORY, "view quality checks")
        return (
            self.db.query(QualityCheck)
            .join(
                GoodsReceiptLine,
                GoodsReceiptLine.id == QualityCheck.goods_receipt_line_id,
            )
            .filter(GoodsReceiptLine.goods_receipt_id == receipt_id)
            .order_by(QualityCheck.created_at)
            .all()
        )

    def uninspected_lines(self, current_user: dict) -> List[dict]:
        """Delivered but never checked.

        The gap somebody has to close: goods sitting in the bay that nobody has
        looked at are stock the system believes it has and nobody has confirmed.
        """
        self._require(current_user, PERM_VIEW_INVENTORY, "view quality checks")

        checked = {
            row[0] for row in
            self.db.query(QualityCheck.goods_receipt_line_id).all()
        }
        rows = (
            self.db.query(GoodsReceiptLine, GoodsReceipt)
            .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
            .order_by(GoodsReceipt.received_date.desc())
            .all()
        )
        return [
            {
                "receipt_line_id": line.id,
                "grn_number": receipt.grn_number,
                "received_date": receipt.received_date,
                "quantity_received": float(line.quantity_received),
            }
            for line, receipt in rows
            if line.id not in checked
        ]

    # --- helpers -------------------------------------------------------------

    def _item_and_location(self, line: GoodsReceiptLine):
        """The item this receipt line delivered and where it landed."""
        receipt = (
            self.db.query(GoodsReceipt)
            .filter(GoodsReceipt.id == line.goods_receipt_id)
            .first()
        )
        po_line = (
            self.db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.id == line.purchase_order_line_id)
            .first()
        )
        if po_line is None or po_line.item_id is None:
            return None, (receipt.location_id if receipt else None)

        item = self.db.query(Item).filter(Item.id == po_line.item_id).first()
        if item is None or not item.is_stocked:
            return None, (receipt.location_id if receipt else None)

        return item.id, (receipt.location_id if receipt else None)

    def _correlation_of(self, line: GoodsReceiptLine):
        receipt = (
            self.db.query(GoodsReceipt)
            .filter(GoodsReceipt.id == line.goods_receipt_id)
            .first()
        )
        return receipt.correlation_id if receipt else None

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
