"""Every module tells the person who has to act, and every waiting state has a clock.

`NotificationService` was called only from `invoice_service`. A requisition
approver, a tender awarder and a payment releaser were told nothing when work
arrived — the first message any of them got about a decision was an SLA
escalation saying it was late. That makes escalation useless as a signal: it
stops meaning "this slipped" and starts meaning "this exists".

And RFQ was the one workflow with no SLA on any state, so a closed tender —
quoting over, vendors waiting — could sit unawarded forever without becoming
overdue anywhere. A timer that does not exist never breaches, so the gap was
invisible in exactly the place built to make delay visible.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import RequisitionState, RFQState, UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.requisition import PurchaseRequisition, PurchaseRequisitionLine
from app.models.rfq import RFQ
from app.models.vendor import Vendor
from app.schemas.payment import PaymentCreate
from app.services.config_defaults import DEFAULT_WORKFLOWS
from app.services.config_provisioning import ConfigProvisioningService
from app.services.notification_service import NotificationService
from app.services.payment_service import PaymentService
from app.services.requisition_service import RequisitionService
from app.services.sourcing_service import SourcingService

pytestmark = pytest.mark.integration


@pytest.fixture
def sent(monkeypatch):
    """What actually left the building. Delivery swallows its own errors, so
    asserting on the absence of an exception would prove nothing."""
    captured = []
    monkeypatch.setattr(
        NotificationService, "_send",
        lambda self, tenant_id, recipients, subject, body, category=None, link=None: captured.append(
            {"to": list(recipients), "subject": subject, "body": body}
        ),
    )
    return captured


@pytest.fixture
def setup(db, tenant, make_user):
    admin = make_user(UserRole.ADMIN)
    ConfigProvisioningService(db).initialize_defaults(admin)
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Orion Supplies Ltd",
        status=VendorStatus.ACTIVE, iban="PK00REAL0000000000000001",
    )
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant, "vendor": vendor, "admin": admin,
        "clerk": make_user(UserRole.AP_CLERK),
        "manager": make_user(UserRole.MANAGER),
        "cfo": make_user(UserRole.CFO),
    }


def _draft_requisition(db, tenant_id, created_by, number, amount="1000"):
    """A requisition complete enough to submit: the draft->pending guard
    requires at least one line."""
    requisition = PurchaseRequisition(
        id=uuid.uuid4(), tenant_id=tenant_id, requisition_number=number,
        title="Laptops", justification="Four engineers start on the 1st.",
        requested_date=date(2026, 9, 1), estimated_amount=Decimal(amount),
        current_state=RequisitionState.DRAFT, created_by=created_by,
    )
    db.add(requisition)
    db.flush()
    db.add(PurchaseRequisitionLine(
        id=uuid.uuid4(), tenant_id=tenant_id, requisition_id=requisition.id,
        line_number=1, description="Developer laptop", quantity=Decimal("4"),
        estimated_unit_price=Decimal(amount), estimated_amount=Decimal(amount),
    ))
    db.flush()
    return requisition


def _issued_rfq(db, setup, number):
    requisition = PurchaseRequisition(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        requisition_number=f"{number}-REQ", title="Laptops",
        justification="Needed for the new starters.",
        requested_date=date(2026, 9, 1), estimated_amount=Decimal("10000"),
        current_state=RequisitionState.APPROVED, created_by=setup["clerk"]["id"],
    )
    db.add(requisition)
    db.flush()
    rfq = RFQ(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id, rfq_number=number,
        title="Laptops", requisition_id=requisition.id,
        current_state=RFQState.ISSUED, created_by=setup["clerk"]["id"],
    )
    db.add(rfq)
    db.flush()
    return rfq


def _to(sent):
    return {addr for msg in sent for addr in msg["to"]}


class TestWorkArrivingIsAnnounced:
    def test_a_submitted_requisition_reaches_its_approver(self, db, setup, sent):
        requisition = _draft_requisition(
            db, setup["tenant"].id, setup["clerk"]["id"], "REQ-NOTIFY"
        )

        RequisitionService(db).submit_requisition(requisition.id, setup["clerk"])

        assert sent, "nobody was told a requisition needs approving"
        assert setup["manager"]["email"] in _to(sent)
        assert "REQ-NOTIFY" in sent[0]["subject"]

    def test_a_closed_tender_reaches_whoever_awards_it(self, db, setup, sent):
        """Quoting has ended and the vendors are waiting on an answer. Nothing
        else chases this, which is exactly why it has to be announced."""
        rfq = _issued_rfq(db, setup, "RFQ-NOTIFY")

        SourcingService(db).close_rfq(rfq.id, setup["clerk"])

        assert sent, "nobody was told a tender is waiting to be awarded"
        assert setup["manager"]["email"] in _to(sent)
        assert "RFQ-NOTIFY" in sent[-1]["subject"]

    def test_a_payment_awaiting_release_reaches_the_releaser(self, db, setup, sent):
        """Maker-checker means somebody else has to release it, so somebody
        else has to know it is there."""
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=setup["tenant"].id,
            invoice_number="INV-PAY", vendor_name=setup["vendor"].legal_name,
            vendor_id=setup["vendor"].id, invoice_date=date(2026, 8, 1),
            total_amount=Decimal("5000"), current_state="approved",
            created_by=setup["clerk"]["id"],
        )
        db.add(invoice)
        db.flush()
        service = PaymentService(db)
        payment = service.prepare_payment(
            [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
        )
        sent.clear()

        service.submit_for_release(payment.id, setup["clerk"])

        assert sent, "nobody was told a payment is waiting for release"
        assert setup["cfo"]["email"] in _to(sent)

    def test_the_person_who_raised_it_is_not_told(self, db, setup, sent):
        """Segregation of duties refuses them at the decision, so telling them
        it is waiting on somebody is noise."""
        requisition = _draft_requisition(
            db, setup["tenant"].id, setup["manager"]["id"], "REQ-OWN"
        )

        RequisitionService(db).submit_requisition(requisition.id, setup["manager"])

        assert setup["manager"]["email"] not in _to(sent)

    def test_recipients_follow_the_permission_not_a_role_name(self, db, setup, sent):
        """Who may approve is a capability. Naming roles in the notifier would
        drift from roles.py the moment one is granted somewhere new."""
        from app.core.roles import ROLE_PERMISSIONS, PERM_APPROVE_REQUISITION

        requisition = _draft_requisition(
            db, setup["tenant"].id, setup["clerk"]["id"], "REQ-PERM"
        )
        RequisitionService(db).submit_requisition(requisition.id, setup["clerk"])

        told = _to(sent)
        for role in (UserRole.MANAGER, UserRole.CFO):
            assert PERM_APPROVE_REQUISITION in ROLE_PERMISSIONS[role.value]
            assert setup[role.value]["email"] in told
        # The clerk cannot approve one, so is not told about it.
        assert PERM_APPROVE_REQUISITION not in ROLE_PERMISSIONS[UserRole.AP_CLERK.value]
        assert setup["clerk"]["email"] not in told


class TestEveryWaitingStateHasAClock:
    def test_the_closed_tender_state_now_has_an_sla(self):
        """RFQ was the only workflow with no SLA anywhere, and 'closed' is its
        waiting state — the one where people, not the system, are the delay."""
        closed = next(
            s for s in DEFAULT_WORKFLOWS["rfq"] if s[0] == RFQState.CLOSED.value
        )
        sla = closed[-1]
        assert sla.get("hours"), "a closed tender can sit unawarded forever"
        assert sla.get("escalate_to")

    def test_no_workflow_is_left_without_one(self):
        """The gap was invisible because nothing checked for it. This is that
        check: a new workflow with a waiting state and no timer fails here."""
        missing = [
            wf for wf, states in DEFAULT_WORKFLOWS.items()
            if not any((s[-1] or {}).get("hours") for s in states)
        ]
        assert not missing, f"workflows with no SLA on any state: {missing}"

    def test_an_overdue_tender_now_escalates(self, db, setup):
        """End to end: with the SLA in place the runner finds it. Before, the
        same tender was simply never late."""
        from app.models.audit_log import AuditLog
        from app.services.sla_service import SlaService
        from app.utils.datetime_helpers import utc_now

        rfq = _issued_rfq(db, setup, "RFQ-SLA")
        rfq.current_state = RFQState.CLOSED
        rfq.state_entered_at = utc_now() - timedelta(hours=72)
        db.flush()

        result = SlaService(db).run_escalations(setup["admin"])

        assert result["escalated_count"] >= 1
        assert db.query(AuditLog).filter(
            AuditLog.object_id == rfq.id, AuditLog.action == "sla_escalated"
        ).first() is not None

    def test_the_escalation_names_the_tender_not_its_uuid(self, db, setup):
        """The runner built its reference from invoice_number-or-po_number, so
        every other workflow escalated under a raw UUID — a notification about
        a number nobody can look up. Same shape of bug as DR-033."""
        from app.services.sla_service import SlaService
        from app.utils.datetime_helpers import utc_now

        rfq = _issued_rfq(db, setup, "RFQ-NAMED")
        rfq.current_state = RFQState.CLOSED
        rfq.state_entered_at = utc_now() - timedelta(hours=72)
        db.flush()

        result = SlaService(db).run_escalations(setup["admin"])

        assert "RFQ-NAMED" in {item["reference"] for item in result["items"]}
