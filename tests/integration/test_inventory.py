"""Inventory: the ledger, and the control on adjusting it.

Build Book Variant D1. Two things here are worth more than the rest of the
module put together, and both fail silently:

  * **The balance must equal the sum of the ledger.** A maintained aggregate
    that can drift from its source without anybody noticing is worse than no
    aggregate — every valuation, reorder point and stock-accuracy figure is
    computed from it, and a wrong number looks exactly like a right one.
  * **An adjustment is how a theft is covered up.** It is the only way stock
    moves with nothing physical behind it, so the person who counted must not
    be the person who signs, and a large write-off must carry two different
    signatures.
"""
import uuid
from decimal import Decimal

import pytest

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.models.inventory import (
    Item, StockBalance, StockLocation, StockMovement,
    MOVE_ADJUSTMENT, MOVE_RECEIPT, REASON_COUNT_CORRECTION, REASON_DAMAGED,
)
from app.models.inventory_control import (
    ADJ_PENDING_APPROVAL, ADJ_POSTED, ADJ_REJECTED,
)
from app.services.inventory_adjustment_service import InventoryAdjustmentService
from app.services.stock_service import InsufficientStock, StockService

pytestmark = pytest.mark.integration


def _item(db, tenant, sku="SKU-1", cost="10.00", stocked=True, reorder=None):
    item = Item(
        id=uuid.uuid4(), tenant_id=tenant.id, sku=sku, name=f"Item {sku}",
        uom="each", is_stocked=stocked, standard_cost=Decimal(cost),
        reorder_point=Decimal(reorder) if reorder else None,
    )
    db.add(item)
    db.flush()
    return item


def _location(db, tenant, code="MAIN"):
    location = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code=code, name=f"{code} store",
    )
    db.add(location)
    db.flush()
    return location


def _receive(db, tenant, item, location, quantity, user=None):
    return StockService(db).post_movement(
        tenant_id=tenant.id, item_id=item.id, location_id=location.id,
        quantity=quantity, movement_type=MOVE_RECEIPT, current_user=user,
    )


class TestTheLedgerIsTheTruth:
    def test_a_movement_changes_the_balance(self, db, tenant, make_user):
        item, location = _item(db, tenant), _location(db, tenant)

        _receive(db, tenant, item, location, 10)

        assert StockService(db).on_hand(item.id) == Decimal("10")

    def test_movements_accumulate(self, db, tenant, make_user):
        item, location = _item(db, tenant), _location(db, tenant)

        _receive(db, tenant, item, location, 10)
        _receive(db, tenant, item, location, 5)
        StockService(db).post_movement(
            tenant_id=tenant.id, item_id=item.id, location_id=location.id,
            quantity=-3, movement_type=MOVE_ADJUSTMENT,
        )

        assert StockService(db).on_hand(item.id) == Decimal("12")

    def test_the_balance_always_equals_the_sum_of_the_ledger(
        self, db, tenant, make_user
    ):
        """The invariant the whole design rests on. If this can drift, the
        cached number is a liability rather than a shortcut."""
        item, location = _item(db, tenant), _location(db, tenant)
        for quantity in (10, 5, -3, 20, -12, 1):
            StockService(db).post_movement(
                tenant_id=tenant.id, item_id=item.id, location_id=location.id,
                quantity=quantity, movement_type=MOVE_ADJUSTMENT,
            )

        assert StockService(db).reconcile_balances() == []

    def test_drift_is_detected_rather_than_hidden(self, db, tenant, make_user):
        """Something writing a balance without a movement is a bug, and the
        check has to catch it — otherwise the reconciliation is decoration."""
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 10)

        balance = db.query(StockBalance).filter(
            StockBalance.item_id == item.id
        ).one()
        balance.quantity = Decimal("999")
        db.flush()

        discrepancies = StockService(db).reconcile_balances()

        assert len(discrepancies) == 1
        assert discrepancies[0]["ledger"] == 10.0
        assert discrepancies[0]["balance"] == 999.0

    def test_balances_are_kept_per_location(self, db, tenant, make_user):
        item = _item(db, tenant)
        main, annex = _location(db, tenant, "MAIN"), _location(db, tenant, "ANNEX")

        _receive(db, tenant, item, main, 10)
        _receive(db, tenant, item, annex, 4)

        service = StockService(db)
        assert service.on_hand(item.id, main.id) == Decimal("10")
        assert service.on_hand(item.id, annex.id) == Decimal("4")
        assert service.on_hand(item.id) == Decimal("14")

    def test_one_balance_row_per_item_and_location(self, db, tenant, make_user):
        """Enforced by the database. Two rows for one pair would each hold part
        of the stock and every later read would silently pick one."""
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 10)
        _receive(db, tenant, item, location, 10)

        rows = db.query(StockBalance).filter(
            StockBalance.item_id == item.id,
            StockBalance.location_id == location.id,
        ).count()
        assert rows == 1


class TestStockCannotGoNegative:
    def test_a_movement_below_zero_is_refused(self, db, tenant, make_user):
        """A shelf cannot hold minus five things. Allowed through, this does
        not fail — it poisons every valuation downstream and is found during a
        stock count months later."""
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 3)

        with pytest.raises(InsufficientStock, match="cannot be negative"):
            StockService(db).post_movement(
                tenant_id=tenant.id, item_id=item.id, location_id=location.id,
                quantity=-4, movement_type=MOVE_ADJUSTMENT,
            )

    def test_the_refusal_leaves_the_balance_untouched(self, db, tenant, make_user):
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 3)

        with pytest.raises(InsufficientStock):
            StockService(db).post_movement(
                tenant_id=tenant.id, item_id=item.id, location_id=location.id,
                quantity=-4, movement_type=MOVE_ADJUSTMENT,
            )

        assert StockService(db).on_hand(item.id) == Decimal("3")

    def test_going_exactly_to_zero_is_fine(self, db, tenant, make_user):
        """The boundary. Emptying a location is ordinary, and an off-by-one
        here would block a legitimate write-off of the last unit."""
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 3)

        StockService(db).post_movement(
            tenant_id=tenant.id, item_id=item.id, location_id=location.id,
            quantity=-3, movement_type=MOVE_ADJUSTMENT,
        )

        assert StockService(db).on_hand(item.id) == Decimal("0")

    def test_a_non_stocked_item_has_no_balance_to_move(self, db, tenant, make_user):
        """Services and one-off spend are ordered and received but never held.
        This is why receiving alone never implied inventory."""
        item = _item(db, tenant, sku="SERVICE-1", stocked=False)
        location = _location(db, tenant)

        with pytest.raises(ValueError, match="not a stocked item"):
            _receive(db, tenant, item, location, 5)

    def test_a_zero_movement_is_refused(self, db, tenant, make_user):
        item, location = _item(db, tenant), _location(db, tenant)

        with pytest.raises(ValueError, match="changes nothing"):
            _receive(db, tenant, item, location, 0)


class TestAdjustmentsAreControlled:
    """The fraud surface: stock moving with nobody's word but the raiser's."""

    def _adjustment(self, db, tenant, user, item, location, change=-5,
                    reason=REASON_COUNT_CORRECTION):
        return InventoryAdjustmentService(db).create(
            user, location_id=location.id, reason_code=reason,
            lines=[{"item_id": item.id, "quantity_change": change}],
        )

    def test_the_raiser_cannot_approve_their_own(self, db, tenant, make_user):
        """Maker-checker. Writing stock off is how a loss is covered up, so the
        person who counted must not be the person who signs."""
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        # Give the raiser approval rights to isolate the SoD rule from the
        # permission check — otherwise this passes for the wrong reason.
        admin = make_user(UserRole.ADMIN)
        adjustment = self._adjustment(db, tenant, admin, item, location)
        InventoryAdjustmentService(db).submit(adjustment.id, admin)

        with pytest.raises(PermissionError, match="cannot approve it"):
            InventoryAdjustmentService(db).approve(adjustment.id, admin)

    def test_an_admin_gets_no_exemption_from_that(self, db, tenant, make_user):
        """Unlike the invoice approval rules, which exempt admins so a
        one-person demo tenant still works. This control exists precisely for
        the person with the most access."""
        admin = make_user(UserRole.ADMIN)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)
        adjustment = self._adjustment(db, tenant, admin, item, location)
        InventoryAdjustmentService(db).submit(adjustment.id, admin)

        with pytest.raises(PermissionError):
            InventoryAdjustmentService(db).approve(adjustment.id, admin)

    def test_approval_posts_the_movement(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = self._adjustment(db, tenant, clerk, item, location, change=-5)
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)

        db.refresh(adjustment)
        assert adjustment.current_state == ADJ_POSTED
        assert StockService(db).on_hand(item.id) == Decimal("15")

    def test_stock_does_not_move_before_approval(self, db, tenant, make_user):
        """The whole point of the workflow. A draft or submitted adjustment
        that already moved stock would make approval decorative."""
        clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = self._adjustment(db, tenant, clerk, item, location, change=-5)
        assert StockService(db).on_hand(item.id) == Decimal("20")

        service.submit(adjustment.id, clerk)
        assert StockService(db).on_hand(item.id) == Decimal("20")

    def test_a_rejected_adjustment_never_moves_stock(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = self._adjustment(db, tenant, clerk, item, location, change=-5)
        service.submit(adjustment.id, clerk)
        service.reject(adjustment.id, manager, reason="Recount first")

        db.refresh(adjustment)
        assert adjustment.current_state == ADJ_REJECTED
        assert StockService(db).on_hand(item.id) == Decimal("20")

    def test_a_clerk_cannot_approve(self, db, tenant, make_user):
        """The permission split: raising and approving are separate grants, so
        no arrangement of roles can collapse them."""
        clerk = make_user(UserRole.AP_CLERK)
        other_clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = self._adjustment(db, tenant, clerk, item, location)
        service.submit(adjustment.id, clerk)

        with pytest.raises(PermissionError, match="does not have permission"):
            service.approve(adjustment.id, other_clerk)


class TestDualApproval:
    def _big_adjustment(self, db, tenant, user, item, location):
        """Over the threshold: 100 units at 1,000 each is 100k against a 50k
        limit."""
        return InventoryAdjustmentService(db).create(
            user, location_id=location.id, reason_code=REASON_DAMAGED,
            lines=[{"item_id": item.id, "quantity_change": -100}],
        )

    def test_a_large_write_off_needs_two_signatures(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item = _item(db, tenant, cost="1000.00")
        location = _location(db, tenant)
        _receive(db, tenant, item, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = self._big_adjustment(db, tenant, clerk, item, location)
        service.submit(adjustment.id, clerk)
        assert adjustment.requires_dual_approval is True

        service.approve(adjustment.id, manager)

        db.refresh(adjustment)
        assert adjustment.current_state == ADJ_PENDING_APPROVAL
        assert StockService(db).on_hand(item.id) == Decimal("500")

    def test_the_second_signature_posts_it(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        item = _item(db, tenant, cost="1000.00")
        location = _location(db, tenant)
        _receive(db, tenant, item, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = self._big_adjustment(db, tenant, clerk, item, location)
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)
        service.approve(adjustment.id, cfo)

        db.refresh(adjustment)
        assert adjustment.current_state == ADJ_POSTED
        assert StockService(db).on_hand(item.id) == Decimal("400")

    def test_the_same_person_cannot_sign_twice(self, db, tenant, make_user):
        """Otherwise dual approval is one person clicking twice — the control
        failing while appearing to work."""
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item = _item(db, tenant, cost="1000.00")
        location = _location(db, tenant)
        _receive(db, tenant, item, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = self._big_adjustment(db, tenant, clerk, item, location)
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)

        with pytest.raises(PermissionError, match="clicking twice"):
            service.approve(adjustment.id, manager)

    def test_a_small_adjustment_needs_only_one(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item = _item(db, tenant, cost="10.00")
        location = _location(db, tenant)
        _receive(db, tenant, item, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)
        assert adjustment.requires_dual_approval is False

        service.approve(adjustment.id, manager)

        db.refresh(adjustment)
        assert adjustment.current_state == ADJ_POSTED

    def test_the_threshold_is_measured_on_absolute_value(self, db, tenant, make_user):
        """A write-off of 100k and a write-on of 100k are equally worth a second
        signature. Netting them would let one adjustment move a fortune in both
        directions and score as zero."""
        clerk = make_user(UserRole.AP_CLERK)
        item_a = _item(db, tenant, sku="A", cost="1000.00")
        item_b = _item(db, tenant, sku="B", cost="1000.00")
        location = _location(db, tenant)
        _receive(db, tenant, item_a, location, 500)
        _receive(db, tenant, item_b, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[
                {"item_id": item_a.id, "quantity_change": -100},
                {"item_id": item_b.id, "quantity_change": 100},
            ],
        )
        service.submit(adjustment.id, clerk)

        assert adjustment.total_value == Decimal("200000.00")
        assert adjustment.requires_dual_approval is True

    def test_the_requirement_is_fixed_at_submission(self, db, tenant, make_user):
        """Recomputing at approval would let the required approver change if an
        item's cost were edited in between — a way to route a large write-off
        past the second signature without touching the adjustment."""
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item = _item(db, tenant, cost="1000.00")
        location = _location(db, tenant)
        _receive(db, tenant, item, location, 500)

        service = InventoryAdjustmentService(db)
        adjustment = self._big_adjustment(db, tenant, clerk, item, location)
        service.submit(adjustment.id, clerk)

        item.standard_cost = Decimal("0.01")
        db.flush()

        service.approve(adjustment.id, manager)

        db.refresh(adjustment)
        assert adjustment.requires_dual_approval is True
        assert adjustment.current_state == ADJ_PENDING_APPROVAL


class TestTheTrail:
    def test_every_step_is_audited(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)

        actions = [
            row.action for row in db.query(AuditLog).filter(
                AuditLog.object_id == adjustment.id
            ).order_by(AuditLog.timestamp).all()
        ]
        assert actions == ["created", "submitted", "approved", "posted"]

    def test_the_movement_says_what_caused_it(self, db, tenant, make_user):
        """A movement has to explain itself years later, when the record behind
        it may have been withdrawn."""
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_DAMAGED,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)

        movement = db.query(StockMovement).filter(
            StockMovement.source_id == adjustment.id
        ).one()
        assert movement.movement_type == MOVE_ADJUSTMENT
        assert movement.reason_code == REASON_DAMAGED
        assert movement.source_type == "inventory_adjustment"

    def test_the_line_records_what_was_expected(self, db, tenant, make_user):
        """So a count correction reads as "expected 20, found 15" rather than
        as a bare -5 that means nothing on its own."""
        clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        adjustment = InventoryAdjustmentService(db).create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )

        assert adjustment.lines[0].quantity_before == Decimal("20")


class TestValidation:
    def test_an_unknown_reason_code_is_refused(self, db, tenant, make_user):
        """Reason codes are a fixed vocabulary so they can be counted. Free
        text makes "which vendor damages the most goods" unanswerable."""
        clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)

        with pytest.raises(ValueError, match="not a reason code"):
            InventoryAdjustmentService(db).create(
                clerk, location_id=location.id, reason_code="just because",
                lines=[{"item_id": item.id, "quantity_change": -1}],
            )

    def test_an_adjustment_with_no_lines_is_refused(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        location = _location(db, tenant)

        with pytest.raises(ValueError, match="changes nothing"):
            InventoryAdjustmentService(db).create(
                clerk, location_id=location.id,
                reason_code=REASON_COUNT_CORRECTION, lines=[],
            )

    def test_a_posted_adjustment_cannot_be_cancelled(self, db, tenant, make_user):
        """Posted stock is reversed with an opposing adjustment, not by
        withdrawing the record that moved it — otherwise the ledger and its
        paperwork disagree."""
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 20)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)
        service.approve(adjustment.id, manager)

        with pytest.raises(ValueError, match="cannot be cancelled"):
            service.cancel(adjustment.id, clerk)


class TestReceivingPutsStockOnTheShelf:
    """The integration that makes receiving mean something.

    Before inventory existed a receipt was a statement that a quantity arrived
    against an order line, with nowhere for it to arrive *to*. Now goods land
    somewhere — but only when the line names a stocked item and the receipt
    names a location, which keeps services and every pre-inventory receipt
    behaving exactly as they did.
    """

    def _issued_order(self, db, tenant, user, item=None, quantity=10):
        from datetime import date
        from app.core.enums import PurchaseOrderState, VendorStatus
        from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
        from app.models.vendor import Vendor

        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id,
            legal_name=f"V-{uuid.uuid4().hex[:6]}",
            status=VendorStatus.ACTIVE, created_by=user["id"],
        )
        db.add(vendor)
        order = PurchaseOrder(
            id=uuid.uuid4(), tenant_id=tenant.id,
            po_number=f"PO-{uuid.uuid4().hex[:6]}", vendor_id=vendor.id,
            vendor_name=vendor.legal_name, order_date=date(2026, 8, 1),
            total_amount=1000, current_state=PurchaseOrderState.ISSUED,
            created_by=user["id"], correlation_id=uuid.uuid4(),
        )
        db.add(order)
        db.flush()
        line = PurchaseOrderLine(
            id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
            line_number=1, description="Widgets", quantity=quantity,
            unit_price=100, amount=100 * quantity,
            item_id=item.id if item else None,
        )
        db.add(line)
        db.flush()
        return order, line

    def _receive(self, db, order, line, quantity, user, location=None):
        from types import SimpleNamespace
        from app.services.goods_receipt_service import GoodsReceiptService

        payload = SimpleNamespace(
            received_date=None, delivery_note=None, notes=None,
            location_id=location.id if location else None,
            lines=[SimpleNamespace(
                purchase_order_line_id=line.id, quantity_received=quantity,
            )],
        )
        return GoodsReceiptService(db).record_receipt(order.id, payload, user)

    def test_a_receipt_lands_stock_at_the_named_location(
        self, db, tenant, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        item = _item(db, tenant)
        bay = _location(db, tenant, "BAY")
        order, line = self._issued_order(db, tenant, clerk, item=item)

        self._receive(db, order, line, 7, clerk, location=bay)

        assert StockService(db).on_hand(item.id, bay.id) == Decimal("7")

    def test_the_movement_points_back_at_the_receipt(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        item = _item(db, tenant)
        bay = _location(db, tenant, "BAY")
        order, line = self._issued_order(db, tenant, clerk, item=item)

        receipt = self._receive(db, order, line, 7, clerk, location=bay)

        movement = db.query(StockMovement).filter(
            StockMovement.source_id == receipt.id
        ).one()
        assert movement.movement_type == MOVE_RECEIPT
        assert movement.correlation_id == order.correlation_id

    def test_a_line_with_no_item_moves_nothing(self, db, tenant, make_user):
        """Services and ad-hoc spend. The receipt is still recorded — what
        arrived still arrived — it simply never reaches a shelf."""
        clerk = make_user(UserRole.AP_CLERK)
        bay = _location(db, tenant, "BAY")
        order, line = self._issued_order(db, tenant, clerk, item=None)

        receipt = self._receive(db, order, line, 7, clerk, location=bay)

        assert receipt is not None
        assert db.query(StockMovement).filter(
            StockMovement.source_id == receipt.id
        ).count() == 0

    def test_a_non_stocked_item_moves_nothing(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        item = _item(db, tenant, sku="SERVICE-9", stocked=False)
        bay = _location(db, tenant, "BAY")
        order, line = self._issued_order(db, tenant, clerk, item=item)

        receipt = self._receive(db, order, line, 7, clerk, location=bay)

        assert db.query(StockMovement).filter(
            StockMovement.source_id == receipt.id
        ).count() == 0

    def test_with_no_location_anywhere_nothing_is_invented(
        self, db, tenant, make_user
    ):
        """A receipt that silently lands stock in an arbitrary location is
        worse than one that lands none: the first is wrong everywhere
        downstream, the second is visibly incomplete."""
        clerk = make_user(UserRole.AP_CLERK)
        item = _item(db, tenant)
        order, line = self._issued_order(db, tenant, clerk, item=item)

        receipt = self._receive(db, order, line, 7, clerk, location=None)

        assert receipt.location_id is None
        assert StockService(db).on_hand(item.id) == Decimal("0")

    def test_the_receiving_bay_is_used_when_none_is_named(
        self, db, tenant, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        item = _item(db, tenant)
        bay = _location(db, tenant, "BAY")
        bay.is_receiving_bay = True
        db.flush()
        order, line = self._issued_order(db, tenant, clerk, item=item)

        self._receive(db, order, line, 7, clerk, location=None)

        assert StockService(db).on_hand(item.id, bay.id) == Decimal("7")


class TestTheInventoryWorkflowsHaveAClock:
    """The gap that nearly shipped.

    Migration 038 gave adjustments and returns their states, and the config
    defaults gave those states SLA settings. None of it did anything: the
    escalation runner scans models declaring `WORKFLOW_TYPE`, which neither
    declared, and `sla_status` computes a deadline from `state_entered_at`,
    which neither had. So both sat outside every clock in the system — the
    inbox showed them as never overdue and nothing would ever have escalated
    them.

    That is the same failure the register already records twice (DR-009 for
    escalations generally, DR-037 for tenders), arriving a third time in a new
    module. These tests are what make it visible the next time.
    """

    def _overdue_adjustment(self, db, tenant, make_user):
        from datetime import timedelta
        from app.services.config_provisioning import ConfigProvisioningService
        from app.utils.datetime_helpers import utc_now

        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)

        clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 50)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)

        # Waiting far longer than the 24 hours its pending state allows.
        adjustment.state_entered_at = utc_now() - timedelta(hours=100)
        db.flush()
        return admin, adjustment

    def test_the_escalation_runner_scans_both_new_workflows(self):
        """Declaring WORKFLOW_TYPE is what puts a module in front of the
        runner. Without it the SLA config is decoration."""
        from app.services.workflow import workflow_models

        scanned = set(workflow_models())

        assert "inventory_adjustment" in scanned
        assert "vendor_return" in scanned

    def test_an_overdue_adjustment_actually_escalates(self, db, tenant, make_user):
        from app.services.sla_service import SlaService

        admin, adjustment = self._overdue_adjustment(db, tenant, make_user)

        result = SlaService(db).run_escalations(admin)

        assert any(
            row["object_type"] == "inventory_adjustment"
            for row in result["items"]
        ), f"nothing escalated: {result}"

    def test_the_timer_restarts_when_the_state_changes(self, db, tenant, make_user):
        """Build Book: SLA timers start when a task enters a state. Measuring
        from creation instead would make a record that moved quickly through
        three states look overdue on arrival at the fourth."""
        clerk = make_user(UserRole.AP_CLERK)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 50)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        before = adjustment.state_entered_at

        service.submit(adjustment.id, clerk)

        assert adjustment.state_entered_at is not None
        assert before is None or adjustment.state_entered_at >= before

    def test_the_inbox_can_see_a_deadline(self, db, tenant, make_user):
        """Without state_entered_at the inbox computes no due date at all, so
        every one of these reads as "never overdue" — which is indistinguishable
        from being on time."""
        from app.services.decision_inbox_service import DecisionInboxService

        admin, adjustment = self._overdue_adjustment(db, tenant, make_user)

        items = DecisionInboxService(db).get_inbox(admin)["items"]
        row = next(
            (i for i in items if i["object_id"] == adjustment.id), None
        )

        assert row is not None
        assert row["sla_due_at"] is not None
        assert row["overdue"] is True

    def test_an_approver_is_told_when_one_arrives(self, db, tenant, make_user):
        """Not only when it breaches. An approver whose first message about a
        write-off is "this is late" makes the escalation meaningless as a
        signal — the failure DR-037 fixed for every other module."""
        from app.models.notification_outbox import NotificationOutbox

        clerk = make_user(UserRole.AP_CLERK)
        make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 50)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        before = db.query(NotificationOutbox).count()

        service.submit(adjustment.id, clerk)

        assert db.query(NotificationOutbox).count() > before

    def test_the_raiser_is_not_told_about_their_own(self, db, tenant, make_user):
        """They cannot approve it, so a message asking them to is noise that
        teaches people to ignore the queue."""
        from app.models.notification_outbox import NotificationOutbox
        from app.models.user import User

        clerk = make_user(UserRole.AP_CLERK)
        make_user(UserRole.MANAGER)
        item, location = _item(db, tenant), _location(db, tenant)
        _receive(db, tenant, item, location, 50)

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            clerk, location_id=location.id, reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, clerk)

        clerk_row = db.query(User).filter(User.id == clerk["id"]).one()
        recipients = {
            row.user_id for row in db.query(NotificationOutbox).all()
        }
        assert clerk_row.id not in recipients
