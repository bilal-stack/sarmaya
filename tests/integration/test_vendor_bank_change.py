"""Changing a vendor's bank details is a controlled act.

Build Book A1 control: vendor bank change verification with dual approval and
cooling period policy.

The scenario these tests are about is the most common invoice fraud there is,
and the reason it works is that nothing downstream is wrong. The invoice is
real, the approval is real, the release is real — only the destination account
changed, and the release screen shows a vendor name and an amount, not the fact
that the account changed yesterday. So the control has to sit at the change.

Before this module, `PATCH /vendors/{id}` wrote bank fields directly behind
`vendors.manage`, which the AP clerk holds, and the audit entry for a vendor
update recorded only the legal name. The change was uncontrolled *and*
invisible.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import (
    UserRole, VendorStatus, InvoiceState, BankChangeState,
)
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.payment import PaymentCreate
from app.schemas.vendor import VendorUpdate
from app.schemas.vendor_bank_change import BankChangeRequest
from app.services.config_provisioning import ConfigProvisioningService
from app.services.payment_service import PaymentService
from app.services.vendor_bank_service import VendorBankService
from app.services.vendor_service import VendorService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive


def _naive_now():
    """The DB columns are tz-naive; comparing an aware value raises."""
    return make_naive(to_utc(utc_now()))

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Genuine Supplies",
        status=VendorStatus.ACTIVE,
        bank_account_name="Genuine Supplies Ltd",
        bank_account_number="1111111111",
        bank_name="Real Bank",
        iban="PK00REAL0000000000000001",
    )
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant,
        "vendor": vendor,
        # Maintains vendors and prepares payments — and so is exactly the
        # person the control is watching.
        "clerk": make_user(UserRole.AP_CLERK),
        "manager": make_user(UserRole.MANAGER),
        "cfo": make_user(UserRole.CFO),
        "admin": make_user(UserRole.ADMIN),
    }


ATTACKER_IBAN = "PK99FAKE0000000000000009"


def _request_change(db, setup, actor=None, iban=ATTACKER_IBAN):
    return VendorBankService(db).request_change(
        setup["vendor"].id,
        BankChangeRequest(
            reason="Vendor emailed new details on headed paper.",
            iban=iban,
            bank_account_number="9999999999",
        ),
        actor or setup["clerk"],
    )


def _approved_invoice(db, setup, amount="50000"):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        vendor_id=setup["vendor"].id, vendor_name=setup["vendor"].legal_name,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
        current_state=InvoiceState.APPROVED, created_by=setup["clerk"]["id"],
    )
    db.add(invoice)
    db.flush()
    return invoice


def _actions(db, object_id):
    return [
        a.action for a in
        db.query(AuditLog).filter(AuditLog.object_id == object_id).all()
    ]


class TestBankDetailsCannotBeEditedDirectly:
    """The hole this closes. A clerk could rewrite the account in one call."""

    def test_the_update_endpoint_refuses_bank_fields(self, db, setup):
        with pytest.raises(ValueError, match="cannot be edited directly"):
            VendorService(db).update_vendor(
                setup["vendor"].id,
                VendorUpdate(iban=ATTACKER_IBAN),
                setup["clerk"],
            )

        db.refresh(setup["vendor"])
        assert setup["vendor"].iban == "PK00REAL0000000000000001"

    def test_other_fields_still_update(self, db, setup):
        """The refusal is about bank details, not about editing vendors."""
        updated = VendorService(db).update_vendor(
            setup["vendor"].id,
            VendorUpdate(phone="+92-300-0000000"),
            setup["clerk"],
        )
        assert updated.phone == "+92-300-0000000"

    def test_a_vendor_edit_records_every_changed_field(self, db, setup):
        """It recorded only the legal name, so any other change was opaque."""
        VendorService(db).update_vendor(
            setup["vendor"].id,
            VendorUpdate(email="new@genuine-supplies.com"),
            setup["clerk"],
        )
        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == setup["vendor"].id,
                    AuditLog.action == "updated")
            .first()
        )
        assert entry.after_value.get("email") == "new@genuine-supplies.com"


class TestRequestingAChange:

    def test_it_records_both_sides(self, db, setup):
        """The substitution is the thing a reviewer looks at, and the vendor
        row will not hold the old account once this is applied."""
        change = _request_change(db, setup)

        assert change.current_state == BankChangeState.PENDING_APPROVAL
        assert change.new_iban == ATTACKER_IBAN
        assert change.old_iban == "PK00REAL0000000000000001"
        assert "bank_change_requested" in _actions(db, setup["vendor"].id)

    def test_a_reason_is_required(self, db, setup):
        """It is what the approver is judging."""
        with pytest.raises(ValueError, match="how this change was received"):
            BankChangeRequest(reason="ok", iban=ATTACKER_IBAN)

    def test_at_least_one_detail_is_required(self, db, setup):
        with pytest.raises(ValueError, match="at least one new bank detail"):
            BankChangeRequest(reason="Vendor sent a letter we verified.")

    def test_two_open_changes_are_refused(self, db, setup):
        """Two open requests make it unclear which account was agreed to."""
        _request_change(db, setup)
        with pytest.raises(ValueError, match="already has a bank change"):
            _request_change(db, setup, iban="PK11OTHER000000000000001")

    def test_the_vendor_is_not_changed_yet(self, db, setup):
        _request_change(db, setup)
        db.refresh(setup["vendor"])
        assert setup["vendor"].iban == "PK00REAL0000000000000001"


class TestApprovalIsSomeoneElsesJob:

    def test_the_requester_cannot_approve_their_own(self, db, setup):
        """The exact step the fraud needs."""
        change = _request_change(db, setup)

        # Give the requester the permission, so it is the SoD rule refusing
        # them rather than a missing right.
        requester_with_rights = {**setup["clerk"], "role": UserRole.MANAGER.value}
        with pytest.raises(PermissionError, match="Segregation of duties"):
            VendorBankService(db).approve_change(change.id, requester_with_rights)

    def test_not_even_an_admin_may_approve_their_own(self, db, setup):
        """The invoice rules exempt admins so a one-person tenant works. This
        one does not — the carve-out that keeps a one-person tenant working
        would keep a one-person fraud working."""
        change = _request_change(db, setup, actor=setup["admin"])

        with pytest.raises(PermissionError, match="Segregation of duties"):
            VendorBankService(db).approve_change(change.id, setup["admin"])

    def test_a_refused_approval_is_audited(self, db, setup):
        """An attempt to self-approve a bank change is the single most
        interesting line in this trail."""
        change = _request_change(db, setup, actor=setup["admin"])
        with pytest.raises(PermissionError):
            VendorBankService(db).approve_change(change.id, setup["admin"])

        assert "bank_change_approval_blocked" in _actions(db, setup["vendor"].id)

    def test_the_clerk_who_maintains_vendors_cannot_approve(self, db, setup):
        """Whoever maintains vendor records is exactly who would make this
        change, so the permission is held elsewhere."""
        from app.core.roles import has_permission, PERM_APPROVE_BANK_CHANGE

        assert has_permission("ap_clerk", PERM_APPROVE_BANK_CHANGE) is False

    def test_someone_else_approves_it(self, db, setup):
        change = _request_change(db, setup)
        approved = VendorBankService(db).approve_change(change.id, setup["manager"])

        assert approved.current_state == BankChangeState.APPROVED
        assert approved.effective_at is not None
        assert "bank_change_approved" in _actions(db, setup["vendor"].id)


class TestTheCoolingPeriod:
    """Approval starts a clock rather than taking effect. The wait is the
    window in which the real vendor can say they never asked for this."""

    def test_the_vendor_is_still_unchanged_after_approval(self, db, setup):
        change = _request_change(db, setup)
        VendorBankService(db).approve_change(change.id, setup["manager"])

        db.refresh(setup["vendor"])
        assert setup["vendor"].iban == "PK00REAL0000000000000001"

    def test_applying_early_is_refused(self, db, setup):
        change = _request_change(db, setup)
        VendorBankService(db).approve_change(change.id, setup["manager"])

        with pytest.raises(ValueError, match="cooling period"):
            VendorBankService(db).apply_change(change.id, setup["clerk"])

    def test_applying_after_the_wait_updates_the_vendor(self, db, setup):
        change = _request_change(db, setup)
        VendorBankService(db).approve_change(change.id, setup["manager"])

        # Wind the clock back rather than sleep for a day.
        change.effective_at = _naive_now() - timedelta(minutes=1)
        db.flush()

        vendor = VendorBankService(db).apply_change(change.id, setup["clerk"])
        assert vendor.iban == ATTACKER_IBAN
        assert "bank_change_applied" in _actions(db, setup["vendor"].id)

    def test_the_applied_entry_carries_both_approvers(self, db, setup):
        """Who asked and who agreed, on the event that actually changed it."""
        change = _request_change(db, setup)
        VendorBankService(db).approve_change(change.id, setup["manager"])
        change.effective_at = _naive_now() - timedelta(minutes=1)
        db.flush()
        VendorBankService(db).apply_change(change.id, setup["clerk"])

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == setup["vendor"].id,
                    AuditLog.action == "bank_change_applied")
            .first()
        )
        assert entry.before_value["iban"] == "PK00REAL0000000000000001"
        assert entry.after_value["iban"] == ATTACKER_IBAN
        assert entry.after_value["requested_by"] != entry.after_value["approved_by"]


class TestPaymentsAreHeldWhileAChangeIsOpen:
    """The part that actually stops the money.

    Payments are held to *either* account during a change: if it is fraudulent
    the old account may already be compromised, and if it is genuine the vendor
    is expecting the new one. Holding is the only answer right in both cases.
    """

    def test_preparation_is_refused(self, db, setup):
        invoice = _approved_invoice(db, setup)
        _request_change(db, setup)

        with pytest.raises(ValueError, match="bank change awaiting resolution"):
            PaymentService(db).prepare_payment(
                [invoice.id], PaymentCreate(invoice_ids=[invoice.id]),
                setup["clerk"],
            )

    def test_a_run_prepared_earlier_cannot_be_released(self, db, setup):
        """The dangerous case: a run sits for days, the details change while
        it waits, and the line already holds the account copied at preparation."""
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = service.prepare_payment(
            [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
        )
        service.submit_for_release(payment.id, setup["clerk"])

        _request_change(db, setup)

        with pytest.raises(ValueError, match="bank change"):
            service.release_payment(payment.id, setup["cfo"])

    def test_payment_resumes_once_the_change_is_rejected(self, db, setup):
        invoice = _approved_invoice(db, setup)
        change = _request_change(db, setup)
        VendorBankService(db).reject_change(
            change.id, "Rang the number we already had; they never asked.",
            setup["manager"],
        )

        payment = PaymentService(db).prepare_payment(
            [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
        )
        assert payment.lines[0].iban == "PK00REAL0000000000000001"

    def test_payment_resumes_once_the_change_is_applied(self, db, setup):
        invoice = _approved_invoice(db, setup)
        change = _request_change(db, setup)
        VendorBankService(db).approve_change(change.id, setup["manager"])
        change.effective_at = _naive_now() - timedelta(minutes=1)
        db.flush()
        VendorBankService(db).apply_change(change.id, setup["clerk"])

        payment = PaymentService(db).prepare_payment(
            [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
        )
        assert payment.lines[0].iban == ATTACKER_IBAN


class TestTheWholeFraudAttempt:

    def test_a_clerk_alone_cannot_redirect_a_payment(self, db, setup):
        """The scenario, end to end, by one person with a clerk account.

        They hold vendors.manage and payments.prepare, which before this was
        everything needed: rewrite the account, prepare a run against a real
        approved invoice, and let a releaser who sees a vendor name and an
        amount authorise it.
        """
        invoice = _approved_invoice(db, setup)

        # 1. Direct edit — refused.
        with pytest.raises(ValueError):
            VendorService(db).update_vendor(
                setup["vendor"].id, VendorUpdate(iban=ATTACKER_IBAN), setup["clerk"]
            )

        # 2. So they raise it properly, and try to approve it themselves.
        change = _request_change(db, setup)
        with pytest.raises(PermissionError):
            VendorBankService(db).approve_change(
                change.id, {**setup["clerk"], "role": UserRole.MANAGER.value}
            )

        # The account is untouched, and every attempt is on the vendor's own
        # timeline. Checked here rather than after the payment attempt below:
        # a refused preparation rolls its session back deliberately, which in a
        # test also discards the fixtures this would read.
        db.refresh(setup["vendor"])
        assert setup["vendor"].iban == "PK00REAL0000000000000001"

        actions = _actions(db, setup["vendor"].id)
        assert "bank_change_requested" in actions
        assert "bank_change_approval_blocked" in actions

        # 3. And meanwhile no payment can go out at all.
        with pytest.raises(ValueError, match="bank change awaiting resolution"):
            PaymentService(db).prepare_payment(
                [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"]
            )
