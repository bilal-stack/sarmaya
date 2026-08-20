"""The three Variant D1 reports, and the inbox items that feed them.

Build Book D1 "Reports": stock accuracy and adjustment rate, supplier delivery
performance and lead time adherence, GRN-to-invoice latency and impact on AP.

Each has one judgement in it worth pinning down, and in all three cases the
wrong choice produces a number that looks fine:

  * **Stock accuracy nets nothing.** Two adjustments that cancel out are two
    discrepancies, not a tidy warehouse.
  * **A vendor with no promised date is unmeasurable, not punctual.** Counting
    them as on time flatters every supplier whose orders never carried a date.
  * **Received-but-uninvoiced is a liability.** A month-end that misses it
    understates what is owed.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.enums import PurchaseOrderState, UserRole, VendorStatus
from app.models.inventory import (
    Item, StockLocation, REASON_COUNT_CORRECTION, REASON_DAMAGED,
    REASON_THEFT_OR_LOSS,
)
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.services.dashboards import DashboardService
from app.utils.datetime_helpers import make_naive, to_utc, utc_now
from app.services.decision_inbox_service import DecisionInboxService
from app.services.goods_receipt_service import GoodsReceiptService
from app.services.inventory_adjustment_service import InventoryAdjustmentService
from app.services.stock_service import StockService
from app.services.vendor_return_service import VendorReturnService

pytestmark = pytest.mark.integration


def _utc_today():
    """The clock the reports use.

    `date.today()` is the local date, which is a different day from UTC for
    most of the world and made an age assertion here off by one. Every stored
    timestamp in this system is naive UTC, so a test measuring an age has to
    ask the same clock.
    """
    return make_naive(to_utc(utc_now())).date()


@pytest.fixture
def yard(db, tenant, make_user):
    clerk = make_user(UserRole.AP_CLERK)
    manager = make_user(UserRole.MANAGER)
    item = Item(
        id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-R", name="Widget",
        uom="each", standard_cost=Decimal("100.00"),
    )
    location = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code="MAIN", name="Main",
    )
    db.add_all([item, location])
    db.flush()
    StockService(db).post_movement(
        tenant_id=tenant.id, item_id=item.id, location_id=location.id,
        quantity=500, movement_type="receipt",
    )
    return SimpleNamespace(clerk=clerk, manager=manager, item=item, location=location)


def _posted_adjustment(db, yard, change, reason=REASON_COUNT_CORRECTION):
    service = InventoryAdjustmentService(db)
    adjustment = service.create(
        yard.clerk, location_id=yard.location.id, reason_code=reason,
        lines=[{"item_id": yard.item.id, "quantity_change": change}],
    )
    service.submit(adjustment.id, yard.clerk)
    service.approve(adjustment.id, yard.manager)
    return adjustment


class TestStockAccuracy:
    def test_write_offs_and_write_ons_are_not_netted(self, db, tenant, yard):
        """Netting is how a loss disappears into an average."""
        _posted_adjustment(db, yard, -10)
        _posted_adjustment(db, yard, 10)

        report = DashboardService(db).stock_accuracy(yard.manager)

        assert report["value_written_off"] == 1000.0
        assert report["value_written_on"] == 1000.0

    def test_loss_and_theft_are_called_out_separately(self, db, tenant, yard):
        """These mean something left without anybody selling it. Buried in a
        total, they stop being noticed."""
        _posted_adjustment(db, yard, -5, reason=REASON_THEFT_OR_LOSS)
        _posted_adjustment(db, yard, -5, reason=REASON_COUNT_CORRECTION)

        report = DashboardService(db).stock_accuracy(yard.manager)

        assert report["unexplained_loss_value"] == 500.0
        assert report["value_written_off"] == 1000.0

    def test_the_write_off_rate_is_measured_against_what_is_held(
        self, db, tenant, yard
    ):
        """A rate needs something to be a rate of. "Adjustments per month" says
        nothing about a warehouse that doubled in size."""
        _posted_adjustment(db, yard, -10)

        report = DashboardService(db).stock_accuracy(yard.manager)

        # 490 units left at 100 each after writing off 10.
        assert report["holding_value"] == 49000.0
        assert report["write_off_rate_percent"] == pytest.approx(2.04, abs=0.01)

    def test_an_unapproved_adjustment_is_not_counted(self, db, tenant, yard):
        """Only posted adjustments moved stock. Counting a draft would report a
        write-off that never happened."""
        InventoryAdjustmentService(db).create(
            yard.clerk, location_id=yard.location.id,
            reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": yard.item.id, "quantity_change": -50}],
        )

        report = DashboardService(db).stock_accuracy(yard.manager)

        assert report["adjustments_posted"] == 0
        assert report["value_written_off"] == 0.0

    def test_reasons_are_broken_out(self, db, tenant, yard):
        _posted_adjustment(db, yard, -5, reason=REASON_DAMAGED)

        report = DashboardService(db).stock_accuracy(yard.manager)

        assert [r["reason"] for r in report["by_reason"]] == [REASON_DAMAGED]


class TestSupplierPerformance:
    def _delivery(self, db, tenant, yard, expected_date, received_date,
                  vendor_name="Punctual Ltd"):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name=vendor_name,
            status=VendorStatus.ACTIVE, created_by=yard.clerk["id"],
        )
        db.add(vendor)
        db.flush()
        order = PurchaseOrder(
            id=uuid.uuid4(), tenant_id=tenant.id,
            po_number=f"PO-{uuid.uuid4().hex[:6]}", vendor_id=vendor.id,
            vendor_name=vendor_name, order_date=date(2026, 7, 1),
            expected_date=expected_date, total_amount=1000,
            current_state=PurchaseOrderState.ISSUED, created_by=yard.clerk["id"],
            correlation_id=uuid.uuid4(),
        )
        db.add(order)
        db.flush()
        line = PurchaseOrderLine(
            id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
            line_number=1, description="Widgets", quantity=10, unit_price=100,
            amount=1000, item_id=yard.item.id,
        )
        db.add(line)
        db.flush()

        GoodsReceiptService(db).record_receipt(
            order.id,
            SimpleNamespace(
                received_date=received_date, delivery_note=None, notes=None,
                location_id=yard.location.id,
                lines=[SimpleNamespace(
                    purchase_order_line_id=line.id, quantity_received=10,
                )],
            ),
            yard.clerk,
        )
        return vendor

    def test_a_late_delivery_is_counted_and_measured(self, db, tenant, yard):
        today = date.today()
        self._delivery(
            db, tenant, yard,
            expected_date=today - timedelta(days=10),
            received_date=today - timedelta(days=4),
        )

        report = DashboardService(db).supplier_delivery_performance(yard.manager)
        vendor = report["vendors"][0]

        assert vendor["late"] == 1
        assert vendor["average_days_late"] == 6.0
        assert vendor["on_time_percent"] == 0.0

    def test_an_early_delivery_counts_as_on_time(self, db, tenant, yard):
        today = date.today()
        self._delivery(
            db, tenant, yard,
            expected_date=today - timedelta(days=2),
            received_date=today - timedelta(days=5),
        )

        report = DashboardService(db).supplier_delivery_performance(yard.manager)

        assert report["vendors"][0]["on_time"] == 1

    def test_a_delivery_with_no_promised_date_is_unmeasurable_not_punctual(
        self, db, tenant, yard
    ):
        """Assuming on time would flatter every supplier whose orders never
        carried a date — which is most of them, in most systems."""
        self._delivery(
            db, tenant, yard, expected_date=None,
            received_date=date.today() - timedelta(days=3),
        )

        report = DashboardService(db).supplier_delivery_performance(yard.manager)
        vendor = report["vendors"][0]

        assert vendor["unknown_due_date"] == 1
        assert vendor["on_time"] == 0
        assert vendor["on_time_percent"] is None
        assert report["deliveries_with_no_promised_date"] == 1

    def test_the_worst_supplier_sorts_first(self, db, tenant, yard):
        today = date.today()
        self._delivery(
            db, tenant, yard, vendor_name="Good Ltd",
            expected_date=today - timedelta(days=5),
            received_date=today - timedelta(days=6),
        )
        self._delivery(
            db, tenant, yard, vendor_name="Bad Ltd",
            expected_date=today - timedelta(days=20),
            received_date=today - timedelta(days=2),
        )

        report = DashboardService(db).supplier_delivery_performance(yard.manager)

        assert report["vendors"][0]["vendor"] == "Bad Ltd"

    def test_returns_that_are_the_vendors_fault_are_attributed(
        self, db, tenant, yard
    ):
        vendor = self._delivery(
            db, tenant, yard, vendor_name="Sloppy Ltd",
            expected_date=date.today() - timedelta(days=5),
            received_date=date.today() - timedelta(days=5),
        )
        service = VendorReturnService(db)
        vendor_return = service.create(
            yard.clerk, vendor_id=vendor.id, location_id=yard.location.id,
            reason_code=REASON_DAMAGED,
            lines=[{"item_id": yard.item.id, "quantity": 2}],
        )
        service.submit(vendor_return.id, yard.clerk)
        service.approve(vendor_return.id, yard.manager)

        report = DashboardService(db).supplier_delivery_performance(yard.manager)
        sloppy = next(v for v in report["vendors"] if v["vendor"] == "Sloppy Ltd")

        assert sloppy["returns_their_fault"] == 1


class TestReceiptToInvoiceLatency:
    def test_goods_received_with_no_invoice_are_a_liability(
        self, db, tenant, yard
    ):
        """The accrual an auditor asks about. A month-end that misses this
        understates what is owed."""
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Waiting Ltd",
            status=VendorStatus.ACTIVE, created_by=yard.clerk["id"],
        )
        db.add(vendor)
        db.flush()
        order = PurchaseOrder(
            id=uuid.uuid4(), tenant_id=tenant.id, po_number="PO-WAIT",
            vendor_id=vendor.id, vendor_name="Waiting Ltd",
            order_date=date(2026, 7, 1), total_amount=1000,
            current_state=PurchaseOrderState.ISSUED, created_by=yard.clerk["id"],
            correlation_id=uuid.uuid4(),
        )
        db.add(order)
        db.flush()
        line = PurchaseOrderLine(
            id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
            line_number=1, description="Widgets", quantity=10, unit_price=100,
            amount=1000, item_id=yard.item.id,
        )
        db.add(line)
        db.flush()

        GoodsReceiptService(db).record_receipt(
            order.id,
            SimpleNamespace(
                received_date=_utc_today() - timedelta(days=12),
                delivery_note=None, notes=None, location_id=yard.location.id,
                lines=[SimpleNamespace(
                    purchase_order_line_id=line.id, quantity_received=10,
                )],
            ),
            yard.clerk,
        )

        report = DashboardService(db).receipt_to_invoice_latency(yard.manager)

        assert report["receipts_awaiting_invoice"] == 1
        assert report["value_awaiting_invoice"] == 1000.0
        assert report["oldest"][0]["days_waiting"] == 12


class TestTheInboxCoversTheseToo:
    """Definition of Done: the Decision Inbox supports every work item type in
    the variant."""

    def test_an_adjustment_awaiting_approval_reaches_the_inbox(
        self, db, tenant, yard
    ):
        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            yard.clerk, location_id=yard.location.id,
            reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": yard.item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, yard.clerk)

        inbox = DecisionInboxService(db).get_inbox(yard.manager)

        assert "inventory_adjustment" in {i["object_type"] for i in inbox["items"]}

    def test_the_raiser_does_not_see_their_own(self, db, tenant, yard, make_user):
        """It is not their decision to make — SoD refuses it at approval, so
        showing it would be an item they cannot action.

        Raised by an admin, who is the only role holding both permissions; a
        manager cannot raise one at all, which is the permission split doing
        its job and would make this test pass for the wrong reason.
        """
        service = InventoryAdjustmentService(db)
        admin = make_user(UserRole.ADMIN)
        adjustment = service.create(
            admin, location_id=yard.location.id,
            reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": yard.item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, admin)

        inbox = DecisionInboxService(db).get_inbox(admin)

        assert adjustment.id not in {i["object_id"] for i in inbox["items"]}

    def test_the_first_approver_does_not_see_it_again(self, db, tenant, yard, make_user):
        """Waiting on a *second* person. Leaving it in the first approver's
        inbox would be asking them to click twice."""
        cfo = make_user(UserRole.CFO)
        item = yard.item
        item.standard_cost = Decimal("1000.00")
        db.flush()

        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            yard.clerk, location_id=yard.location.id, reason_code=REASON_DAMAGED,
            lines=[{"item_id": item.id, "quantity_change": -100}],
        )
        service.submit(adjustment.id, yard.clerk)
        service.approve(adjustment.id, yard.manager)

        assert adjustment.id not in {
            i["object_id"] for i in DecisionInboxService(db).get_inbox(yard.manager)["items"]
        }
        assert adjustment.id in {
            i["object_id"] for i in DecisionInboxService(db).get_inbox(cfo)["items"]
        }

    def test_a_return_awaiting_approval_reaches_the_inbox(self, db, tenant, yard):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Return Co",
            status=VendorStatus.ACTIVE, created_by=yard.clerk["id"],
        )
        db.add(vendor)
        db.flush()

        service = VendorReturnService(db)
        vendor_return = service.create(
            yard.clerk, vendor_id=vendor.id, location_id=yard.location.id,
            reason_code=REASON_DAMAGED,
            lines=[{"item_id": yard.item.id, "quantity": 2}],
        )
        service.submit(vendor_return.id, yard.clerk)

        inbox = DecisionInboxService(db).get_inbox(yard.manager)

        assert "vendor_return" in {i["object_type"] for i in inbox["items"]}

    def test_a_clerk_sees_neither(self, db, tenant, yard):
        """The inbox only shows what you can actually act on."""
        service = InventoryAdjustmentService(db)
        adjustment = service.create(
            yard.clerk, location_id=yard.location.id,
            reason_code=REASON_COUNT_CORRECTION,
            lines=[{"item_id": yard.item.id, "quantity_change": -5}],
        )
        service.submit(adjustment.id, yard.clerk)

        inbox = DecisionInboxService(db).get_inbox(yard.clerk)

        assert "inventory_adjustment" not in {i["object_type"] for i in inbox["items"]}


class TestTheReportsExport:
    def test_each_supply_chain_report_exports(
        self, db, tenant, client, as_user, make_user, yard
    ):
        as_user(yard.manager)

        for report in ("stock-accuracy", "supplier-performance", "receipt-to-invoice"):
            response = client.get(f"/api/v1/dashboard/{report}/export?format=html")
            assert response.status_code == 200, f"{report}: {response.text[:200]}"
