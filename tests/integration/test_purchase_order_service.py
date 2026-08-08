"""Purchase orders reuse the governance layer rather than reimplementing it.

The value of this module is that almost none of it is new: approval routing,
segregation of duties, transition guards, the hash-chained audit trail and
policy-evaluation snapshots all come from the invoice module. These tests are
mostly about proving that reuse actually happened — a second module that
quietly grew its own approval logic would be the failure worth catching.
"""
import uuid
from decimal import Decimal

import pytest

from app.core.enums import UserRole, PurchaseOrderState, VendorStatus
from app.models.audit_log import AuditLog
from app.models.policy_eval import PolicyEval
from app.models.purchase_order import PurchaseOrder
from app.models.vendor import Vendor
from app.schemas.purchase_order import (
    PurchaseOrderCreate, PurchaseOrderLineCreate, PurchaseOrderUpdate,
)
from app.services.config_provisioning import ConfigProvisioningService
from app.services.purchase_order_service import PurchaseOrderService

pytestmark = pytest.mark.integration


@pytest.fixture
def provisioned(db, tenant, make_user):
    """A tenant with its workflows and approval matrix seeded."""
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    return tenant


@pytest.fixture
def vendor(db, tenant):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
               legal_name="Procurement Vendor", status=VendorStatus.ACTIVE)
    db.add(v)
    db.flush()
    return v


def _order_payload(vendor, quantity=10, unit_price=Decimal("100")):
    return PurchaseOrderCreate(
        vendor_id=vendor.id,
        lines=[PurchaseOrderLineCreate(
            description="Safety helmets", quantity=quantity, unit_price=unit_price,
        )],
    )


def _actions(db, po_id):
    return [
        a.action for a in
        db.query(AuditLog).filter(AuditLog.object_id == po_id).all()
    ]


class TestRaisingAnOrder:

    def test_a_clerk_can_raise_one(self, db, provisioned, vendor, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        order = PurchaseOrderService(db).create_order(_order_payload(vendor), clerk)

        assert order.po_number.startswith("PO-")
        assert order.current_state == PurchaseOrderState.DRAFT
        assert "created" in _actions(db, order.id)

    def test_totals_are_derived_from_the_lines(self, db, provisioned, vendor, make_user):
        """The header must not be able to disagree with what was ordered; the
        three-way match compares against these numbers."""
        clerk = make_user(UserRole.AP_CLERK)
        order = PurchaseOrderService(db).create_order(
            _order_payload(vendor, quantity=10, unit_price=Decimal("100")), clerk
        )
        assert Decimal(order.subtotal_amount) == Decimal("1000")
        assert Decimal(order.total_amount) == Decimal("1000")

    def test_it_opens_a_correlation_chain(self, db, provisioned, vendor, make_user):
        """The receipts and the invoice later join this id."""
        order = PurchaseOrderService(db).create_order(
            _order_payload(vendor), make_user(UserRole.AP_CLERK)
        )
        assert order.correlation_id is not None

    def test_an_approver_cannot_raise_one(self, db, provisioned, vendor, make_user):
        """Buying and approving are separate authorities."""
        with pytest.raises(PermissionError, match="create purchase orders"):
            PurchaseOrderService(db).create_order(
                _order_payload(vendor), make_user(UserRole.MANAGER)
            )

    def test_an_unknown_vendor_is_refused(self, db, provisioned, make_user):
        """A PO names who you are buying from; unlike an invoice upload it
        never invents the vendor."""
        with pytest.raises(ValueError, match="No vendor named"):
            PurchaseOrderService(db).create_order(
                PurchaseOrderCreate(
                    vendor_name="Nobody Ltd",
                    lines=[PurchaseOrderLineCreate(
                        description="x", quantity=1, unit_price=1)],
                ),
                make_user(UserRole.AP_CLERK),
            )


class TestApprovalReusesTheInvoiceGovernance:

    def test_submit_snapshots_the_routing_decision(self, db, provisioned, vendor, make_user):
        """The same approval matrix routes orders and invoices, so a tenant
        configures thresholds once."""
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)

        order, required_role = service.submit_for_approval(order.id, clerk)

        assert order.current_state == PurchaseOrderState.PENDING_APPROVAL
        assert required_role in ("manager", "cfo")
        assert db.query(PolicyEval).filter(
            PolicyEval.object_id == order.id
        ).count() >= 1

    def test_a_large_order_routes_to_the_cfo(self, db, provisioned, vendor, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(
            _order_payload(vendor, quantity=1, unit_price=Decimal("500000")), clerk
        )
        _, required_role = service.submit_for_approval(order.id, clerk)
        assert required_role == "cfo"

    def test_the_raiser_cannot_approve_their_own_order(self, db, provisioned, vendor, make_user):
        """Maker-checker, the same rule the invoice module enforces."""
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)
        service.submit_for_approval(order.id, clerk)

        # Give the raiser approval rights so the SoD rule is what refuses them,
        # not a missing permission.
        raiser_as_approver = {**clerk, "role": UserRole.MANAGER.value}
        with pytest.raises(PermissionError, match="Segregation of duties"):
            service.approve_order(order.id, raiser_as_approver)

    def test_a_blocked_approval_is_audited(self, db, provisioned, vendor, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)
        service.submit_for_approval(order.id, clerk)

        with pytest.raises(PermissionError):
            service.approve_order(order.id, {**clerk, "role": UserRole.MANAGER.value})

        assert "approval_blocked" in _actions(db, order.id)

    def test_a_manager_approves_someone_elses_order(self, db, provisioned, vendor, make_user):
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), make_user(UserRole.AP_CLERK))
        service.submit_for_approval(order.id, make_user(UserRole.AP_CLERK))

        approved = service.approve_order(order.id, make_user(UserRole.MANAGER))

        assert approved.current_state == PurchaseOrderState.APPROVED
        assert approved.approved_by is not None
        assert "approved" in _actions(db, order.id)


class TestTheOrderIsFrozenOnceSubmitted:

    def test_a_draft_can_be_edited(self, db, provisioned, vendor, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)

        updated = service.update_order(
            order.id,
            PurchaseOrderUpdate(lines=[PurchaseOrderLineCreate(
                description="Boots", quantity=2, unit_price=Decimal("250"))]),
            clerk,
        )
        assert Decimal(updated.total_amount) == Decimal("500")

    def test_a_submitted_order_cannot_be_edited(self, db, provisioned, vendor, make_user):
        """Otherwise the approval, the receipt and the invoice would refer to
        different orders."""
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)
        service.submit_for_approval(order.id, clerk)

        with pytest.raises(ValueError, match="only drafts can be changed"):
            service.update_order(
                order.id, PurchaseOrderUpdate(description="sneaky change"), clerk
            )


class TestGuardsApply:

    def test_an_empty_order_cannot_be_submitted(self, db, provisioned, vendor, make_user):
        """A PO with no lines commits to nothing and could never be matched."""
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(
            PurchaseOrderCreate(vendor_id=vendor.id, lines=[]), clerk
        )
        with pytest.raises(ValueError, match="no lines"):
            service.submit_for_approval(order.id, clerk)

    def test_issuing_requires_a_verified_vendor(self, db, provisioned, tenant, make_user):
        """Blocking an unverified vendor at invoice time is too late — the
        order has already been placed and goods may have arrived."""
        pending = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
                         legal_name="Unverified Supplier",
                         status=VendorStatus.PENDING_VERIFICATION)
        db.add(pending)
        db.flush()

        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(pending), clerk)
        service.submit_for_approval(order.id, clerk)
        service.approve_order(order.id, make_user(UserRole.MANAGER))

        with pytest.raises(ValueError, match="not active"):
            service.issue_order(order.id, clerk)


class TestTheFullLifecycle:

    def test_draft_to_issued_leaves_a_complete_trail(self, db, provisioned, vendor, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)

        order = service.create_order(_order_payload(vendor), clerk)
        service.submit_for_approval(order.id, clerk)
        service.approve_order(order.id, make_user(UserRole.MANAGER))
        issued = service.issue_order(order.id, clerk)

        assert issued.current_state == PurchaseOrderState.ISSUED
        assert set(_actions(db, order.id)) >= {
            "created", "submitted_for_approval", "approved", "issued",
        }


class TestTheOrderJoinsTheSameChainAsTheInvoice:
    """The correlation chain and evidence pack were hardcoded to invoices.

    A purchase order's audit entries therefore resolved to no chain at all, so
    its evidence pack came back empty while reporting success — the promise
    that further modules join the same chain was quietly untrue. Both now
    discover chain-owning models from the registry.
    """

    def test_audit_entries_are_linked_to_the_orders_chain(
        self, db, provisioned, vendor, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        order = PurchaseOrderService(db).create_order(_order_payload(vendor), clerk)

        entries = db.query(AuditLog).filter(AuditLog.object_id == order.id).all()
        assert entries
        assert all(e.correlation_id == order.correlation_id for e in entries), (
            "the order's audit entries did not join its chain"
        )

    def test_the_chain_lists_the_order_as_an_object(
        self, db, provisioned, vendor, make_user
    ):
        from app.services.correlation import CorrelationService

        clerk = make_user(UserRole.AP_CLERK)
        order = PurchaseOrderService(db).create_order(_order_payload(vendor), clerk)

        chain = CorrelationService(db).get_chain(
            order.correlation_id, make_user(UserRole.ADMIN)
        )
        assert [(o["object_type"], o["reference"]) for o in chain["objects"]] == [
            ("purchase_order", order.po_number)
        ]

    def test_the_evidence_pack_covers_the_order(self, db, provisioned, vendor, make_user):
        from app.services.evidence_pack import EvidencePackService

        clerk = make_user(UserRole.AP_CLERK)
        service = PurchaseOrderService(db)
        order = service.create_order(_order_payload(vendor), clerk)
        service.submit_for_approval(order.id, clerk)

        pack = EvidencePackService(db).build(
            order.correlation_id, make_user(UserRole.ADMIN)
        )
        assert pack["counts"]["objects"] == 1
        assert pack["counts"]["audit_events"] >= 2
        assert pack["all_chains_verified"] is True
        assert pack["content"]["objects"][0]["object_type"] == "purchase_order"


class TestTheTimelineIsVisibleToWhoeverCanViewTheOrder:
    """The audit timeline promises history is visible to whoever can view the
    object, not only to auditors.

    Its permission map defaults anything unlisted to the far narrower
    audit.view, so purchase orders were hidden from the very clerks who raise
    them — a 403 on the order's own page. Found by opening that page, not by
    any test.
    """

    def test_a_clerk_can_read_an_orders_timeline(self, db, provisioned, vendor, make_user):
        from app.services.audit_service import AuditService

        clerk = make_user(UserRole.AP_CLERK)
        order = PurchaseOrderService(db).create_order(_order_payload(vendor), clerk)

        timeline = AuditService(db).get_timeline("purchase_order", order.id, clerk)
        assert timeline["total_events"] >= 1

    def test_a_role_without_view_still_cannot(self, db, provisioned, vendor, make_user):
        from app.services.audit_service import AuditService

        order = PurchaseOrderService(db).create_order(
            _order_payload(vendor), make_user(UserRole.AP_CLERK)
        )
        with pytest.raises(PermissionError):
            AuditService(db).get_timeline(
                "purchase_order", order.id, make_user(UserRole.USER)
            )

    def test_every_chain_owning_module_is_mapped(self):
        """A module absent from the map silently falls back to audit.view, which
        is how this broke. New chain owners must be listed."""
        from app.services.audit_service import _VIEW_PERMISSION
        from app.services.correlation import chain_owners

        missing = set(chain_owners()) - set(_VIEW_PERMISSION)
        assert not missing, (
            f"these modules have no timeline permission mapped, so only "
            f"auditors can read their history: {missing}"
        )
