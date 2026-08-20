"""The inventory API, and the AI explanation behind a bad delivery.

Two things are tested harder than the rest:

  * **The approval route is one route.** A small adjustment takes one call and
    a large one takes two, from two different people, and the *service* decides
    which. An endpoint pair like `/approve` and `/second-approve` would let a
    client provide the second signature without the first ever existing.
  * **The AI explanation is validated, never trusted.** A model inventing a
    reason code would quietly create a category nothing counts — the exact
    failure a fixed vocabulary exists to prevent — and the deterministic
    explanation has to stand alone when no model answers at all.
"""
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.enums import PurchaseOrderState, UserRole, VendorStatus
from app.models.goods_receipt import GoodsReceiptLine
from app.models.inventory import Item, StockLocation, REASON_COUNT_CORRECTION
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.services.goods_receipt_service import GoodsReceiptService
from app.services.receiving_exception_service import ReceivingExceptionService

pytestmark = pytest.mark.integration


class TestTheCatalog:
    def test_an_item_can_be_created_and_listed(
        self, db, tenant, client, as_user, make_user
    ):
        as_user(make_user(UserRole.AP_CLERK))

        created = client.post("/api/v1/inventory/items", json={
            "sku": "WIDGET-1", "name": "Widget", "uom": "each",
            "standard_cost": "12.50",
        })
        assert created.status_code == 201, created.text

        listed = client.get("/api/v1/inventory/items")
        assert "WIDGET-1" in {i["sku"] for i in listed.json()}

    def test_a_duplicate_sku_is_refused(
        self, db, tenant, client, as_user, make_user
    ):
        """A SKU is how people refer to a thing out loud, so two of them makes
        every report ambiguous."""
        as_user(make_user(UserRole.AP_CLERK))
        client.post("/api/v1/inventory/items", json={"sku": "DUP", "name": "A"})

        again = client.post("/api/v1/inventory/items", json={"sku": "DUP", "name": "B"})

        assert again.status_code == 400
        assert "already exists" in again.json()["detail"]

    def test_a_reorder_point_on_a_non_stocked_item_is_refused(
        self, db, tenant, client, as_user, make_user
    ):
        """It could never be reached, so it would read as configured when it is
        not — which is worse than leaving it unset."""
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post("/api/v1/inventory/items", json={
            "sku": "SVC-1", "name": "Consulting", "is_stocked": False,
            "reorder_point": "5",
        })

        assert response.status_code == 400

    def test_a_location_cannot_be_both_bay_and_quarantine(
        self, db, tenant, client, as_user, make_user
    ):
        """Goods would be quarantined into the place they arrive."""
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post("/api/v1/inventory/locations", json={
            "code": "BOTH", "name": "Both", "is_receiving_bay": True,
            "is_quarantine": True,
        })

        assert response.status_code == 400

    def test_an_auditor_can_read_but_not_create(
        self, db, tenant, client, as_user, make_user
    ):
        as_user(make_user(UserRole.AUDITOR))

        assert client.get("/api/v1/inventory/items").status_code == 200
        assert client.post(
            "/api/v1/inventory/items", json={"sku": "X", "name": "X"}
        ).status_code == 403


@pytest.fixture
def stocked(db, tenant, make_user):
    """An item with 100 on hand, and the people who act on it."""
    from app.services.stock_service import StockService

    clerk = make_user(UserRole.AP_CLERK)
    manager = make_user(UserRole.MANAGER)
    cfo = make_user(UserRole.CFO)
    item = Item(
        id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-API", name="Widget",
        uom="each", standard_cost=Decimal("1000.00"),
    )
    location = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant.id, code="MAIN", name="Main",
    )
    db.add_all([item, location])
    db.flush()
    StockService(db).post_movement(
        tenant_id=tenant.id, item_id=item.id, location_id=location.id,
        quantity=100, movement_type="receipt",
    )
    db.commit()
    return SimpleNamespace(
        clerk=clerk, manager=manager, cfo=cfo, item=item, location=location,
    )


class TestAdjustmentsThroughTheApi:
    def _create(self, client, stocked, change):
        return client.post("/api/v1/inventory/adjustments", json={
            "location_id": str(stocked.location.id),
            "reason_code": REASON_COUNT_CORRECTION,
            "lines": [{
                "item_id": str(stocked.item.id), "quantity_change": str(change),
            }],
        })

    def test_the_whole_small_adjustment_flow(
        self, db, tenant, client, as_user, stocked
    ):
        as_user(stocked.clerk)
        created = self._create(client, stocked, -1)
        assert created.status_code == 201, created.text
        adjustment_id = created.json()["id"]

        submitted = client.post(
            f"/api/v1/inventory/adjustments/{adjustment_id}/submit"
        )
        assert submitted.json()["requires_dual_approval"] is False

        as_user(stocked.manager)
        approved = client.post(
            f"/api/v1/inventory/adjustments/{adjustment_id}/approve"
        )

        assert approved.status_code == 200, approved.text
        assert approved.json()["current_state"] == "posted"

    def test_one_route_serves_both_signatures(
        self, db, tenant, client, as_user, stocked
    ):
        """The service decides whether a call is the first or the second. A
        client that could choose would be able to provide the second signature
        without the first ever existing."""
        as_user(stocked.clerk)
        adjustment_id = self._create(client, stocked, -60).json()["id"]
        client.post(f"/api/v1/inventory/adjustments/{adjustment_id}/submit")

        as_user(stocked.manager)
        first = client.post(f"/api/v1/inventory/adjustments/{adjustment_id}/approve")
        assert first.json()["current_state"] == "pending_approval"

        as_user(stocked.cfo)
        second = client.post(f"/api/v1/inventory/adjustments/{adjustment_id}/approve")
        assert second.json()["current_state"] == "posted"

    def test_the_raiser_is_refused_at_the_api_too(
        self, db, tenant, client, as_user, make_user, stocked
    ):
        """SoD has to hold at the edge, not only in the service."""
        admin = make_user(UserRole.ADMIN)
        as_user(admin)
        adjustment_id = self._create(client, stocked, -1).json()["id"]
        client.post(f"/api/v1/inventory/adjustments/{adjustment_id}/submit")

        response = client.post(
            f"/api/v1/inventory/adjustments/{adjustment_id}/approve"
        )

        assert response.status_code == 403

    def test_writing_off_more_than_exists_is_a_conflict_not_a_bad_request(
        self, db, tenant, client, as_user, stocked
    ):
        """409, because the request was well formed and would have been fine
        against a different balance — which is what a client needs to know to
        decide whether retrying could ever help.

        Deliberately a cheap item: the expensive one crosses the dual-approval
        threshold, so the first signature correctly returns "still pending"
        without posting, and the overdraw would not surface until the second.
        That is right, and it would make this test pass for the wrong reason.
        """
        from app.services.stock_service import StockService

        cheap = Item(
            id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-CHEAP",
            name="Screws", uom="each", standard_cost=Decimal("1.00"),
        )
        db.add(cheap)
        db.flush()
        StockService(db).post_movement(
            tenant_id=tenant.id, item_id=cheap.id,
            location_id=stocked.location.id, quantity=10,
            movement_type="receipt",
        )
        db.commit()

        as_user(stocked.clerk)
        created = client.post("/api/v1/inventory/adjustments", json={
            "location_id": str(stocked.location.id),
            "reason_code": REASON_COUNT_CORRECTION,
            "lines": [{"item_id": str(cheap.id), "quantity_change": "-500"}],
        })
        adjustment_id = created.json()["id"]
        submitted = client.post(
            f"/api/v1/inventory/adjustments/{adjustment_id}/submit"
        )
        assert submitted.json()["requires_dual_approval"] is False

        as_user(stocked.manager)
        response = client.post(
            f"/api/v1/inventory/adjustments/{adjustment_id}/approve"
        )

        assert response.status_code == 409

    def test_stock_and_movements_are_readable(
        self, db, tenant, client, as_user, stocked
    ):
        as_user(stocked.clerk)

        stock = client.get("/api/v1/inventory/stock")
        movements = client.get("/api/v1/inventory/movements")

        assert stock.status_code == 200
        assert stock.json()[0]["quantity"] == 100.0
        assert movements.json()[0]["movement_type"] == "receipt"

    def test_the_ledger_and_the_balance_agree(
        self, db, tenant, client, as_user, stocked
    ):
        as_user(stocked.clerk)

        response = client.get("/api/v1/inventory/reconcile")

        assert response.json()["discrepancies"] == []


class TestExplainingABadDelivery:
    @pytest.fixture
    def short_delivery(self, db, tenant, make_user):
        """10 ordered, 7 delivered, four days late."""
        clerk = make_user(UserRole.AP_CLERK)
        item = Item(
            id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-EX", name="Widget",
            uom="each", standard_cost=Decimal("10.00"),
        )
        location = StockLocation(
            id=uuid.uuid4(), tenant_id=tenant.id, code="BAY", name="Bay",
        )
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Late Ltd",
            status=VendorStatus.ACTIVE, created_by=clerk["id"],
        )
        db.add_all([item, location, vendor])
        db.flush()

        order = PurchaseOrder(
            id=uuid.uuid4(), tenant_id=tenant.id, po_number="PO-EX",
            vendor_id=vendor.id, vendor_name="Late Ltd",
            order_date=date(2026, 7, 1),
            expected_date=date(2026, 8, 1), total_amount=100,
            current_state=PurchaseOrderState.ISSUED, created_by=clerk["id"],
            correlation_id=uuid.uuid4(),
        )
        db.add(order)
        db.flush()
        po_line = PurchaseOrderLine(
            id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
            line_number=1, description="Widgets", quantity=10, unit_price=10,
            amount=100, item_id=item.id,
        )
        db.add(po_line)
        db.flush()

        receipt = GoodsReceiptService(db).record_receipt(
            order.id,
            SimpleNamespace(
                received_date=date(2026, 8, 5), delivery_note=None, notes=None,
                location_id=location.id,
                lines=[SimpleNamespace(
                    purchase_order_line_id=po_line.id, quantity_received=7,
                )],
            ),
            clerk,
        )
        line = db.query(GoodsReceiptLine).filter(
            GoodsReceiptLine.goods_receipt_id == receipt.id
        ).one()
        return SimpleNamespace(clerk=clerk, receipt_line=line)

    def test_the_computed_explanation_stands_without_any_ai(
        self, db, tenant, short_delivery
    ):
        """The AI is off in this environment, which is the point: a receiving
        clerk deciding whether to reject a delivery needs an answer now."""
        explanation = ReceivingExceptionService(db).explain(
            short_delivery.receipt_line.id, short_delivery.clerk,
        )

        kinds = {e["type"] for e in explanation["exceptions"]}
        assert "shortage" in kinds
        assert "delay" in kinds
        assert explanation["quantity_received"] == 7.0
        assert explanation["days_late"] == 4

    def test_the_absence_of_an_ai_answer_is_stated(
        self, db, tenant, short_delivery
    ):
        """A blank where an explanation should be reads as a broken screen."""
        explanation = ReceivingExceptionService(db).explain(
            short_delivery.receipt_line.id, short_delivery.clerk,
        )

        assert explanation["ai"] is None
        assert "stand on their own" in explanation["ai_note"]

    def test_a_hallucinated_reason_code_is_discarded(
        self, db, tenant, short_delivery, monkeypatch
    ):
        """A model inventing a code would create a category nothing counts —
        the exact failure a fixed vocabulary exists to prevent."""
        from app.services.ai.router import AIRouter, RoutedResult
        from app.services.ai.schemas import ReceivingExceptionExplanation

        def _fake_run(self, prompt_name, variables, **kwargs):
            return RoutedResult(
                output=ReceivingExceptionExplanation(
                    likely_cause="Supplier split the shipment.",
                    suggested_reason_code="carrier_lost_it",
                    follow_up_actions=["Chase the balance"],
                    vendor_attributable=True,
                    confidence=0.9,
                    reasoning="7 of 10 arrived",
                ),
                prompt_name="receiving_exception", prompt_version="v1",
                prompt_hash="abc123", provider="test", model="test-model",
            )

        monkeypatch.setattr(AIRouter, "run", _fake_run)

        explanation = ReceivingExceptionService(db).explain(
            short_delivery.receipt_line.id, short_delivery.clerk,
        )

        assert explanation["ai"]["suggested_reason_code"] is None
        assert explanation["ai"]["likely_cause"] == "Supplier split the shipment."

    def test_a_valid_reason_code_is_kept(
        self, db, tenant, short_delivery, monkeypatch
    ):
        from app.services.ai.router import AIRouter, RoutedResult
        from app.services.ai.schemas import ReceivingExceptionExplanation

        def _fake_run(self, prompt_name, variables, **kwargs):
            return RoutedResult(
                output=ReceivingExceptionExplanation(
                    likely_cause="Short shipped.",
                    suggested_reason_code="shortage",
                    follow_up_actions=["Chase the balance"],
                    vendor_attributable=True,
                    confidence=0.9, reasoning="7 of 10",
                ),
                prompt_name="receiving_exception", prompt_version="v1",
                prompt_hash="abc123", provider="test", model="test-model",
            )

        monkeypatch.setattr(AIRouter, "run", _fake_run)

        explanation = ReceivingExceptionService(db).explain(
            short_delivery.receipt_line.id, short_delivery.clerk,
        )

        assert explanation["ai"]["suggested_reason_code"] == "shortage"

    def test_the_ai_call_is_logged_with_its_provenance(
        self, db, tenant, short_delivery, monkeypatch
    ):
        """Build Book: prompt and model versions stored with every AI output."""
        from app.models.ai_action_log import AIActionLog
        from app.services.ai.router import AIRouter, RoutedResult
        from app.services.ai.schemas import ReceivingExceptionExplanation

        def _fake_run(self, prompt_name, variables, **kwargs):
            return RoutedResult(
                output=ReceivingExceptionExplanation(
                    likely_cause="Short shipped.", suggested_reason_code="shortage",
                    follow_up_actions=[], vendor_attributable=True,
                    confidence=0.88, reasoning="7 of 10",
                ),
                prompt_name="receiving_exception", prompt_version="v1",
                prompt_hash="abc123", provider="test", model="test-model",
            )

        monkeypatch.setattr(AIRouter, "run", _fake_run)

        ReceivingExceptionService(db).explain(
            short_delivery.receipt_line.id, short_delivery.clerk,
        )

        entry = db.query(AIActionLog).filter(
            AIActionLog.action == "receiving_exception"
        ).one()
        assert entry.ai_model == "test-model"
        assert entry.prompt_version == "v1"
        assert entry.confidence == pytest.approx(0.88)

    def test_a_clean_delivery_has_nothing_to_explain(
        self, db, tenant, make_user
    ):
        """And says so, rather than inventing a problem."""
        clerk = make_user(UserRole.AP_CLERK)
        item = Item(
            id=uuid.uuid4(), tenant_id=tenant.id, sku="SKU-OK", name="Widget",
            uom="each", standard_cost=Decimal("10.00"),
        )
        location = StockLocation(
            id=uuid.uuid4(), tenant_id=tenant.id, code="BAY2", name="Bay",
        )
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Good Ltd",
            status=VendorStatus.ACTIVE, created_by=clerk["id"],
        )
        db.add_all([item, location, vendor])
        db.flush()
        order = PurchaseOrder(
            id=uuid.uuid4(), tenant_id=tenant.id, po_number="PO-OK",
            vendor_id=vendor.id, vendor_name="Good Ltd",
            order_date=date(2026, 7, 1), expected_date=date(2026, 8, 10),
            total_amount=100, current_state=PurchaseOrderState.ISSUED,
            created_by=clerk["id"], correlation_id=uuid.uuid4(),
        )
        db.add(order)
        db.flush()
        po_line = PurchaseOrderLine(
            id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=order.id,
            line_number=1, description="Widgets", quantity=10, unit_price=10,
            amount=100, item_id=item.id,
        )
        db.add(po_line)
        db.flush()
        receipt = GoodsReceiptService(db).record_receipt(
            order.id,
            SimpleNamespace(
                received_date=date(2026, 8, 9), delivery_note=None, notes=None,
                location_id=location.id,
                lines=[SimpleNamespace(
                    purchase_order_line_id=po_line.id, quantity_received=10,
                )],
            ),
            clerk,
        )
        line = db.query(GoodsReceiptLine).filter(
            GoodsReceiptLine.goods_receipt_id == receipt.id
        ).one()

        explanation = ReceivingExceptionService(db).explain(line.id, clerk)

        assert explanation["exceptions"] == []
        assert "Nothing to explain" in explanation["ai_note"]
