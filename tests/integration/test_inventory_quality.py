"""Quality checks, putaway, and returns to the vendor.

Build Book Variant D1. The parts worth testing hardest are the ones that are
about accountability rather than arithmetic:

  * A rejection must carry a reason code and a note, or "27 units rejected" is
    unusable when somebody later asks which supplier keeps sending damaged
    goods — which is the entire reason reason codes exist.
  * Whether a return is the vendor's fault is decided once, at creation, and
    stored. A scorecard that recomputes it would silently rewrite last quarter
    the moment the definition changed, and a supplier disputing their score is
    exactly when the number must not move.
  * Stock leaves on dispatch, not on approval. The goods are still on the
    premises until they physically go.
"""
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.enums import PurchaseOrderState, UserRole, VendorStatus
from app.models.goods_receipt import GoodsReceiptLine
from app.models.inventory import (
    Item, StockLocation, REASON_COUNT_CORRECTION, REASON_DAMAGED,
)
from app.models.inventory_control import (
    QC_FAILED, QC_PARTIAL, QC_PASSED,
    RET_APPROVED, RET_CREDITED, RET_DISPATCHED, RET_PENDING_APPROVAL,
)
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.services.goods_receipt_service import GoodsReceiptService
from app.services.quality_check_service import QualityCheckService
from app.services.stock_service import StockService
from app.services.vendor_return_service import VendorReturnService

pytestmark = pytest.mark.integration


@pytest.fixture
def warehouse(db, tenant):
    """A receiving bay, a shelf, and a quarantine bin."""
    bay = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code="BAY", name="Receiving bay",
        is_receiving_bay=True,
    )
    shelf = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code="SHELF", name="Main shelf",
    )
    quarantine = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code="QUAR", name="Quarantine",
        is_quarantine=True,
    )
    db.add_all([bay, shelf, quarantine])
    db.flush()
    return SimpleNamespace(bay=bay, shelf=shelf, quarantine=quarantine)


@pytest.fixture
def delivery(db, tenant, warehouse, make_user):
    """20 units of a stocked item, received into the bay."""
    clerk = make_user(UserRole.AP_CLERK)
    item = Item(
        id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-QC", name="Widget",
        uom="each", standard_cost=Decimal("100.00"),
    )
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Delivery Co",
        status=VendorStatus.ACTIVE, created_by=clerk["id"],
    )
    db.add_all([item, vendor])
    db.flush()

    order = PurchaseOrder(
        id=uuid.uuid4(), tenant_id=tenant.id, po_number=f"PO-{uuid.uuid4().hex[:6]}",
        vendor_id=vendor.id, vendor_name=vendor.legal_name,
        order_date=date(2026, 8, 1), total_amount=2000,
        current_state=PurchaseOrderState.ISSUED, created_by=clerk["id"],
        correlation_id=uuid.uuid4(),
    )
    db.add(order)
    db.flush()
    po_line = PurchaseOrderLine(
        id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
        line_number=1, description="Widgets", quantity=20, unit_price=100,
        amount=2000, item_id=item.id,
    )
    db.add(po_line)
    db.flush()

    receipt = GoodsReceiptService(db).record_receipt(
        order.id,
        SimpleNamespace(
            received_date=None, delivery_note=None, notes=None,
            location_id=warehouse.bay.id,
            lines=[SimpleNamespace(
                purchase_order_line_id=po_line.id, quantity_received=20,
            )],
        ),
        clerk,
    )
    receipt_line = db.query(GoodsReceiptLine).filter(
        GoodsReceiptLine.goods_receipt_id == receipt.id
    ).one()

    return SimpleNamespace(
        clerk=clerk, item=item, vendor=vendor, order=order,
        receipt=receipt, receipt_line=receipt_line,
    )


class TestARejectionMustBeExplained:
    def test_a_rejection_without_a_reason_code_is_refused(
        self, db, tenant, warehouse, delivery
    ):
        with pytest.raises(ValueError, match="needs a reason code"):
            QualityCheckService(db).record(
                delivery.receipt_line.id, delivery.clerk,
                quantity_accepted=15, quantity_rejected=5, notes="Dented",
            )

    def test_a_rejection_without_a_note_is_refused(
        self, db, tenant, warehouse, delivery
    ):
        """The code says the category; the note is the evidence."""
        with pytest.raises(ValueError, match="needs a note"):
            QualityCheckService(db).record(
                delivery.receipt_line.id, delivery.clerk,
                quantity_accepted=15, quantity_rejected=5,
                reason_code=REASON_DAMAGED,
            )

    def test_a_clean_pass_needs_neither(self, db, tenant, warehouse, delivery):
        """Nothing to explain, so nothing is demanded. A control that nags on
        the happy path gets clicked through on the unhappy one."""
        check = QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=20, quantity_rejected=0,
        )

        assert check.outcome == QC_PASSED

    def test_inspecting_more_than_arrived_is_refused(
        self, db, tenant, warehouse, delivery
    ):
        with pytest.raises(ValueError, match="only 20"):
            QualityCheckService(db).record(
                delivery.receipt_line.id, delivery.clerk,
                quantity_accepted=25, quantity_rejected=0,
            )


class TestRejectedStockIsQuarantined:
    def test_rejected_goods_leave_the_bay(self, db, tenant, warehouse, delivery):
        """Recording a rejection while the goods stay available for picking is
        worse than no check at all — it reads as a control that was applied."""
        QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=15, quantity_rejected=5,
            reason_code=REASON_DAMAGED, notes="Crushed in transit",
        )

        service = StockService(db)
        assert service.on_hand(delivery.item.id, warehouse.bay.id) == Decimal("15")
        assert service.on_hand(delivery.item.id, warehouse.quarantine.id) == Decimal("5")

    def test_quarantining_does_not_create_or_destroy_stock(
        self, db, tenant, warehouse, delivery
    ):
        """A transfer is two movements that must sum to zero. If they do not,
        a quality check silently changes how much exists."""
        QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=15, quantity_rejected=5,
            reason_code=REASON_DAMAGED, notes="Crushed in transit",
        )

        assert StockService(db).on_hand(delivery.item.id) == Decimal("20")
        assert StockService(db).reconcile_balances() == []

    def test_the_outcome_reflects_a_partial_rejection(
        self, db, tenant, warehouse, delivery
    ):
        check = QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=15, quantity_rejected=5,
            reason_code=REASON_DAMAGED, notes="Some crushed",
        )
        assert check.outcome == QC_PARTIAL

    def test_a_total_rejection_reads_as_failed(
        self, db, tenant, warehouse, delivery
    ):
        check = QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=0, quantity_rejected=20,
            reason_code=REASON_DAMAGED, notes="Whole pallet soaked",
        )
        assert check.outcome == QC_FAILED
        assert StockService(db).on_hand(delivery.item.id, warehouse.bay.id) == Decimal("0")

    def test_the_receipt_is_not_undone(self, db, tenant, warehouse, delivery):
        """What arrived still arrived. Deleting it would be editing away a
        fact, and the three-way match still needs to see the delivery."""
        QualityCheckService(db).record(
            delivery.receipt_line.id, delivery.clerk,
            quantity_accepted=0, quantity_rejected=20,
            reason_code=REASON_DAMAGED, notes="Whole pallet soaked",
        )

        assert db.query(GoodsReceiptLine).filter(
            GoodsReceiptLine.id == delivery.receipt_line.id
        ).one().quantity_received == Decimal("20")


class TestPutaway:
    def test_accepted_goods_move_to_the_shelf(
        self, db, tenant, warehouse, delivery
    ):
        QualityCheckService(db).putaway(
            delivery.receipt_line.id, warehouse.shelf.id, 20, delivery.clerk,
        )

        service = StockService(db)
        assert service.on_hand(delivery.item.id, warehouse.bay.id) == Decimal("0")
        assert service.on_hand(delivery.item.id, warehouse.shelf.id) == Decimal("20")

    def test_putaway_conserves_stock(self, db, tenant, warehouse, delivery):
        QualityCheckService(db).putaway(
            delivery.receipt_line.id, warehouse.shelf.id, 12, delivery.clerk,
        )

        assert StockService(db).on_hand(delivery.item.id) == Decimal("20")
        assert StockService(db).reconcile_balances() == []

    def test_putting_away_to_where_it_already_is_is_refused(
        self, db, tenant, warehouse, delivery
    ):
        with pytest.raises(ValueError, match="already there"):
            QualityCheckService(db).putaway(
                delivery.receipt_line.id, warehouse.bay.id, 5, delivery.clerk,
            )


class TestReturns:
    def _return(self, db, warehouse, delivery, user, reason=REASON_DAMAGED):
        return VendorReturnService(db).create(
            user, vendor_id=delivery.vendor.id, location_id=warehouse.bay.id,
            reason_code=reason,
            lines=[{"item_id": delivery.item.id, "quantity": 5}],
        )

    def test_vendor_fault_is_decided_at_creation(
        self, db, tenant, warehouse, delivery, make_user
    ):
        """Stored, not judged at report time. A scorecard that recomputes this
        rewrites last quarter the moment the definition changes."""
        vendor_return = self._return(db, warehouse, delivery, delivery.clerk)

        assert vendor_return.vendor_attributable is True

    def test_our_own_miscount_is_not_the_vendors_fault(
        self, db, tenant, warehouse, delivery, make_user
    ):
        vendor_return = self._return(
            db, warehouse, delivery, delivery.clerk, reason=REASON_COUNT_CORRECTION,
        )

        assert vendor_return.vendor_attributable is False

    def test_stock_leaves_on_dispatch_not_on_approval(
        self, db, tenant, warehouse, delivery, make_user
    ):
        """The goods are still on the premises until the lorry goes."""
        manager = make_user(UserRole.MANAGER)
        service = VendorReturnService(db)
        vendor_return = self._return(db, warehouse, delivery, delivery.clerk)
        service.submit(vendor_return.id, delivery.clerk)
        service.approve(vendor_return.id, manager)

        assert StockService(db).on_hand(delivery.item.id, warehouse.bay.id) == Decimal("20")

        service.dispatch(vendor_return.id, delivery.clerk)

        assert StockService(db).on_hand(delivery.item.id, warehouse.bay.id) == Decimal("15")

    def test_the_raiser_cannot_approve_their_own_return(
        self, db, tenant, warehouse, delivery, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        service = VendorReturnService(db)
        vendor_return = self._return(db, warehouse, delivery, admin)
        service.submit(vendor_return.id, admin)

        with pytest.raises(PermissionError, match="cannot approve it"):
            service.approve(vendor_return.id, admin)

    def test_a_credit_note_reference_is_required(
        self, db, tenant, warehouse, delivery, make_user
    ):
        """Without it there is nothing to reconcile the credit against later."""
        manager = make_user(UserRole.MANAGER)
        service = VendorReturnService(db)
        vendor_return = self._return(db, warehouse, delivery, delivery.clerk)
        service.submit(vendor_return.id, delivery.clerk)
        service.approve(vendor_return.id, manager)
        service.dispatch(vendor_return.id, delivery.clerk)

        with pytest.raises(ValueError, match="credit note reference"):
            service.record_credit(vendor_return.id, delivery.clerk, "")

    def test_the_full_loop_closes(self, db, tenant, warehouse, delivery, make_user):
        manager = make_user(UserRole.MANAGER)
        service = VendorReturnService(db)
        vendor_return = self._return(db, warehouse, delivery, delivery.clerk)

        service.submit(vendor_return.id, delivery.clerk)
        assert vendor_return.current_state == RET_PENDING_APPROVAL
        service.approve(vendor_return.id, manager)
        assert vendor_return.current_state == RET_APPROVED
        service.dispatch(vendor_return.id, delivery.clerk)
        assert vendor_return.current_state == RET_DISPATCHED
        service.record_credit(vendor_return.id, delivery.clerk, "CN-9912")

        db.refresh(vendor_return)
        assert vendor_return.current_state == RET_CREDITED
        assert vendor_return.credit_note_reference == "CN-9912"

    def test_a_dispatched_return_cannot_be_cancelled(
        self, db, tenant, warehouse, delivery, make_user
    ):
        """Goods that have left are brought back with a receipt, not by
        withdrawing the record that sent them."""
        manager = make_user(UserRole.MANAGER)
        service = VendorReturnService(db)
        vendor_return = self._return(db, warehouse, delivery, delivery.clerk)
        service.submit(vendor_return.id, delivery.clerk)
        service.approve(vendor_return.id, manager)
        service.dispatch(vendor_return.id, delivery.clerk)

        with pytest.raises(ValueError, match="cannot be cancelled"):
            service.cancel(vendor_return.id, delivery.clerk)

    def test_returning_more_than_is_held_is_refused(
        self, db, tenant, warehouse, delivery, make_user
    ):
        """The negative-stock guard, reached through the return path."""
        from app.services.stock_service import InsufficientStock

        manager = make_user(UserRole.MANAGER)
        service = VendorReturnService(db)
        vendor_return = VendorReturnService(db).create(
            delivery.clerk, vendor_id=delivery.vendor.id,
            location_id=warehouse.bay.id, reason_code=REASON_DAMAGED,
            lines=[{"item_id": delivery.item.id, "quantity": 50}],
        )
        service.submit(vendor_return.id, delivery.clerk)
        service.approve(vendor_return.id, manager)

        with pytest.raises(InsufficientStock):
            service.dispatch(vendor_return.id, delivery.clerk)
