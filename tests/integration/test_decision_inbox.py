"""Integration tests for the Decision Inbox (decision_inbox_service.py).

Verifies each pending invoice surfaces once under its most-blocking action
(duplicate > vendor > approval), that items are filtered to what the caller can
do, and that ordering/links are correct.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.services.decision_inbox_service import DecisionInboxService
from app.services.config_provisioning import ConfigProvisioningService

pytestmark = pytest.mark.integration


def _vendor(db, tenant_id, status=VendorStatus.ACTIVE):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id,
               legal_name=f"V-{uuid.uuid4().hex[:6]}", status=status)
    db.add(v)
    db.flush()
    return v


def _invoice(db, tenant_id, created_by, amount, *, vendor=None, state="pending_approval",
             potential_dup=None, dup_ack=False):
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name if vendor else "Acme",
        vendor_id=vendor.id if vendor else None,
        invoice_date=date(2026, 1, 1),
        total_amount=amount,
        current_state=state,
        created_by=created_by,
        potential_duplicate_id=potential_dup,
        duplicate_acknowledged=dup_ack,
    )
    db.add(inv)
    db.flush()
    return inv


def _scenario(db, tenant, created_by):
    """One invoice of each blocker type, plus a manager-level approval."""
    active = _vendor(db, tenant.id, VendorStatus.ACTIVE)
    unverified = _vendor(db, tenant.id, VendorStatus.PENDING_VERIFICATION)

    original = _invoice(db, tenant.id, created_by, 100_000, vendor=active, state="approved")
    flagged = _invoice(db, tenant.id, created_by, 100_000, vendor=active, potential_dup=original.id)
    vendor_blocked = _invoice(db, tenant.id, created_by, 50_000, vendor=unverified)
    cfo_approval = _invoice(db, tenant.id, created_by, 300_000, vendor=active)
    mgr_approval = _invoice(db, tenant.id, created_by, 80_000, vendor=active)
    return {
        "flagged": flagged, "vendor_blocked": vendor_blocked,
        "cfo_approval": cfo_approval, "mgr_approval": mgr_approval,
    }


class TestDecisionInbox:
    def test_admin_sees_all_categories_prioritized(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        s = _scenario(db, tenant, admin["id"])

        inbox = DecisionInboxService(db).get_inbox(admin)

        assert inbox["counts"]["duplicate_review"] == 1
        assert inbox["counts"]["vendor_verification"] == 1
        assert inbox["counts"]["approval"] == 2  # admin sees both mgr + cfo approvals
        # Highest priority first.
        assert inbox["items"][0]["category"] == "duplicate_review"
        assert [i["priority"] for i in inbox["items"]] == sorted(i["priority"] for i in inbox["items"])
        # Timeline link points at the invoice.
        dup_item = next(i for i in inbox["items"] if i["category"] == "duplicate_review")
        assert dup_item["timeline_url"].endswith(f"/invoice/{s['flagged'].id}")

    def test_clerk_sees_only_vendor_verification(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        clerk = make_user(UserRole.AP_CLERK)
        _scenario(db, tenant, clerk["id"])

        inbox = DecisionInboxService(db).get_inbox(clerk)

        # Clerk can manage vendors but not approve.
        assert set(inbox["counts"]) == {"vendor_verification"}
        assert inbox["total"] == 1

    def test_cfo_sees_duplicate_and_cfo_approval_only(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        cfo = make_user(UserRole.CFO)
        _scenario(db, tenant, cfo["id"])

        inbox = DecisionInboxService(db).get_inbox(cfo)

        # CFO can approve + review duplicates, but not manage vendors, and only
        # the CFO-routed approval matches their role.
        assert inbox["counts"].get("duplicate_review") == 1
        assert inbox["counts"].get("approval") == 1
        assert "vendor_verification" not in inbox["counts"]
        approval = next(i for i in inbox["items"] if i["category"] == "approval")
        assert approval["required_role"] == "cfo"
        assert approval["amount"] == 300_000

    def test_auditor_inbox_is_empty(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        auditor = make_user(UserRole.AUDITOR)
        _scenario(db, tenant, auditor["id"])

        inbox = DecisionInboxService(db).get_inbox(auditor)
        assert inbox["total"] == 0
        assert inbox["items"] == []


class TestTheEndpointSerialisesWhatTheServiceProduces:
    """The rest of this file calls the service directly, which cannot see the
    response model the endpoint declares.

    That gap was not hypothetical: when the service moved from invoice-shaped
    items to neutral ones, `DecisionInboxItem` still required `invoice_id`, so
    every request with anything in it failed response validation and the page
    returned a 500. The whole suite stayed green, because the one API test
    that hit this path had an empty inbox and an empty list validates against
    any item schema. This one asserts there are items to serialise.
    """

    def test_a_populated_inbox_serialises(self, db, tenant, client, as_user, make_user):
        from decimal import Decimal
        from app.core.enums import PaymentState, RequisitionState
        from app.models.payment import Payment
        from app.models.requisition import PurchaseRequisition

        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, clerk["id"], 300_000, vendor=vendor)
        db.add(PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-API",
            title="Laptops", justification="For the new starters.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("1000"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=clerk["id"],
        ))
        db.add(Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-API",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=clerk["id"],
        ))
        db.flush()

        as_user(cfo)
        response = client.get("/api/v1/inbox")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"], "nothing was serialised, so the schema was not exercised"
        assert body["total"] == len(body["items"])
        assert body["by_work_item_type"]
        for item in body["items"]:
            # Every field the client reads must survive the response model.
            assert item["object_id"] and item["object_type"]
            assert item["detail_url"] and item["timeline_url"]
            assert item["work_item_type"]


class TestEveryWorkItemTypeReachesTheInbox:
    """The Build Book's Definition of Done for the inbox: every work item type
    in the variant, one surface across all departments.

    It read invoices and nothing else. Requisitions, tenders, orders, payment
    runs, bank changes and reconciliation breaks were each built afterwards and
    none of them ever reached the inbox, so a manager with four approvals
    waiting saw an empty screen. These fail if a module stops reporting.
    """

    def test_a_pending_requisition_reaches_an_approver(self, db, tenant, make_user):
        from decimal import Decimal
        from app.core.enums import RequisitionState
        from app.models.requisition import PurchaseRequisition

        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)

        db.add(PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-INBOX",
            title="Laptops", justification="Four engineers start on the 1st.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("1000"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=clerk["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(manager)
        assert "REQ-INBOX" in {i["reference"] for i in inbox["items"]}
        assert inbox["by_work_item_type"].get("approval") == 1

    def test_a_requisition_does_not_reach_the_person_who_raised_it(
        self, db, tenant, make_user
    ):
        """SoD refuses them at approval, so it is not their work item."""
        from decimal import Decimal
        from app.core.enums import RequisitionState
        from app.models.requisition import PurchaseRequisition

        manager = make_user(UserRole.MANAGER)
        db.add(PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-OWN",
            title="Mine", justification="I asked for this one myself.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("500"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=manager["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(manager)
        assert "REQ-OWN" not in {i["reference"] for i in inbox["items"]}

    def test_an_admin_still_sees_the_requisition_they_raised_themselves(
        self, db, tenant, make_user
    ):
        """The inbox must ask the SoD rule, not restate it.

        `violates_self_approval` exempts admins so a one-person tenant still
        works. The inbox compared ids inline instead, so the admin of such a
        tenant could approve their own requisition but was never shown it: the
        item stalled with nothing on screen explaining why.
        """
        from decimal import Decimal
        from app.core.enums import RequisitionState
        from app.models.requisition import PurchaseRequisition

        admin = make_user(UserRole.ADMIN)
        db.add(PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-SOLO",
            title="Only person here", justification="One-person tenant.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("500"),
            current_state=RequisitionState.PENDING_APPROVAL, created_by=admin["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(admin)
        assert "REQ-SOLO" in {i["reference"] for i in inbox["items"]}

    def test_an_admin_never_sees_their_own_bank_change(self, db, tenant, make_user):
        """The mirror of the case above: this rule has no admin exemption, and
        an inbox that applied one blanket rule everywhere would get one of the
        two wrong."""
        from app.core.enums import BankChangeState
        from app.models.vendor_bank_change import VendorBankChange

        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        db.add(VendorBankChange(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            reason="Requested by me.", old_iban="PK00OLD0000000000000001",
            new_iban="PK99NEW0000000000000009",
            current_state=BankChangeState.PENDING_APPROVAL, requested_by=admin["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(admin)
        assert not [
            i for i in inbox["items"] if i["object_type"] == "vendor_bank_change"
        ]

    def test_a_closed_tender_awaiting_award_reaches_the_awarder(
        self, db, tenant, make_user
    ):
        """Nothing else chases this: quoting has ended and the vendors wait."""
        from decimal import Decimal
        from app.core.enums import RequisitionState, RFQState
        from app.models.requisition import PurchaseRequisition
        from app.models.rfq import RFQ

        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        requisition = PurchaseRequisition(
            id=uuid.uuid4(), tenant_id=tenant.id, requisition_number="REQ-T",
            title="Laptops", justification="Needed for the new starters.",
            requested_date=date(2026, 8, 1), estimated_amount=Decimal("10000"),
            current_state=RequisitionState.APPROVED, created_by=clerk["id"],
        )
        db.add(requisition)
        db.flush()
        db.add(RFQ(
            id=uuid.uuid4(), tenant_id=tenant.id, rfq_number="RFQ-INBOX",
            title="Laptops", requisition_id=requisition.id,
            current_state=RFQState.CLOSED, created_by=clerk["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(manager)
        assert "RFQ-INBOX" in {i["reference"] for i in inbox["items"]}

    def test_a_payment_awaiting_release_reaches_the_releaser(
        self, db, tenant, make_user
    ):
        from decimal import Decimal
        from app.core.enums import PaymentState
        from app.models.payment import Payment

        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        db.add(Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-INBOX",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=clerk["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(cfo)
        assert "PAY-INBOX" in {i["reference"] for i in inbox["items"]}

    def test_a_payment_does_not_reach_the_person_who_prepared_it(
        self, db, tenant, make_user
    ):
        """Maker-checker refuses it at release, so listing it would be a lie."""
        from decimal import Decimal
        from app.core.enums import PaymentState
        from app.models.payment import Payment

        cfo = make_user(UserRole.CFO)
        db.add(Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-OWN",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=cfo["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(cfo)
        assert "PAY-OWN" not in {i["reference"] for i in inbox["items"]}

    def test_a_pending_bank_change_reaches_its_approver(self, db, tenant, make_user):
        from app.core.enums import BankChangeState
        from app.models.vendor_bank_change import VendorBankChange

        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)

        db.add(VendorBankChange(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            reason="Vendor emailed new details on headed paper.",
            old_iban="PK00OLD0000000000000001",
            new_iban="PK99NEW0000000000000009",
            current_state=BankChangeState.PENDING_APPROVAL,
            requested_by=clerk["id"],
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(manager)
        item = next(
            (i for i in inbox["items"] if i["object_type"] == "vendor_bank_change"),
            None,
        )
        assert item is not None
        assert item["work_item_type"] == "admin"
        # Ranked above ordinary approvals: payments to the vendor are held while
        # it is open, and a cooling period only helps if somebody looks.
        assert item["priority"] < 2

    def test_an_unexplained_debit_outranks_everything_else(
        self, db, tenant, make_user
    ):
        """Every other item is money about to move. This one already did, with
        nothing in the system accounting for it."""
        from decimal import Decimal
        from app.models.bank_statement import BankStatement, BankStatementLine

        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, status=VendorStatus.PENDING_VERIFICATION)
        _invoice(db, tenant.id, clerk["id"], 1000, vendor=vendor)

        statement = BankStatement(
            id=uuid.uuid4(), tenant_id=tenant.id, statement_reference="STMT-1",
            source_format="csv", file_hash="x" * 64, imported_by=clerk["id"],
        )
        db.add(statement)
        db.flush()
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id, bank_statement_id=statement.id,
            line_number=1, amount=Decimal("91000"), is_debit=True,
            description="UNKNOWN TRANSFER", bank_reference="XX-9",
        ))
        db.flush()

        inbox = DecisionInboxService(db).get_inbox(clerk)
        assert inbox["items"][0]["category"] == "unexplained_debit"
        assert inbox["by_work_item_type"].get("reconciliation") == 1

    def test_the_auditor_still_has_nothing_to_do(self, db, tenant, make_user):
        """A read-only oversight role holds no work items however many modules
        report — the inbox is what you must act on, not what exists."""
        from decimal import Decimal
        from app.core.enums import PaymentState
        from app.models.payment import Payment

        clerk = make_user(UserRole.AP_CLERK)
        auditor = make_user(UserRole.AUDITOR)
        db.add(Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-AUD",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=clerk["id"],
        ))
        db.flush()

        assert DecisionInboxService(db).get_inbox(auditor)["total"] == 0
