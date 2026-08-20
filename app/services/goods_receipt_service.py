"""Recording what arrived against a purchase order.

Receiving is deliberately its own permission (purchase_orders.receive) held by
the clerk who raises orders but not by the people who approve them. Whoever
confirms goods arrived should not also be the person who authorised the spend,
or the delivery leg of the three-way match verifies nothing.

Only an issued order can be received against: goods cannot arrive for an order
the vendor was never sent.
"""
import logging
from decimal import Decimal
from typing import List
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.goods_receipt import GoodsReceipt, GoodsReceiptLine
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.core.enums import PurchaseOrderState
from app.core.roles import has_permission, PERM_RECEIVE_GOODS, PERM_VIEW_PO
from app.models.inventory import Item, StockLocation, MOVE_RECEIPT
from app.services.audit import log_audit
from app.services.stock_service import StockService
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "goods_receipt"


class GoodsReceiptService:
    def __init__(self, db: Session):
        self.db = db

    def list_for_order(self, po_id: UUID, current_user: dict) -> List[GoodsReceipt]:
        self._require(current_user, PERM_VIEW_PO, "view goods receipts")

        # Check the order is visible before listing anything against it.
        # Without this the query still returned nothing for another tenant's
        # order — but only because the receipts themselves are tenant-scoped,
        # which makes the isolation incidental rather than stated. A cross-
        # tenant probe found this returning 200 [] where every sibling endpoint
        # returns 404, and an endpoint that never looks at its parent is one
        # refactor away from being a real leak.
        order = self.db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
        if not order:
            raise ValueError("Purchase order not found")

        return (
            self.db.query(GoodsReceipt)
            .filter(GoodsReceipt.purchase_order_id == po_id)
            .order_by(GoodsReceipt.received_date.asc())
            .all()
        )

    def record_receipt(self, po_id: UUID, data, current_user: dict) -> GoodsReceipt:
        self._require(current_user, PERM_RECEIVE_GOODS, "record goods receipts")

        order = (
            self.db.query(PurchaseOrder)
            .filter(PurchaseOrder.id == po_id)
            .first()
        )
        if not order:
            raise ValueError("Purchase order not found")

        state = str(getattr(order.current_state, "value", order.current_state)).lower()
        if state != PurchaseOrderState.ISSUED.value:
            raise ValueError(
                f"Cannot receive against a purchase order in {state} state; "
                "goods cannot arrive for an order the vendor was never sent."
            )

        lines_by_id = {
            line.id: line
            for line in self.db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.purchase_order_id == order.id)
            .all()
        }
        if not data.lines:
            raise ValueError("A goods receipt must record at least one line")

        receipt = GoodsReceipt(
            tenant_id=current_user["tenant_id"],
            purchase_order_id=order.id,
            grn_number=self._next_grn_number(),
            received_date=data.received_date or utc_now().date(),
            location_id=getattr(data, "location_id", None) or self._default_location(),
            delivery_note=data.delivery_note,
            notes=data.notes,
            # Inherited, so the receipt lands in the order's story rather than
            # starting one of its own.
            correlation_id=order.correlation_id,
            received_by=current_user["id"],
        )
        self.db.add(receipt)
        self.db.flush()

        recorded = []
        for index, entry in enumerate(data.lines, start=1):
            line = lines_by_id.get(entry.purchase_order_line_id)
            if line is None:
                raise ValueError(
                    "Receipt line does not belong to this purchase order"
                )

            quantity = Decimal(entry.quantity_received)
            self.db.add(GoodsReceiptLine(
                tenant_id=current_user["tenant_id"],
                goods_receipt_id=receipt.id,
                purchase_order_line_id=line.id,
                line_number=index,
                quantity_received=quantity,
            ))
            # The running total on the order line is what the match reads, so
            # it never has to replay every receipt.
            line.received_quantity = Decimal(line.received_quantity or 0) + quantity
            self.db.add(line)

            # Goods that are actually held now land on a shelf. A line with no
            # item, a non-stocked item, or a receipt with nowhere to put it
            # moves nothing — which is the ordinary case for services and for
            # every receipt recorded before inventory existed.
            self._post_to_stock(receipt, line, quantity, current_user)
            recorded.append({
                "po_line": line.line_number,
                "description": line.description,
                "quantity": float(quantity),
                "received_to_date": float(line.received_quantity),
                "ordered": float(line.quantity),
            })

        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=receipt.id,
            action="goods_received",
            after_value={
                "grn_number": receipt.grn_number,
                "purchase_order": order.po_number,
                "lines": recorded,
            },
        )
        # The receipt also belongs on the order's own trail, so someone reading
        # the order sees the delivery without having to look elsewhere.
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="purchase_order",
            object_id=order.id,
            action="goods_received",
            workflow_type="purchase_order",
            comment=f"Receipt {receipt.grn_number} recorded against this order.",
            after_value={"grn_number": receipt.grn_number, "lines": recorded},
        )

        self.db.commit()
        self.db.refresh(receipt)
        return receipt

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _default_location(self):
        """The tenant's receiving bay, if one is configured.

        Not invented when missing: a receipt that silently lands stock in an
        arbitrary location is worse than one that lands none, because the
        first is wrong everywhere downstream and the second is visibly
        incomplete.
        """
        bay = (
            self.db.query(StockLocation)
            .filter(
                StockLocation.is_receiving_bay.is_(True),
                StockLocation.is_active.is_(True),
            )
            .order_by(StockLocation.code)
            .first()
        )
        return bay.id if bay else None

    def _post_to_stock(self, receipt, po_line, quantity, current_user) -> None:
        """Put the delivered quantity on the shelf.

        Inside the caller's transaction on purpose: a receipt whose stock
        movement rolled back would claim goods arrived that the ledger has
        never heard of, and the two would disagree forever.
        """
        if receipt.location_id is None or po_line.item_id is None:
            return

        item = self.db.query(Item).filter(Item.id == po_line.item_id).first()
        if item is None or not item.is_stocked:
            return
        if quantity == 0:
            return

        StockService(self.db).post_movement(
            tenant_id=receipt.tenant_id,
            item_id=item.id,
            location_id=receipt.location_id,
            quantity=quantity,
            movement_type=MOVE_RECEIPT,
            current_user=current_user,
            source_type=OBJECT_TYPE,
            source_id=receipt.id,
            correlation_id=receipt.correlation_id,
        )

    def _next_grn_number(self) -> str:
        existing = self.db.query(GoodsReceipt).count()
        return f"GRN-{existing + 1:05d}"
