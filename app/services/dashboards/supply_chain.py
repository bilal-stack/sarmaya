"""COO and Supply Chain.

Variant D's stock accuracy, supplier delivery performance and
receipt-to-invoice latency, plus inventory turns and the whole
purchase-to-pay run. The last of those is the only report in the system
that follows one purchase across all five modules.
"""
from datetime import timedelta
from typing import Dict, List

from sqlalchemy import func, tuple_

from app.core.roles import has_permission
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.inventory import (
    MOVE_ISSUE, Item, StockBalance, StockMovement,
)
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    DashboardBase, _bucket, _median_days,
    _now,
)


class SupplyChainReports(DashboardBase):
    # --- Variant D: supply chain --------------------------------------------
    #
    # Build Book D1 asks for three: stock accuracy and adjustment rate,
    # supplier delivery performance and lead time adherence, and GRN-to-invoice
    # latency with its impact on AP. All three are computed from what the
    # ledger and the receipts already record — this is reporting on existing
    # truth, not new plumbing.

    def stock_accuracy(self, current_user: dict, days: int = 90) -> Dict:
        """How often the count disagrees with the system, and by how much.

        The headline is the *write-off* rate rather than the net. Netting a
        write-on against a write-off is how a loss disappears into an
        arithmetic average: two adjustments that cancel out are not a warehouse
        in good order, they are two discrepancies.
        """
        self._require(current_user)
        from app.models.inventory import (
            Item, StockBalance, StockMovement, MOVE_ADJUSTMENT,
            REASON_THEFT_OR_LOSS, REASON_COUNT_CORRECTION,
        )
        from app.models.inventory_control import InventoryAdjustment, ADJ_POSTED

        since = _now() - timedelta(days=days)

        adjustments = (
            self.db.query(InventoryAdjustment)
            .filter(
                InventoryAdjustment.current_state == ADJ_POSTED,
                InventoryAdjustment.posted_at >= since,
            )
            .all()
        )

        by_reason: Dict[str, Dict] = {}
        written_off = written_on = 0.0
        for adjustment in adjustments:
            value = money_to_float(adjustment.total_value or 0)
            direction = sum(
                float(line.quantity_change) for line in adjustment.lines
            )
            bucket = by_reason.setdefault(
                adjustment.reason_code,
                {"reason": adjustment.reason_code, "count": 0, "value": 0.0},
            )
            bucket["count"] += 1
            bucket["value"] = round(bucket["value"] + value, 2)
            if direction < 0:
                written_off += value
            else:
                written_on += value

        # The denominator: what is on hand, valued. An adjustment rate needs
        # something to be a rate *of*, and "adjustments per month" says nothing
        # about a warehouse that doubled in size.
        holding = (
            self.db.query(
                func.coalesce(
                    func.sum(StockBalance.quantity * Item.standard_cost), 0
                )
            )
            .join(Item, Item.id == StockBalance.item_id)
            .scalar()
        ) or 0
        holding_value = money_to_float(holding)

        movement_count = (
            self.db.query(func.count(StockMovement.id))
            .filter(StockMovement.created_at >= since)
            .scalar()
        ) or 0
        adjustment_movements = (
            self.db.query(func.count(StockMovement.id))
            .filter(
                StockMovement.created_at >= since,
                StockMovement.movement_type == MOVE_ADJUSTMENT,
            )
            .scalar()
        ) or 0

        # Loss and theft called out separately: these are the reasons that mean
        # something left without anybody selling it, and burying them in a
        # total is how they stop being noticed.
        unexplained = round(sum(
            row["value"] for code, row in by_reason.items()
            if code == REASON_THEFT_OR_LOSS
        ), 2)

        return {
            "window_days": days,
            "adjustments_posted": len(adjustments),
            "value_written_off": round(written_off, 2),
            "value_written_on": round(written_on, 2),
            "unexplained_loss_value": unexplained,
            "holding_value": holding_value,
            "write_off_rate_percent": (
                round(written_off / holding_value * 100, 2)
                if holding_value else 0.0
            ),
            "adjustment_share_of_movements_percent": (
                round(adjustment_movements / movement_count * 100, 2)
                if movement_count else 0.0
            ),
            "count_corrections": sum(
                row["count"] for code, row in by_reason.items()
                if code == REASON_COUNT_CORRECTION
            ),
            "by_reason": sorted(by_reason.values(), key=lambda r: -r["value"]),
        }

    def supplier_delivery_performance(
        self, current_user: dict, days: int = 180
    ) -> Dict:
        """Who delivers on time, in full, and undamaged.

        Three separate questions, deliberately not averaged into one score. A
        supplier who is always late but never wrong needs a different
        conversation from one who is punctual and sends damaged goods, and a
        single number hides which of them you have.
        """
        self._require(current_user)
        from app.models.goods_receipt import GoodsReceipt
        from app.models.inventory_control import VendorReturn
        from app.models.purchase_order import PurchaseOrder

        since = (_now() - timedelta(days=days)).date()

        rows = (
            self.db.query(
                PurchaseOrder.vendor_name,
                PurchaseOrder.expected_date,
                GoodsReceipt.received_date,
                GoodsReceipt.id,
            )
            .join(GoodsReceipt, GoodsReceipt.purchase_order_id == PurchaseOrder.id)
            .filter(GoodsReceipt.received_date >= since)
            .all()
        )

        by_vendor: Dict[str, Dict] = {}
        for vendor_name, expected, received, _receipt_id in rows:
            bucket = by_vendor.setdefault(vendor_name, {
                "vendor": vendor_name, "deliveries": 0, "on_time": 0,
                "late": 0, "unknown_due_date": 0, "total_days_late": 0,
                "returns_their_fault": 0,
            })
            bucket["deliveries"] += 1

            if expected is None:
                # No promised date means on-time is unanswerable. Counted
                # separately rather than assumed on time, which would flatter
                # every vendor whose orders never carried a date.
                bucket["unknown_due_date"] += 1
            elif received and received > expected:
                bucket["late"] += 1
                bucket["total_days_late"] += (received - expected).days
            else:
                bucket["on_time"] += 1

        attributable = dict(
            self.db.query(VendorReturn.vendor_id, func.count(VendorReturn.id))
            .filter(VendorReturn.vendor_attributable.is_(True))
            .group_by(VendorReturn.vendor_id)
            .all()
        )
        if attributable:
            for vendor in (
                self.db.query(Vendor)
                .filter(Vendor.id.in_(list(attributable)))
                .all()
            ):
                if vendor.legal_name in by_vendor:
                    by_vendor[vendor.legal_name]["returns_their_fault"] = (
                        attributable[vendor.id]
                    )

        vendors = []
        for row in by_vendor.values():
            measurable = row["on_time"] + row["late"]
            vendors.append({
                **row,
                "on_time_percent": (
                    round(row["on_time"] / measurable * 100, 1)
                    if measurable else None
                ),
                "average_days_late": (
                    round(row["total_days_late"] / row["late"], 1)
                    if row["late"] else 0.0
                ),
            })

        # Worst first, with the unmeasurable ones last: a vendor nobody can
        # score is a data problem, not a performance problem, and putting them
        # at the top would bury the suppliers actually failing.
        vendors.sort(
            key=lambda v: (v["on_time_percent"] is None, v["on_time_percent"] or 0)
        )

        return {
            "window_days": days,
            "vendors": vendors,
            "deliveries": sum(v["deliveries"] for v in vendors),
            "late_deliveries": sum(v["late"] for v in vendors),
            "deliveries_with_no_promised_date": sum(
                v["unknown_due_date"] for v in vendors
            ),
        }

    def receipt_to_invoice_latency(self, current_user: dict, days: int = 90) -> Dict:
        """How long goods sit received but uninvoiced, and what it is worth.

        The Build Book calls this "GRN to invoice latency and impact on AP",
        and the impact is the point: goods received without an invoice are a
        liability the ledger does not show yet. A month-end that misses them
        understates what is owed, which is the accrual an auditor asks about.
        """
        self._require(current_user)
        from app.models.goods_receipt import GoodsReceipt, GoodsReceiptLine
        from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine

        since = (_now() - timedelta(days=days)).date()
        today = _now().date()

        invoiced_orders = {
            row[0] for row in
            self.db.query(Invoice.purchase_order_id)
            .filter(Invoice.purchase_order_id.isnot(None))
            .all()
        }

        rows = (
            self.db.query(
                GoodsReceipt.grn_number,
                GoodsReceipt.received_date,
                PurchaseOrder.id,
                PurchaseOrder.vendor_name,
                func.sum(
                    GoodsReceiptLine.quantity_received * PurchaseOrderLine.unit_price
                ),
            )
            .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
            .join(
                GoodsReceiptLine,
                GoodsReceiptLine.goods_receipt_id == GoodsReceipt.id,
            )
            .join(
                PurchaseOrderLine,
                PurchaseOrderLine.id == GoodsReceiptLine.purchase_order_line_id,
            )
            .filter(GoodsReceipt.received_date >= since)
            .group_by(
                GoodsReceipt.id, GoodsReceipt.grn_number,
                GoodsReceipt.received_date, PurchaseOrder.id,
                PurchaseOrder.vendor_name,
            )
            .all()
        )

        waiting, buckets, total_value = [], {}, 0.0
        for grn, received_date, order_id, vendor, value in rows:
            if order_id in invoiced_orders:
                continue
            age = (today - received_date).days if received_date else 0
            amount = money_to_float(value or 0)
            total_value += amount
            bucket = _bucket(age)
            buckets[bucket] = buckets.get(bucket, 0) + 1
            waiting.append({
                "grn_number": grn,
                "vendor": vendor,
                "received_date": received_date,
                "days_waiting": age,
                "value": amount,
                "link": f"/ai-tools/purchase-orders/{order_id}",
            })

        waiting.sort(key=lambda r: -r["days_waiting"])

        return {
            "window_days": days,
            "receipts_awaiting_invoice": len(waiting),
            "value_awaiting_invoice": round(total_value, 2),
            "by_age": [
                {"bucket": name, "count": count} for name, count in buckets.items()
            ],
            "oldest": waiting[:20],
        }


    # --- COO / Supply Chain: turns, and the whole purchase-to-pay run --------

    def _require_inventory(self, current_user: dict) -> None:
        """Stock reports read with inventory.view."""
        from app.core.roles import PERM_VIEW_INVENTORY

        if not has_permission(current_user["role"], PERM_VIEW_INVENTORY):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view inventory reports"
            )

    def inventory_turns(self, current_user: dict, days: int = 365) -> Dict:
        """How many times the stock turned over, and what could not be valued.

        Build Book, COO / Supply Chain: "inventory turns."

        Turns are cost of goods sold divided by average inventory value. Both
        halves are computed from the movement ledger rather than from a stored
        valuation, which is what makes the average honest:

          * COGS is the value of everything issued in the window. `issue` is
            the movement type for stock consumed — receipts, transfers and
            adjustments are not sales and are excluded, because counting a
            transfer between two of our own locations as a turn would let a
            warehouse improve this figure by shuffling boxes.
          * Average inventory is the mean of the opening and closing value.
            The opening balance is *reconstructed exactly* — current balance
            minus the net of every movement in the window — which is possible
            only because the ledger is append-only and never edited. A report
            that used closing value alone would flatter a business that had
            just run its stock down, and understate one that had just built up
            for a season.

        **Items with no standard_cost cannot be valued**, so the whole report
        describes the costed subset of the warehouse, and `uncosted` says how
        much is outside it.

        Worth being precise about why, because the obvious argument is wrong:
        costing an unknown item at zero would *not* distort the ratio. Zero
        contributes nothing to COGS and nothing to inventory value, so the
        quotient is identical either way — excluding and zeroing produce the
        same number. The problem is not manipulation, it is representativeness.
        A ratio computed over the costed items is only a fact about the
        business to the extent that most of the business is costed, and a
        reader cannot judge that without being told. Hence `uncosted`, and
        hence excluding rather than zeroing: the two give the same answer, and
        only one of them leaves a trace that the answer was partial.
        """
        self._require_inventory(current_user)
        since = _now() - timedelta(days=days)

        costs = dict(
            self.db.query(Item.id, Item.standard_cost)
            .filter(Item.standard_cost.isnot(None)).all()
        )
        uncosted_ids = [
            row[0] for row in
            self.db.query(Item.id).filter(Item.standard_cost.is_(None)).all()
        ]

        # Closing position, as the balances stand now.
        balances = self.db.query(
            StockBalance.item_id, func.sum(StockBalance.quantity)
        ).group_by(StockBalance.item_id).all()

        # Everything that moved inside the window, per item. Signed, so the
        # sum is the net change and subtracting it from the closing balance
        # gives the opening one exactly.
        net_movement = dict(
            self.db.query(StockMovement.item_id, func.sum(StockMovement.quantity))
            .filter(StockMovement.created_at >= since)
            .group_by(StockMovement.item_id).all()
        )
        issued = dict(
            self.db.query(StockMovement.item_id, func.sum(StockMovement.quantity))
            .filter(
                StockMovement.created_at >= since,
                StockMovement.movement_type == MOVE_ISSUE,
            )
            .group_by(StockMovement.item_id).all()
        )

        closing_value = 0.0
        opening_value = 0.0
        cogs = 0.0
        uncosted_units = 0.0

        for item_id, quantity in balances:
            units = float(quantity or 0)
            cost = costs.get(item_id)
            if cost is None:
                uncosted_units += units
                continue
            cost = money_to_float(cost)
            closing_value += units * cost
            opening_value += (units - float(net_movement.get(item_id, 0) or 0)) * cost

        for item_id, quantity in issued.items():
            cost = costs.get(item_id)
            if cost is not None:
                # Issues are negative; the value consumed is their magnitude.
                cogs += abs(float(quantity or 0)) * money_to_float(cost)

        average_value = (opening_value + closing_value) / 2.0
        # Annualised, so a 90-day window and a year are comparable. Turns is
        # conventionally a yearly figure and reporting a 90-day raw ratio
        # under the same name would read as a business turning over four times
        # more slowly than it is.
        turns = (
            round((cogs / average_value) * (365.0 / days), 2)
            if average_value > 0 else None
        )

        return {
            "window_days": days,
            "since": since.date().isoformat(),
            "cogs": round(cogs, 2),
            "opening_value": round(opening_value, 2),
            "closing_value": round(closing_value, 2),
            "average_value": round(average_value, 2),
            # None rather than 0.0 when there is no stock to turn: zero turns
            # reads as stock sitting dead, which is a different problem from
            # having none.
            "turns_per_year": turns,
            "days_of_stock": (
                round(365.0 / turns, 1) if turns and turns > 0 else None
            ),
            # How much of the warehouse the figures above do not cover.
            # See the docstring: the ratio is the same whether these are
            # excluded or zeroed, so this block is the disclosure, not a
            # defence against a distorted number.
            "uncosted": {
                "item_count": len(uncosted_ids),
                "units_on_hand": round(uncosted_units, 3),
            },
        }

    #: Purchase-to-pay, end to end. Each hop is a handover between two teams,
    #: which is where elapsed time actually accumulates — the work inside a
    #: step is rarely the problem.
    P2P_STEPS = [
        ("requisition_to_po", "Requisition approved to PO raised",
         "requisition", "approved", "purchase_order", "created"),
        ("po_to_receipt", "PO raised to goods received",
         "purchase_order", "created", "goods_receipt", "goods_received"),
        ("receipt_to_invoice", "Goods received to invoice entered",
         "goods_receipt", "goods_received", "invoice", "created"),
        ("invoice_to_approval", "Invoice entered to approved",
         "invoice", "created", "invoice", "approved"),
        ("approval_to_payment", "Invoice approved to payment released",
         "invoice", "approved", "payment", "released"),
    ]

    def p2p_cycle_time(self, current_user: dict, days: int = 180) -> Dict:
        """The whole run from "we need this" to "they were paid".

        Build Book, COO / Supply Chain: "P2P cycle."

        Every other cycle-time report in this system measures inside one
        module. This one is the only report that follows a single purchase
        across all five, and it can exist because every record in the chain
        carries the same `correlation_id` — the thing that makes an evidence
        pack possible makes this possible too.

        Reported per hop rather than end to end. An end-to-end median is a
        number nobody owns: it is the sum of five handovers between different
        teams, and the only actionable question is which handover is the slow
        one. The total is reported alongside, but as the sum of the parts
        rather than instead of them.

        A chain is only counted for a hop where both of its events exist, so a
        purchase still in flight contributes to the hops it has completed and
        to none after. Treating a missing end as "now" would make every
        in-flight purchase look like a delay and make the figure grow whenever
        business picked up.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        wanted = {(t, a) for _, _, t, a, _, _ in self.P2P_STEPS}
        wanted |= {(t, a) for _, _, _, _, t, a in self.P2P_STEPS}

        rows = (
            self.db.query(
                AuditLog.correlation_id, AuditLog.object_type, AuditLog.action,
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.correlation_id.isnot(None),
                AuditLog.timestamp >= since,
                tuple_(AuditLog.object_type, AuditLog.action).in_(wanted),
            )
            .group_by(AuditLog.correlation_id, AuditLog.object_type, AuditLog.action)
            .all()
        )

        chains: Dict[str, Dict[tuple, object]] = {}
        for correlation_id, object_type, action, at in rows:
            chains.setdefault(str(correlation_id), {})[(object_type, action)] = at

        durations: Dict[str, List[float]] = {k: [] for k, _, _, _, _, _ in self.P2P_STEPS}
        for marks in chains.values():
            for key, _label, from_type, from_action, to_type, to_action in self.P2P_STEPS:
                start = marks.get((from_type, from_action))
                end = marks.get((to_type, to_action))
                if start and end:
                    hours = (end - start).total_seconds() / 3600.0
                    if hours >= 0:
                        durations[key].append(hours)

        steps = [
            {
                "step": key,
                "label": label,
                "from": f"{from_type}.{from_action}",
                "to": f"{to_type}.{to_action}",
                "count": len(durations[key]),
                "median_days": _median_days(durations[key]),
                "worst_days": (
                    round(max(durations[key]) / 24.0, 1) if durations[key] else None
                ),
            }
            for key, label, from_type, from_action, to_type, to_action
            in self.P2P_STEPS
        ]

        measured = [s["median_days"] for s in steps if s["median_days"] is not None]
        return {
            "window_days": days,
            "chains_seen": len(chains),
            "steps": steps,
            # The sum of the medians, not the median of the totals. Stated
            # plainly because they are different numbers and the difference
            # matters: no single purchase necessarily took this long, and the
            # figure is here to be read as "the typical run, hop by hop".
            "typical_total_days": round(sum(measured), 1) if measured else None,
            "slowest_step": (
                max(
                    (s for s in steps if s["median_days"] is not None),
                    key=lambda s: s["median_days"], default=None,
                ) or {}
            ).get("step"),
        }
