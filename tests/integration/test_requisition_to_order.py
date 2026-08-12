"""The upstream half of procure-to-pay: need → tender → quotes → award → order.

Until this existed the purchase order was the first record in the chain, so an
approver had nothing to check it against and the audit trail could prove an
order was properly approved without ever answering *why it was ordered at all*.

The tests below are mostly about the controls rather than the happy path,
because the happy path is not where procurement goes wrong. The decisions worth
pinning down are: who may approve a need, who may run a tender, who may pick
the winner, whether a losing quote can be revised after the field is known, and
whether an order can quietly exceed what was approved.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import (
    UserRole, RequisitionState, RFQState, QuoteState, VendorStatus,
    PurchaseOrderState,
)
from app.models.audit_log import AuditLog
from app.models.vendor import Vendor
from app.schemas.requisition import RequisitionCreate, RequisitionLineCreate
from app.schemas.sourcing import RFQCreate, QuoteCreate, QuoteLineCreate
from app.services.config_provisioning import ConfigProvisioningService
from app.services.requisition_service import RequisitionService
from app.services.sourcing_service import SourcingService

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendors = {}
    for name in ("Cheap Supplies", "Mid Supplies", "Costly Supplies"):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name=name,
            status=VendorStatus.ACTIVE,
        )
        db.add(vendor)
        vendors[name] = vendor
    db.flush()
    return {
        "tenant": tenant,
        "vendors": vendors,
        # The buyer runs the tender but cannot award it.
        "clerk": make_user(UserRole.AP_CLERK),
        # The manager approves the need and picks the winner.
        "manager": make_user(UserRole.MANAGER),
        "cfo": make_user(UserRole.CFO),
        "admin": make_user(UserRole.ADMIN),
    }


def _requisition(db, setup, actor=None, amount="10000", quantity="1"):
    return RequisitionService(db).create_requisition(
        RequisitionCreate(
            title="Laptops for the new starters",
            justification="Four new engineers start next month and have no machines.",
            budget_code="ENG-2026",
            department="Engineering",
            lines=[RequisitionLineCreate(
                description="Developer laptop", quantity=Decimal(quantity),
                estimated_unit_price=Decimal(amount),
            )],
        ),
        actor or setup["clerk"],
    )


def _approved_requisition(db, setup, amount="10000"):
    service = RequisitionService(db)
    requisition = _requisition(db, setup, amount=amount)
    service.submit_requisition(requisition.id, setup["clerk"])
    return service.approve_requisition(requisition.id, setup["manager"])


def _issued_rfq(db, setup, requisition):
    service = SourcingService(db)
    rfq = service.create_rfq(
        RFQCreate(
            requisition_id=requisition.id,
            title="Laptops",
            vendor_ids=[v.id for v in setup["vendors"].values()],
        ),
        setup["clerk"],
    )
    return service.issue_rfq(rfq.id, setup["clerk"])


def _quote(db, setup, rfq, vendor_name, amount, compliant=True, reason=None):
    return SourcingService(db).record_quote(
        rfq.id,
        QuoteCreate(
            vendor_id=setup["vendors"][vendor_name].id,
            is_compliant=compliant,
            non_compliance_reason=reason,
            lines=[QuoteLineCreate(
                description="Developer laptop", quantity=Decimal("1"),
                unit_price=Decimal(amount),
            )],
        ),
        setup["clerk"],
    )


def _actions(db, object_id):
    return [
        a.action for a in
        db.query(AuditLog).filter(AuditLog.object_id == object_id).all()
    ]


class TestRaisingTheNeed:

    def test_a_requisition_starts_the_chain(self, db, setup):
        """The correlation id everything downstream inherits begins here."""
        requisition = _requisition(db, setup)

        assert requisition.requisition_number.startswith("REQ-")
        assert requisition.current_state == RequisitionState.DRAFT
        assert requisition.correlation_id is not None
        assert Decimal(requisition.estimated_amount) == Decimal("10000")

    def test_a_justification_is_required(self, db, setup):
        """It is what the approver is deciding on, so an empty one is refused
        before it reaches them."""
        with pytest.raises(ValueError, match="justification"):
            RequisitionCreate(
                title="Something", justification="   ",
                lines=[RequisitionLineCreate(
                    description="x", quantity=Decimal("1"),
                    estimated_unit_price=Decimal("1"),
                )],
            )

    def test_submitting_without_lines_is_refused(self, db, setup):
        """A requisition asking for nothing gives the approver nothing to
        decide on and a quote nothing to price."""
        requisition = RequisitionService(db).create_requisition(
            RequisitionCreate(
                title="Empty", justification="Nothing in particular, honestly.",
                lines=[],
            ),
            setup["clerk"],
        )
        with pytest.raises(ValueError):
            RequisitionService(db).submit_requisition(requisition.id, setup["clerk"])


class TestApprovingTheNeed:

    def test_the_requester_cannot_approve_their_own(self, db, setup):
        """Otherwise the record is a permission slip the requester wrote
        themselves."""
        service = RequisitionService(db)
        requisition = _requisition(db, setup)
        service.submit_requisition(requisition.id, setup["clerk"])

        # Give the requester approval rights, so it is the SoD rule that
        # refuses them rather than a missing permission.
        requester_with_rights = {**setup["clerk"], "role": UserRole.MANAGER.value}
        with pytest.raises(PermissionError, match="Segregation of duties"):
            service.approve_requisition(requisition.id, requester_with_rights)

    def test_a_refused_approval_is_audited(self, db, setup):
        service = RequisitionService(db)
        requisition = _requisition(db, setup)
        service.submit_requisition(requisition.id, setup["clerk"])
        with pytest.raises(PermissionError):
            service.approve_requisition(
                requisition.id, {**setup["clerk"], "role": UserRole.MANAGER.value}
            )

        assert "approval_blocked" in _actions(db, requisition.id)

    def test_the_approval_matrix_applies(self, db, setup):
        """The thresholds that decide who may approve spending decide who may
        authorise a request to spend — reused, so the two cannot drift."""
        service = RequisitionService(db)
        requisition = _requisition(db, setup, amount="900000")
        service.submit_requisition(requisition.id, setup["clerk"])

        with pytest.raises(PermissionError, match="only approve"):
            service.approve_requisition(requisition.id, setup["manager"])

        approved = service.approve_requisition(requisition.id, setup["cfo"])
        assert approved.current_state == RequisitionState.APPROVED

    def test_someone_else_approves_it(self, db, setup):
        approved = _approved_requisition(db, setup)

        assert approved.current_state == RequisitionState.APPROVED
        assert approved.approved_by is not None
        assert str(approved.approved_by) != setup["clerk"]["id"]
        assert "approved" in _actions(db, approved.id)


class TestRunningTheTender:

    def test_an_rfq_needs_an_approved_requisition(self, db, setup):
        """Going to market on an unapproved need commits the company's name to
        a purchase nobody authorised."""
        requisition = _requisition(db, setup)

        with pytest.raises(ValueError, match="not approved"):
            SourcingService(db).create_rfq(
                RFQCreate(requisition_id=requisition.id,
                          vendor_ids=[v.id for v in setup["vendors"].values()]),
                setup["clerk"],
            )

    def test_the_rfq_inherits_the_requisitions_chain(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)

        assert rfq.correlation_id == requisition.correlation_id

    def test_a_single_invited_vendor_cannot_be_issued(self, db, setup):
        """One quote is not a comparison. A single-source purchase is
        legitimate but is a different decision."""
        requisition = _approved_requisition(db, setup)
        service = SourcingService(db)
        rfq = service.create_rfq(
            RFQCreate(requisition_id=requisition.id,
                      vendor_ids=[setup["vendors"]["Cheap Supplies"].id]),
            setup["clerk"],
        )

        with pytest.raises(ValueError):
            service.issue_rfq(rfq.id, setup["clerk"])

    def test_a_blocked_vendor_cannot_be_invited(self, db, setup):
        """Inviting a blocked vendor to quote is how a blocked vendor gets
        work."""
        requisition = _approved_requisition(db, setup)
        setup["vendors"]["Cheap Supplies"].status = VendorStatus.BLOCKED
        db.flush()

        with pytest.raises(ValueError, match="not active"):
            SourcingService(db).create_rfq(
                RFQCreate(requisition_id=requisition.id,
                          vendor_ids=[setup["vendors"]["Cheap Supplies"].id]),
                setup["clerk"],
            )

    def test_an_uninvited_vendor_cannot_quote(self, db, setup):
        requisition = _approved_requisition(db, setup)
        service = SourcingService(db)
        rfq = service.create_rfq(
            RFQCreate(requisition_id=requisition.id, vendor_ids=[
                setup["vendors"]["Cheap Supplies"].id,
                setup["vendors"]["Mid Supplies"].id,
            ]),
            setup["clerk"],
        )
        service.issue_rfq(rfq.id, setup["clerk"])

        with pytest.raises(ValueError, match="not invited"):
            _quote(db, setup, rfq, "Costly Supplies", "9000")

    def test_a_vendor_cannot_quote_twice(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")

        with pytest.raises(ValueError, match="already quoted"):
            _quote(db, setup, rfq, "Cheap Supplies", "8000")


class TestQuotesLockWhenTheRfqCloses:
    """The control that makes the quotes evidence rather than a record of what
    someone wrote down once the field was known."""

    def test_no_quote_after_closing(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")
        _quote(db, setup, rfq, "Mid Supplies", "9500")
        SourcingService(db).close_rfq(rfq.id, setup["clerk"])

        with pytest.raises(ValueError, match="locked"):
            _quote(db, setup, rfq, "Costly Supplies", "8500")

    def test_no_further_invitations_after_closing(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")
        SourcingService(db).close_rfq(rfq.id, setup["clerk"])

        with pytest.raises(ValueError, match="locked"):
            SourcingService(db).invite_vendor(
                rfq.id, setup["vendors"]["Costly Supplies"].id, setup["clerk"]
            )

    def test_closing_snapshots_the_field(self, db, setup):
        """What was on the table at the moment quoting ended, recorded then
        rather than reconstructed later."""
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")
        _quote(db, setup, rfq, "Mid Supplies", "9500")
        SourcingService(db).close_rfq(rfq.id, setup["clerk"])

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == rfq.id, AuditLog.action == "closed")
            .first()
        )
        assert entry is not None
        assert len(entry.after_value["quotes"]) == 2


class TestComparingQuotes:

    def test_the_lowest_compliant_quote_is_identified(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        cheap = _quote(db, setup, rfq, "Cheap Supplies", "9000")
        _quote(db, setup, rfq, "Mid Supplies", "9500")

        comparison = SourcingService(db).compare_quotes(rfq.id, setup["clerk"])
        assert comparison["lowest_compliant_quote_id"] == cheap.id

    def test_a_non_compliant_quote_does_not_set_the_benchmark(self, db, setup):
        """A cheaper bid for the wrong specification is a different quote, not
        a better one — otherwise every compliant award looks like an override
        that needs justifying."""
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "5000",
               compliant=False, reason="Wrong specification")
        mid = _quote(db, setup, rfq, "Mid Supplies", "9500")

        comparison = SourcingService(db).compare_quotes(rfq.id, setup["clerk"])
        assert comparison["lowest_compliant_quote_id"] == mid.id

    def test_vendors_who_never_answered_are_listed(self, db, setup):
        """A tender answered by one of three invitees is a different decision
        from one answered by all three."""
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")

        comparison = SourcingService(db).compare_quotes(rfq.id, setup["clerk"])
        assert set(comparison["no_response_vendors"]) == {
            "Mid Supplies", "Costly Supplies"
        }

    def test_it_flags_the_market_coming_in_over_the_estimate(self, db, setup):
        requisition = _approved_requisition(db, setup, amount="5000")
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")

        comparison = SourcingService(db).compare_quotes(rfq.id, setup["clerk"])
        assert comparison["lowest_exceeds_estimate"] is True


class TestAwarding:

    def _closed_rfq(self, db, setup, requisition=None):
        requisition = requisition or _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        cheap = _quote(db, setup, rfq, "Cheap Supplies", "9000")
        costly = _quote(db, setup, rfq, "Costly Supplies", "9800")
        SourcingService(db).close_rfq(rfq.id, setup["clerk"])
        return rfq, cheap, costly, requisition

    def test_the_buyer_who_ran_the_tender_cannot_award_it(self, db, setup):
        """Collecting the quotes and choosing the winner are different
        authorities."""
        rfq, cheap, _, _ = self._closed_rfq(db, setup)

        with pytest.raises(PermissionError, match="award"):
            SourcingService(db).award_quote(
                rfq.id, cheap.id, None, setup["clerk"]
            )

    def test_the_lowest_compliant_quote_needs_no_justification(self, db, setup):
        rfq, cheap, _, _ = self._closed_rfq(db, setup)

        awarded = SourcingService(db).award_quote(
            rfq.id, cheap.id, None, setup["manager"]
        )
        assert awarded.current_state == RFQState.AWARDED
        assert awarded.awarded_quote_id == cheap.id

    def test_anything_else_requires_a_written_reason(self, db, setup):
        """The single most examined decision in procurement."""
        rfq, _, costly, _ = self._closed_rfq(db, setup)

        with pytest.raises(ValueError, match="Record why"):
            SourcingService(db).award_quote(
                rfq.id, costly.id, None, setup["manager"]
            )

    def test_with_a_reason_it_is_allowed_and_recorded(self, db, setup):
        rfq, cheap, costly, _ = self._closed_rfq(db, setup)

        awarded = SourcingService(db).award_quote(
            rfq.id, costly.id, "Next-day delivery; the cheaper vendor quoted six weeks.",
            setup["manager"],
        )
        assert awarded.awarded_quote_id == costly.id

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == rfq.id, AuditLog.action == "awarded")
            .first()
        )
        assert entry.after_value["was_lowest_compliant"] is False
        # The figure it was measured against, kept so a later reader need not
        # reconstruct the field from the quotes as they stand afterwards.
        assert entry.after_value["lowest_compliant_amount"] == str(cheap.total_amount)
        assert "six weeks" in entry.comment

    def test_a_non_compliant_quote_cannot_be_awarded(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        bad = _quote(db, setup, rfq, "Cheap Supplies", "5000",
                     compliant=False, reason="Wrong specification")
        _quote(db, setup, rfq, "Mid Supplies", "9500")
        SourcingService(db).close_rfq(rfq.id, setup["clerk"])

        with pytest.raises(ValueError, match="non-compliant"):
            SourcingService(db).award_quote(rfq.id, bad.id, "We want it", setup["manager"])

    def test_losing_quotes_are_marked_rejected(self, db, setup):
        rfq, cheap, costly, _ = self._closed_rfq(db, setup)
        SourcingService(db).award_quote(rfq.id, cheap.id, None, setup["manager"])

        db.refresh(costly)
        assert costly.current_state == QuoteState.REJECTED


class TestConvertingToAnOrder:

    def _awarded(self, db, setup, estimate="10000", quote="9000"):
        requisition = _approved_requisition(db, setup, amount=estimate)
        rfq = _issued_rfq(db, setup, requisition)
        cheap = _quote(db, setup, rfq, "Cheap Supplies", quote)
        _quote(db, setup, rfq, "Mid Supplies", str(Decimal(quote) + Decimal("500")))
        service = SourcingService(db)
        service.close_rfq(rfq.id, setup["clerk"])
        service.award_quote(rfq.id, cheap.id, None, setup["manager"])
        return rfq, requisition, cheap

    def test_the_order_carries_the_chain_end_to_end(self, db, setup):
        """The point of the whole module: need, tender, quotes, award and
        order all read as one story."""
        rfq, requisition, _ = self._awarded(db, setup)

        order = SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

        assert order.correlation_id == requisition.correlation_id
        assert order.current_state == PurchaseOrderState.DRAFT
        assert order.vendor_name == "Cheap Supplies"

    def test_an_unawarded_rfq_cannot_be_converted(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        _quote(db, setup, rfq, "Cheap Supplies", "9000")

        with pytest.raises(ValueError, match="not awarded"):
            SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

    def test_an_award_above_the_approved_estimate_is_refused(self, db, setup):
        """The approval was granted against that figure. The market coming back
        higher is normal — it just needs re-approving rather than absorbing."""
        rfq, _, _ = self._awarded(db, setup, estimate="5000", quote="9000")

        with pytest.raises(ValueError, match="re-approval"):
            SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

    def test_one_approval_cannot_cover_two_orders(self, db, setup):
        rfq, requisition, _ = self._awarded(db, setup)
        SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

        db.refresh(requisition)
        assert requisition.current_state == RequisitionState.CONVERTED

        with pytest.raises(ValueError, match="already been converted"):
            SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

    def test_the_requisition_records_what_became_of_it(self, db, setup):
        """Readable from the need without following the chain forward."""
        rfq, requisition, _ = self._awarded(db, setup)
        order = SourcingService(db).convert_award_to_order(rfq.id, setup["clerk"])

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == requisition.id,
                    AuditLog.action == "converted")
            .first()
        )
        assert entry is not None
        assert entry.after_value["po_number"] == order.po_number


class TestItJoinsTheGovernanceLayer:

    def test_the_whole_chain_shares_one_correlation_id(self, db, setup):
        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        quote = _quote(db, setup, rfq, "Cheap Supplies", "9000")
        _quote(db, setup, rfq, "Mid Supplies", "9500")
        service = SourcingService(db)
        service.close_rfq(rfq.id, setup["clerk"])
        service.award_quote(rfq.id, quote.id, None, setup["manager"])
        order = service.convert_award_to_order(rfq.id, setup["clerk"])

        chain = requisition.correlation_id
        assert rfq.correlation_id == chain
        assert quote.correlation_id == chain
        assert order.correlation_id == chain

    def test_the_requisition_chain_verifies(self, db, setup):
        from app.services.audit_integrity import verify_object_chain

        requisition = _approved_requisition(db, setup)
        assert verify_object_chain(db, "requisition", requisition.id)["verified"] is True

    def test_the_timeline_is_readable_by_a_requisition_viewer(self, db, setup):
        from app.services.audit_service import AuditService

        requisition = _approved_requisition(db, setup)
        timeline = AuditService(db).get_timeline(
            "requisition", requisition.id, setup["clerk"]
        )
        assert timeline["total_events"] >= 2


class TestTheChainReadsInNames:
    """A correlation chain is meant to be read by a person.

    References were resolved by a hardcoded chain of getattr fallbacks that
    every new module had to remember to extend — and payments already had not,
    so a payment appeared in its own story as a raw UUID. Each model now
    declares its own REFERENCE_FIELD.
    """

    def test_every_chain_owner_declares_how_it_names_itself(self):
        from app.services.correlation import chain_owners

        missing = [
            object_type for object_type, model in chain_owners().items()
            if not getattr(model, "REFERENCE_FIELD", None)
        ]
        assert not missing, (
            f"these join correlation chains but would show as a raw UUID: {missing}"
        )

    def test_the_upstream_records_appear_by_reference(self, db, setup):
        from app.services.correlation import CorrelationService

        requisition = _approved_requisition(db, setup)
        rfq = _issued_rfq(db, setup, requisition)
        quote = _quote(db, setup, rfq, "Cheap Supplies", "9000")
        _quote(db, setup, rfq, "Mid Supplies", "9500")
        service = SourcingService(db)
        service.close_rfq(rfq.id, setup["clerk"])
        service.award_quote(rfq.id, quote.id, None, setup["manager"])
        service.convert_award_to_order(rfq.id, setup["clerk"])

        chain = CorrelationService(db).get_chain(
            requisition.correlation_id, setup["admin"]
        )
        references = {o["object_type"]: o["reference"] for o in chain["objects"]}

        assert references["requisition"].startswith("REQ-")
        assert references["rfq"].startswith("RFQ-")
        assert references["purchase_order"].startswith("PO-")
        # A quote has no number; it is identified by who gave it, which is what
        # a reader of the chain actually wants to know.
        assert references["quote"] in ("Cheap Supplies", "Mid Supplies")
