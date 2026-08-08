"""Three-way matching: order versus delivery versus bill.

The control that stops an organisation paying for what it never ordered and
never received. Two-way matching catches a supplier billing the wrong amount;
only the third leg — what actually arrived — catches billing for goods that
never came, which is the case these tests care most about.

Matching is advisory on its own and enforced by the invoice approval gate, so
both are covered: the verdict, and the refusal it produces.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import UserRole, VendorStatus, InvoiceState
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.policy import Policy
from app.models.vendor import Vendor
from app.schemas.purchase_order import (
    PurchaseOrderCreate, PurchaseOrderLineCreate,
    GoodsReceiptCreate, GoodsReceiptLineCreate,
)
from app.services.config_provisioning import ConfigProvisioningService
from app.services.goods_receipt_service import GoodsReceiptService
from app.services.invoice_service import InvoiceService
from app.services.purchase_order_service import PurchaseOrderService
from app.services.three_way_match import (
    ThreeWayMatchService, MATCHED, WITHIN_TOLERANCE, MISMATCHED, UNMATCHED,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendor = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
                    legal_name="Match Vendor", status=VendorStatus.ACTIVE)
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant,
        "vendor": vendor,
        "clerk": make_user(UserRole.AP_CLERK),
        "manager": make_user(UserRole.MANAGER),
    }


def _issued_order(db, setup, quantity=10, unit_price=Decimal("100")):
    """A purchase order taken all the way to issued, ready to receive against."""
    service = PurchaseOrderService(db)
    order = service.create_order(
        PurchaseOrderCreate(
            vendor_id=setup["vendor"].id,
            lines=[PurchaseOrderLineCreate(
                description="Widgets", quantity=quantity, unit_price=unit_price)],
        ),
        setup["clerk"],
    )
    service.submit_for_approval(order.id, setup["clerk"])
    service.approve_order(order.id, setup["manager"])
    return service.issue_order(order.id, setup["clerk"])


def _receive(db, setup, order, quantity):
    line = order.lines[0]
    return GoodsReceiptService(db).record_receipt(
        order.id,
        GoodsReceiptCreate(lines=[GoodsReceiptLineCreate(
            purchase_order_line_id=line.id, quantity_received=Decimal(quantity))]),
        setup["clerk"],
    )


def _invoice(db, setup, order, amount):
    inv = Invoice(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        vendor_id=setup["vendor"].id, vendor_name=setup["vendor"].legal_name,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
        current_state=InvoiceState.PENDING_APPROVAL,
        purchase_order_id=order.id,
        correlation_id=order.correlation_id,
        created_by=setup["clerk"]["id"],
    )
    db.add(inv)
    db.flush()
    return inv


class TestRecordingWhatArrived:

    def test_a_receipt_raises_the_received_quantity(self, db, setup):
        order = _issued_order(db, setup, quantity=10)
        _receive(db, setup, order, 4)

        db.refresh(order.lines[0])
        assert Decimal(order.lines[0].received_quantity) == Decimal("4")

    def test_receipts_accumulate(self, db, setup):
        """Deliveries arrive in parts; the running total is what the match reads."""
        order = _issued_order(db, setup, quantity=10)
        _receive(db, setup, order, 4)
        _receive(db, setup, order, 6)

        db.refresh(order.lines[0])
        assert Decimal(order.lines[0].received_quantity) == Decimal("10")

    def test_a_return_is_recorded_not_erased(self, db, setup):
        """A negative quantity appends a correction, keeping the history of
        what was once claimed to have arrived."""
        order = _issued_order(db, setup, quantity=10)
        _receive(db, setup, order, 10)
        _receive(db, setup, order, -3)

        db.refresh(order.lines[0])
        assert Decimal(order.lines[0].received_quantity) == Decimal("7")
        receipts = GoodsReceiptService(db).list_for_order(order.id, setup["clerk"])
        assert len(receipts) == 2

    def test_an_approver_cannot_record_a_receipt(self, db, setup):
        """Whoever confirms goods arrived must not be who authorised the spend,
        or the delivery leg verifies nothing."""
        order = _issued_order(db, setup, quantity=10)
        with pytest.raises(PermissionError, match="record goods receipts"):
            GoodsReceiptService(db).record_receipt(
                order.id,
                GoodsReceiptCreate(lines=[GoodsReceiptLineCreate(
                    purchase_order_line_id=order.lines[0].id, quantity_received=1)]),
                setup["manager"],
            )

    def test_cannot_receive_against_an_unissued_order(self, db, setup):
        """Goods cannot arrive for an order the vendor was never sent."""
        service = PurchaseOrderService(db)
        order = service.create_order(
            PurchaseOrderCreate(
                vendor_id=setup["vendor"].id,
                lines=[PurchaseOrderLineCreate(
                    description="Widgets", quantity=5, unit_price=10)],
            ),
            setup["clerk"],
        )
        with pytest.raises(ValueError, match="never sent"):
            _receive(db, setup, order, 1)

    def test_the_receipt_joins_the_orders_chain(self, db, setup):
        order = _issued_order(db, setup)
        receipt = _receive(db, setup, order, 10)
        assert receipt.correlation_id == order.correlation_id


class TestTheVerdict:

    def test_a_clean_match(self, db, setup):
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 10)
        invoice = _invoice(db, setup, order, "1000")

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == MATCHED

    def test_an_invoice_with_no_order_has_nothing_to_match(self, db, setup):
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=setup["tenant"].id,
            vendor_id=setup["vendor"].id, vendor_name="Match Vendor",
            invoice_number="INV-NOPO", invoice_date=date(2026, 8, 1),
            total_amount=100, current_state=InvoiceState.PENDING_APPROVAL,
            created_by=setup["clerk"]["id"],
        )
        db.add(invoice)
        db.flush()

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == UNMATCHED

    def test_billing_for_an_undelivered_order_is_caught(self, db, setup):
        """The case only the third leg catches: the amount is exactly right,
        but nothing ever arrived."""
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        invoice = _invoice(db, setup, order, "1000")

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == MISMATCHED
        assert any(d["kind"] == "receipt" for d in result["discrepancies"])

    def test_over_billing_against_the_order_is_caught(self, db, setup):
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 10)
        invoice = _invoice(db, setup, order, "5000")

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == MISMATCHED
        assert any(d["kind"] == "amount" for d in result["discrepancies"])

    def test_a_short_delivery_billed_in_full_is_caught(self, db, setup):
        """Half arrived, all of it invoiced — the partial case a header-only
        total check would pass."""
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 5)
        invoice = _invoice(db, setup, order, "1000")

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == MISMATCHED
        kinds = {d["kind"] for d in result["discrepancies"]}
        assert "quantity" in kinds or "value" in kinds

    def test_a_trivial_discrepancy_is_tolerated(self, db, setup):
        """A control that fails on every rounding difference gets switched off,
        and a control that is switched off protects nothing."""
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 10)
        invoice = _invoice(db, setup, order, "1010")   # 1% over, default allows 2%

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == WITHIN_TOLERANCE

    def test_the_tolerance_is_configurable(self, db, setup):
        """Tightening it is configuration, not a deploy."""
        db.add(Policy(
            id=uuid.uuid4(), tenant_id=setup["tenant"].id,
            policy_type="three_way_match", policy_name="three_way_match",
            rule_config={"amount_percent": 0.0, "quantity_percent": 0.0},
            is_active=True,
        ))
        db.flush()

        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 10)
        invoice = _invoice(db, setup, order, "1010")

        result = ThreeWayMatchService(db).match_invoice(invoice, setup["tenant"].id)
        assert result["result"] == MISMATCHED


class TestTheApprovalGate:

    def test_a_mismatched_invoice_cannot_be_approved(self, db, setup):
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        invoice = _invoice(db, setup, order, "1000")   # nothing received

        with pytest.raises(ValueError, match="does not match its purchase order"):
            InvoiceService(db).approve_invoice(invoice.id, setup["manager"])

    def test_the_refusal_is_audited_with_its_reason(self, db, setup):
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        invoice = _invoice(db, setup, order, "1000")

        with pytest.raises(ValueError):
            InvoiceService(db).approve_invoice(invoice.id, setup["manager"])

        blocks = [
            a for a in db.query(AuditLog).filter(AuditLog.object_id == invoice.id).all()
            if a.action == "approval_blocked"
        ]
        assert blocks, "an attempt to pay for undelivered goods left no trail"
        assert "three_way_match_failed" in (blocks[-1].comment or "")

    def test_a_matching_invoice_approves(self, db, setup):
        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 10)
        invoice = _invoice(db, setup, order, "1000")

        approved = InvoiceService(db).approve_invoice(invoice.id, setup["manager"])
        assert str(getattr(approved.current_state, "value", approved.current_state)) == "approved"

    def test_an_invoice_without_an_order_is_unaffected(self, db, setup):
        """The gate must not block the ordinary non-PO invoice flow."""
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=setup["tenant"].id,
            vendor_id=setup["vendor"].id, vendor_name="Match Vendor",
            invoice_number="INV-PLAIN", invoice_date=date(2026, 8, 1),
            total_amount=500, current_state=InvoiceState.PENDING_APPROVAL,
            created_by=setup["clerk"]["id"],
        )
        db.add(invoice)
        db.flush()

        approved = InvoiceService(db).approve_invoice(invoice.id, setup["manager"])
        assert str(getattr(approved.current_state, "value", approved.current_state)) == "approved"


class TestTheWholeStoryInOnePlace:
    """Order, deliveries and invoice under one correlation id.

    This is what the correlation chain is for, and it is where the modules'
    different shapes bite: a goods receipt has no vendor, no total and no
    state, because it records that something arrived rather than being a
    financial document. Assuming the invoice shape made the entire pack 500
    the first time a receipt joined a chain — found by building one against a
    running server, not by any test that existed.
    """

    def test_the_pack_spans_every_module_in_the_chain(self, db, setup, make_user):
        from app.services.evidence_pack import EvidencePackService

        order = _issued_order(db, setup, quantity=10, unit_price=Decimal("100"))
        _receive(db, setup, order, 6)
        _receive(db, setup, order, 4)
        _invoice(db, setup, order, "1000")

        pack = EvidencePackService(db).build(
            order.correlation_id, make_user(UserRole.ADMIN)
        )

        kinds = {o["object_type"] for o in pack["content"]["objects"]}
        assert kinds == {"purchase_order", "goods_receipt", "invoice"}
        assert pack["counts"]["objects"] == 4      # 1 order, 2 receipts, 1 invoice
        assert pack["all_chains_verified"] is True

    def test_a_receipt_renders_without_invoice_only_fields(self, db, setup, make_user):
        from app.services.evidence_pack import EvidencePackService

        order = _issued_order(db, setup)
        _receive(db, setup, order, 10)

        pack = EvidencePackService(db).build(
            order.correlation_id, make_user(UserRole.ADMIN)
        )
        receipt = next(
            o for o in pack["content"]["objects"] if o["object_type"] == "goods_receipt"
        )
        assert receipt["reference"].startswith("GRN-")
        assert receipt["state"] is None
        assert receipt["total_amount"] == 0

    def test_the_chain_view_lists_them_too(self, db, setup, make_user):
        from app.services.correlation import CorrelationService

        order = _issued_order(db, setup)
        _receive(db, setup, order, 10)

        chain = CorrelationService(db).get_chain(
            order.correlation_id, make_user(UserRole.ADMIN)
        )
        assert {o["object_type"] for o in chain["objects"]} == {
            "purchase_order", "goods_receipt",
        }
