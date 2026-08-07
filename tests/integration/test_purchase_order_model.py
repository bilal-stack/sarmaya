"""The purchase order models are wired into the governance layer.

They replace a stub with three defects, each of which would have been silent:
it was never imported into the model registry so no table existed; its
vendor_id was an Integer against a UUID vendors.id; and it had no tenant_id,
which would have placed purchase orders outside both RLS and the
application-level tenant scoping — a procurement module with no tenant
boundary, and nothing to say so.

These tests assert the wiring rather than behaviour, because that is what was
broken and what a future model is most likely to get wrong.
"""
import uuid

import pytest

from app.core.database import _tenant_scoped_mappers
from app.core.enums import UserRole, PurchaseOrderState, VendorStatus
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.services.workflow import workflow_models, workflow_type_of

pytestmark = pytest.mark.integration


@pytest.fixture
def order(db, tenant, make_user):
    vendor = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
                    legal_name="PO Vendor", status=VendorStatus.ACTIVE)
    db.add(vendor)
    db.flush()

    po = PurchaseOrder(
        id=uuid.uuid4(), tenant_id=tenant.id,
        po_number=f"PO-{uuid.uuid4().hex[:6]}",
        vendor_id=vendor.id, vendor_name=vendor.legal_name,
        order_date="2026-08-01", total_amount=5000,
        current_state=PurchaseOrderState.DRAFT,
        created_by=make_user(UserRole.ADMIN)["id"],
    )
    db.add(po)
    db.flush()
    db.add(PurchaseOrderLine(
        id=uuid.uuid4(), tenant_id=tenant.id, purchase_order_id=po.id,
        line_number=1, description="Safety helmets",
        quantity=100, unit_price=50, amount=5000,
    ))
    db.flush()
    return po


class TestTheModelsAreRegistered:

    def test_the_tables_exist(self, db, order):
        """The stub was never imported, so create_all never made its table."""
        assert db.query(PurchaseOrder).filter(PurchaseOrder.id == order.id).first()
        assert db.query(PurchaseOrderLine).filter(
            PurchaseOrderLine.purchase_order_id == order.id
        ).count() == 1

    def test_the_vendor_link_works(self, db, order):
        """The stub typed vendor_id Integer against a UUID vendors.id."""
        assert order.vendor is not None
        assert order.vendor.legal_name == "PO Vendor"


class TestTheyInheritTheGovernanceLayer:

    def test_both_are_tenant_scoped(self):
        """No tenant_id on the stub meant no RLS policy and no ORM scoping."""
        scoped = {m.__name__ for m in _tenant_scoped_mappers()}
        assert "PurchaseOrder" in scoped
        assert "PurchaseOrderLine" in scoped

    def test_the_order_declares_its_workflow(self, order):
        assert workflow_type_of(order) == "purchase_order"
        assert workflow_models()["purchase_order"] is PurchaseOrder

    def test_it_carries_a_correlation_id_column(self, order):
        """A PO starts the chain its receipts and invoice later join."""
        assert hasattr(order, "correlation_id")

    def test_it_has_an_sla_timer_start(self, order):
        """state_entered_at is what prices every SLA deadline."""
        assert order.state_entered_at is not None


class TestLinesCarryReceiptProgress:

    def test_received_quantity_starts_at_zero(self, db, order):
        line = db.query(PurchaseOrderLine).filter(
            PurchaseOrderLine.purchase_order_id == order.id
        ).first()
        assert float(line.received_quantity) == 0.0

    def test_lines_cascade_with_the_order(self, db, order):
        db.delete(order)
        db.flush()
        assert db.query(PurchaseOrderLine).filter(
            PurchaseOrderLine.purchase_order_id == order.id
        ).count() == 0
