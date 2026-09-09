"""Inventory turns and the purchase-to-pay cycle, against known answers.

Build Book, COO / Supply Chain. Both reports here have a specific way of being
wrong that still renders a plausible number, and each gets a class:

  * **Turns only describe the part of the warehouse that has been costed.**
    An item with no standard cost contributes to neither COGS nor inventory
    value, so the ratio silently covers a subset. Excluding such items and
    costing them at zero give the *identical* ratio — zero contributes nothing
    to either half — so the point is disclosure, not arithmetic protection.
    TestUncostedStockIsDisclosed shows the same warehouse reporting a
    different number as more of it gets costed.
  * **P2P must not count a purchase that has not finished.** Treating a missing
    end event as "now" would make every in-flight purchase look like a delay,
    and the figure would climb whenever business picked up.
    TestAnUnfinishedPurchaseIsNotADelay builds that case.

The opening balance is reconstructed by subtracting the window's net movements
from the current balance, which is exact only because the movement ledger is
append-only. TestTheOpeningBalanceIsReconstructed depends on that property and
would fail if movements ever became editable.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.models.inventory import (
    MOVE_ISSUE, MOVE_RECEIPT, MOVE_TRANSFER, Item, StockBalance, StockLocation,
    StockMovement,
)
from app.services.dashboards import DashboardService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _coo(make_user):
    return make_user(UserRole.ADMIN)


def _location(db, tenant_id):
    location = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant_id,
        code=f"L-{uuid.uuid4().hex[:5]}", name="Main",
    )
    db.add(location)
    db.flush()
    return location


def _item(db, tenant_id, cost=None, name="Widget"):
    """`cost=None` is an item nobody has costed, so it falls outside every
    figure the turns report produces."""
    item = Item(
        id=uuid.uuid4(), tenant_id=tenant_id,
        sku=f"SKU-{uuid.uuid4().hex[:6]}", name=name,
        standard_cost=None if cost is None else Decimal(str(cost)),
    )
    db.add(item)
    db.flush()
    return item


def _balance(db, tenant_id, item, location, quantity):
    db.add(StockBalance(
        id=uuid.uuid4(), tenant_id=tenant_id, item_id=item.id,
        location_id=location.id, quantity=Decimal(str(quantity)),
    ))
    db.flush()


def _movement(db, tenant_id, item, location, quantity, movement_type,
              days_ago=1):
    """Signed: positive adds, negative removes."""
    movement = StockMovement(
        id=uuid.uuid4(), tenant_id=tenant_id, item_id=item.id,
        location_id=location.id, quantity=Decimal(str(quantity)),
        movement_type=movement_type,
    )
    db.add(movement)
    db.flush()
    # created_at is server-defaulted, so it is set afterwards to place the
    # movement inside or outside the report window.
    movement.created_at = _now() - timedelta(days=days_ago)
    db.flush()
    return movement


def _event(db, tenant_id, user, object_type, action, at, correlation_id):
    db.add(AuditLog(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user["id"],
        user_email=user["email"], user_role=user["role"],
        object_type=object_type, object_id=uuid.uuid4(), action=action,
        timestamp=at, correlation_id=correlation_id, custom_metadata={},
    ))
    db.flush()


def _purchase(db, tenant, user, *, req_to_po=1, po_to_receipt=5,
              receipt_to_invoice=2, invoice_to_approval=3, approval_to_pay=4,
              stop_after=None):
    """One purchase-to-pay run, laid out backwards from now.

    `stop_after` truncates the chain, for the in-flight case.
    """
    steps = [
        ("requisition", "approved", 0),
        ("purchase_order", "created", req_to_po),
        ("goods_receipt", "goods_received", po_to_receipt),
        ("invoice", "created", receipt_to_invoice),
        ("invoice", "approved", invoice_to_approval),
        ("payment", "released", approval_to_pay),
    ]
    total = sum(s[2] for s in steps)
    at = _now() - timedelta(days=total)
    correlation_id = uuid.uuid4()
    for object_type, action, gap in steps:
        at = at + timedelta(days=gap)
        _event(db, tenant.id, user, object_type, action, at, correlation_id)
        if stop_after and (object_type, action) == stop_after:
            break
    return correlation_id


# --- Inventory turns ---------------------------------------------------------

class TestTurnsAreCogsOverAverageValue:
    def test_it_computes_the_ratio(self, db, tenant, make_user):
        """100 units on hand at 10 each, 200 issued over a year. Closing 1000,
        opening 1000 + 200*10 = 3000, average 2000. COGS 2000. Turns 1.0."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=10)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, -200, MOVE_ISSUE, days_ago=100)

        report = DashboardService(db).inventory_turns(user, days=365)

        assert report["closing_value"] == 1000.0
        assert report["opening_value"] == 3000.0
        assert report["average_value"] == 2000.0
        assert report["cogs"] == 2000.0
        assert report["turns_per_year"] == 1.0

    def test_it_annualises_a_short_window(self, db, tenant, make_user):
        """Turns is conventionally a yearly figure. Reporting a 90-day raw
        ratio under the same name would read as a business turning over four
        times more slowly than it is."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=10)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, -50, MOVE_ISSUE, days_ago=10)

        annual = DashboardService(db).inventory_turns(user, days=365)
        quarter = DashboardService(db).inventory_turns(user, days=90)

        # The same 50 units, over a quarter, is a faster annual rate.
        assert quarter["turns_per_year"] > annual["turns_per_year"]

    def test_days_of_stock_is_the_inverse(self, db, tenant, make_user):
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=10)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, -200, MOVE_ISSUE, days_ago=100)

        report = DashboardService(db).inventory_turns(user, days=365)

        assert report["turns_per_year"] == 1.0
        assert report["days_of_stock"] == 365.0

    def test_no_stock_reports_none_rather_than_zero_turns(
        self, db, tenant, make_user
    ):
        """Zero turns reads as stock sitting dead, which is a different
        problem from having none."""
        report = DashboardService(db).inventory_turns(_coo(make_user))

        assert report["turns_per_year"] is None
        assert report["days_of_stock"] is None


class TestOnlyIssuesCountAsTurnover:
    def test_a_transfer_between_our_own_locations_is_not_a_turn(
        self, db, tenant, make_user
    ):
        """Otherwise a warehouse could improve this figure by shuffling
        boxes."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=10)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, -300, MOVE_TRANSFER, days_ago=5)

        assert DashboardService(db).inventory_turns(user)["cogs"] == 0.0

    def test_a_receipt_is_not_a_turn(self, db, tenant, make_user):
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=10)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, 300, MOVE_RECEIPT, days_ago=5)

        assert DashboardService(db).inventory_turns(user)["cogs"] == 0.0


class TestTheOpeningBalanceIsReconstructed:
    def test_it_subtracts_the_windows_net_movement_from_today(
        self, db, tenant, make_user
    ):
        """Exact rather than estimated, and only possible because the ledger
        is append-only. 100 on hand now, +40 received and -60 issued during
        the window, so the window opened at 120."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=1)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, 40, MOVE_RECEIPT, days_ago=30)
        _movement(db, tenant.id, item, location, -60, MOVE_ISSUE, days_ago=20)

        report = DashboardService(db).inventory_turns(user, days=365)

        assert report["closing_value"] == 100.0
        assert report["opening_value"] == 120.0

    def test_a_movement_outside_the_window_does_not_move_the_opening(
        self, db, tenant, make_user
    ):
        """It happened before the window opened, so it is already reflected in
        the opening balance rather than something to subtract."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        item = _item(db, tenant.id, cost=1)
        _balance(db, tenant.id, item, location, 100)
        _movement(db, tenant.id, item, location, -500, MOVE_ISSUE, days_ago=400)

        report = DashboardService(db).inventory_turns(user, days=90)

        assert report["opening_value"] == 100.0
        assert report["cogs"] == 0.0


class TestUncostedStockIsDisclosed:
    """Not "cannot flatter the figure" — an earlier version of this class was
    named that, on the reasoning that zeroing an unknown cost would inflate
    turns. It would not: zero contributes nothing to COGS and nothing to
    inventory value, so excluding and zeroing give the identical ratio.

    The real property is narrower and still worth testing. The ratio covers
    only the costed items, so it is a fact about the business exactly to the
    extent that the business is costed — and a reader cannot judge that
    without being told how much sits outside it."""

    def test_an_uncosted_item_contributes_to_neither_half(
        self, db, tenant, make_user
    ):
        """So every value on the report describes the costed subset."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        costed = _item(db, tenant.id, cost=10, name="Costed")
        uncosted = _item(db, tenant.id, cost=None, name="Uncosted")
        _balance(db, tenant.id, costed, location, 100)
        _balance(db, tenant.id, uncosted, location, 5000)
        _movement(db, tenant.id, costed, location, -100, MOVE_ISSUE, days_ago=10)
        _movement(db, tenant.id, uncosted, location, -9000, MOVE_ISSUE, days_ago=10)

        report = DashboardService(db).inventory_turns(user, days=365)

        # Only the costed item contributes anywhere.
        assert report["closing_value"] == 1000.0
        assert report["cogs"] == 1000.0

    def test_the_uncosted_stock_is_reported_rather_than_dropped(
        self, db, tenant, make_user
    ):
        """The load-bearing assertion of this class. Excluding it silently
        would leave a turns figure describing a fraction of the warehouse
        while looking like all of it, and there would be nothing on the page
        to say which fraction."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        uncosted = _item(db, tenant.id, cost=None)
        _balance(db, tenant.id, uncosted, location, 5000)

        report = DashboardService(db).inventory_turns(user)

        assert report["uncosted"]["item_count"] == 1
        assert report["uncosted"]["units_on_hand"] == 5000.0

    def test_the_ratio_is_only_as_representative_as_the_costed_share(
        self, db, tenant, make_user
    ):
        """Why the disclosure matters, stated as arithmetic. The same
        warehouse reports a different number depending on how much of it has
        been costed — not because anything is wrong, but because the figure
        only ever covered part of it."""
        user = _coo(make_user)
        location = _location(db, tenant.id)
        costed = _item(db, tenant.id, cost=10, name="Costed")
        uncosted = _item(db, tenant.id, cost=None, name="Uncosted")
        _balance(db, tenant.id, costed, location, 100)
        _balance(db, tenant.id, uncosted, location, 5000)
        _movement(db, tenant.id, costed, location, -100, MOVE_ISSUE, days_ago=10)
        _movement(db, tenant.id, uncosted, location, -9000, MOVE_ISSUE,
                  days_ago=10)

        partial = DashboardService(db).inventory_turns(user, days=365)

        # Now cost the other item. Nothing about the warehouse changed.
        uncosted.standard_cost = Decimal("2")
        db.flush()
        complete = DashboardService(db).inventory_turns(user, days=365)

        assert partial["uncosted"]["item_count"] == 1
        assert complete["uncosted"]["item_count"] == 0
        assert partial["turns_per_year"] != complete["turns_per_year"]


# --- P2P cycle time ----------------------------------------------------------

class TestEachHopIsMeasuredSeparately:
    def test_it_reports_the_gap_at_every_handover(self, db, tenant, make_user):
        """Five handovers between different teams. An end-to-end median is a
        number nobody owns."""
        user = _coo(make_user)
        _purchase(db, tenant, user, req_to_po=1, po_to_receipt=5,
                  receipt_to_invoice=2, invoice_to_approval=3,
                  approval_to_pay=4)

        report = DashboardService(db).p2p_cycle_time(user)

        assert _step(report, "requisition_to_po")["median_days"] == 1.0
        assert _step(report, "po_to_receipt")["median_days"] == 5.0
        assert _step(report, "receipt_to_invoice")["median_days"] == 2.0
        assert _step(report, "invoice_to_approval")["median_days"] == 3.0
        assert _step(report, "approval_to_payment")["median_days"] == 4.0

    def test_it_names_the_slowest_hop(self, db, tenant, make_user):
        """The only actionable question the report answers."""
        user = _coo(make_user)
        _purchase(db, tenant, user, po_to_receipt=30)

        report = DashboardService(db).p2p_cycle_time(user)

        assert report["slowest_step"] == "po_to_receipt"

    def test_the_total_is_the_sum_of_the_medians(self, db, tenant, make_user):
        """Stated as such because it is not the median of the totals, and no
        single purchase necessarily took this long."""
        user = _coo(make_user)
        _purchase(db, tenant, user, req_to_po=1, po_to_receipt=5,
                  receipt_to_invoice=2, invoice_to_approval=3,
                  approval_to_pay=4)

        report = DashboardService(db).p2p_cycle_time(user)

        assert report["typical_total_days"] == 15.0

    def test_every_step_carries_a_human_label(self, db, tenant, make_user):
        """`goods_receipt.goods_received` is internal vocabulary. It is kept
        alongside for anybody reconciling against the trail, not as the name."""
        report = DashboardService(db).p2p_cycle_time(_coo(make_user))

        for step in report["steps"]:
            assert step["label"] and "_" not in step["label"]
            assert "." in step["from"] and "." in step["to"]


class TestAnUnfinishedPurchaseIsNotADelay:
    def test_it_counts_only_the_hops_that_completed(self, db, tenant, make_user):
        """Received but not yet invoiced. Treating the missing end as "now"
        would make every in-flight purchase look like a delay, and the figure
        would climb whenever business picked up."""
        user = _coo(make_user)
        _purchase(db, tenant, user,
                  stop_after=("goods_receipt", "goods_received"))

        report = DashboardService(db).p2p_cycle_time(user)

        assert _step(report, "requisition_to_po")["count"] == 1
        assert _step(report, "po_to_receipt")["count"] == 1
        assert _step(report, "receipt_to_invoice")["count"] == 0
        assert _step(report, "receipt_to_invoice")["median_days"] is None

    def test_a_finished_and_an_unfinished_run_do_not_mix(
        self, db, tenant, make_user
    ):
        """The completed hops of the in-flight purchase still count; only its
        unreached ones are absent."""
        user = _coo(make_user)
        _purchase(db, tenant, user, req_to_po=2)
        _purchase(db, tenant, user, req_to_po=4,
                  stop_after=("purchase_order", "created"))

        report = DashboardService(db).p2p_cycle_time(user)

        assert _step(report, "requisition_to_po")["count"] == 2
        assert _step(report, "requisition_to_po")["median_days"] == 3.0
        assert _step(report, "approval_to_payment")["count"] == 1


class TestChainsAreKeptApart:
    def test_two_purchases_are_two_chains(self, db, tenant, make_user):
        """Joined by correlation id. If that broke, events from different
        purchases would be paired and the durations would be nonsense rather
        than an error."""
        user = _coo(make_user)
        _purchase(db, tenant, user, po_to_receipt=2)
        _purchase(db, tenant, user, po_to_receipt=8)

        report = DashboardService(db).p2p_cycle_time(user)

        assert report["chains_seen"] == 2
        assert _step(report, "po_to_receipt")["count"] == 2
        assert _step(report, "po_to_receipt")["median_days"] == 5.0
        assert _step(report, "po_to_receipt")["worst_days"] == 8.0


class TestPermissions:
    def test_turns_read_with_the_inventory_gate(self, db, tenant, make_user):
        """An approver can see invoices and not stock."""
        approver = make_user(UserRole.APPROVER)

        with pytest.raises(PermissionError):
            DashboardService(db).inventory_turns(approver)

    def test_p2p_reads_with_the_invoice_gate(self, db, tenant, make_user):
        """It spans five modules but exposes only durations, and everybody who
        can read an invoice can already open the records behind them."""
        assert DashboardService(db).p2p_cycle_time(
            make_user(UserRole.APPROVER)
        ) is not None

    def test_the_api_serves_both(self, client, as_user, make_user):
        as_user(make_user(UserRole.ADMIN))

        assert client.get("/api/v1/dashboard/inventory-turns").status_code == 200
        assert client.get("/api/v1/dashboard/p2p-cycle-time").status_code == 200


def _step(report, name):
    return next(s for s in report["steps"] if s["step"] == name)
