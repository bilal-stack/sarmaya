"""Payments: the step every upstream control exists to protect.

Until now an approved invoice was marked paid by one person with a single
state flip. These tests cover the three controls that replaced it — maker-
checker, re-verification at release, and the refusal to pay an invoice twice —
plus the bank file, which is the only artefact that leaves the system.

The system never moves money. It produces an instruction a treasury user
uploads to their own bank, so "released" means authorised, not transferred.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import UserRole, InvoiceState, PaymentState, VendorStatus
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.payment import PaymentCreate
from app.services.bank_export import BankExportService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.payment_service import PaymentService

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Payable Vendor",
        status=VendorStatus.ACTIVE, bank_account_name="Payable Vendor Ltd",
        bank_account_number="0123456789", bank_name="Demo Bank",
        iban="PK36SCBL0000001123456702", swift_code="SCBLPKKX",
    )
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant,
        "vendor": vendor,
        "clerk": make_user(UserRole.AP_CLERK),
        "cfo": make_user(UserRole.CFO),
        "admin": make_user(UserRole.ADMIN),
    }


def _approved_invoice(db, setup, amount="1000"):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        vendor_id=setup["vendor"].id, vendor_name=setup["vendor"].legal_name,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
        current_state=InvoiceState.APPROVED,
        created_by=setup["clerk"]["id"],
    )
    db.add(invoice)
    db.flush()
    return invoice


def _prepared(db, setup, invoices, actor=None):
    return PaymentService(db).prepare_payment(
        [i.id for i in invoices],
        PaymentCreate(invoice_ids=[i.id for i in invoices]),
        actor or setup["clerk"],
    )


def _actions(db, object_id):
    return [
        a.action for a in
        db.query(AuditLog).filter(AuditLog.object_id == object_id).all()
    ]


class TestPreparingARun:

    def test_a_clerk_prepares_one(self, db, setup):
        invoice = _approved_invoice(db, setup)
        payment = _prepared(db, setup, [invoice])

        assert payment.payment_number.startswith("PAY-")
        assert payment.current_state == PaymentState.DRAFT
        assert Decimal(payment.total_amount) == Decimal("1000")
        assert "prepared" in _actions(db, payment.id)

    def test_the_total_is_the_sum_of_the_invoices(self, db, setup):
        """Amounts come from the invoices, so a run cannot pay a different
        figure from the one that was approved."""
        payment = _prepared(db, setup, [
            _approved_invoice(db, setup, "1000"),
            _approved_invoice(db, setup, "250"),
        ])
        assert Decimal(payment.total_amount) == Decimal("1250")

    def test_bank_details_are_copied_onto_the_line(self, db, setup):
        """They can change after release; the instruction must stay
        reconstructable from what was true when it was built."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        line = payment.lines[0]
        assert line.iban == "PK36SCBL0000001123456702"
        assert line.bank_account_number == "0123456789"

    def test_an_unapproved_invoice_is_refused(self, db, setup):
        invoice = _approved_invoice(db, setup)
        invoice.current_state = InvoiceState.PENDING_APPROVAL
        db.flush()

        with pytest.raises(ValueError, match="only approved invoices"):
            _prepared(db, setup, [invoice])

    def test_an_already_paid_invoice_is_refused(self, db, setup):
        invoice = _approved_invoice(db, setup)
        invoice.current_state = InvoiceState.PAID
        db.flush()

        with pytest.raises(ValueError, match="already been paid"):
            _prepared(db, setup, [invoice])

    def test_an_inactive_vendor_is_refused(self, db, setup):
        invoice = _approved_invoice(db, setup)
        setup["vendor"].status = VendorStatus.BLOCKED
        db.flush()

        with pytest.raises(ValueError, match="not active"):
            _prepared(db, setup, [invoice])

    def test_a_releaser_cannot_prepare(self, db, setup):
        """Preparing and releasing are separate authorities."""
        invoice = _approved_invoice(db, setup)
        with pytest.raises(PermissionError, match="prepare payments"):
            _prepared(db, setup, [invoice], actor=setup["cfo"])


class TestMakerChecker:

    def test_the_preparer_cannot_release_their_own_run(self, db, setup):
        """The control this whole module exists for."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        PaymentService(db).submit_for_release(payment.id, setup["clerk"])

        # Give the preparer release rights, so it is the SoD rule that refuses
        # them rather than a missing permission.
        preparer_with_rights = {**setup["clerk"], "role": UserRole.CFO.value}
        with pytest.raises(PermissionError, match="Segregation of duties"):
            PaymentService(db).release_payment(payment.id, preparer_with_rights)

    def test_not_even_an_admin_may_release_their_own(self, db, setup):
        """The invoice rules exempt admins so a one-person tenant still works.
        This one does not: it guards the moment money leaves."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)],
                            actor=setup["admin"])
        PaymentService(db).submit_for_release(payment.id, setup["admin"])

        with pytest.raises(PermissionError, match="Segregation of duties"):
            PaymentService(db).release_payment(payment.id, setup["admin"])

    def test_a_refused_release_is_audited(self, db, setup):
        payment = _prepared(db, setup, [_approved_invoice(db, setup)],
                            actor=setup["admin"])
        PaymentService(db).submit_for_release(payment.id, setup["admin"])
        with pytest.raises(PermissionError):
            PaymentService(db).release_payment(payment.id, setup["admin"])

        assert "release_blocked" in _actions(db, payment.id)

    def test_someone_else_releases_it(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])

        released = service.release_payment(payment.id, setup["cfo"])

        assert released.current_state == PaymentState.RELEASED
        assert released.released_by is not None
        assert released.released_by != released.prepared_by

    def test_releasing_settles_the_invoices(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])
        service.release_payment(payment.id, setup["cfo"])

        db.refresh(invoice)
        state = str(getattr(invoice.current_state, "value", invoice.current_state))
        assert state == InvoiceState.PAID.value
        # The run keeps its own chain, so the invoice is told separately.
        assert "marked_paid" in _actions(db, invoice.id)


class TestNoDoublePayment:

    def test_an_invoice_on_a_pending_run_cannot_join_another(self, db, setup):
        """The check that actually stops money going out twice."""
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        first = _prepared(db, setup, [invoice])
        service.submit_for_release(first.id, setup["clerk"])

        with pytest.raises(ValueError, match="already on another payment run"):
            _prepared(db, setup, [invoice])

    def test_an_invoice_on_a_released_run_cannot_join_another(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        first = _prepared(db, setup, [invoice])
        service.submit_for_release(first.id, setup["clerk"])
        service.release_payment(first.id, setup["cfo"])

        with pytest.raises(ValueError, match="already been paid"):
            _prepared(db, setup, [invoice])

    def test_payable_list_excludes_claimed_invoices(self, db, setup):
        """Offered to the preparer so the rule is visible up front rather than
        a refusal discovered at release."""
        claimed = _approved_invoice(db, setup)
        free = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [claimed])
        service.submit_for_release(payment.id, setup["clerk"])

        payable = {i.id for i in service.payable_invoices(setup["clerk"])}
        assert free.id in payable
        assert claimed.id not in payable


class TestReleaseRechecksTheWorld:
    """A run can wait days. Checking only at preparation would authorise a
    picture of the world that has since changed, and release cannot be undone."""

    def test_an_invoice_paid_meanwhile_blocks_the_release(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])

        # Settled by some other route while the run sat waiting.
        invoice.current_state = InvoiceState.PAID
        db.flush()

        with pytest.raises(ValueError, match="already been paid"):
            service.release_payment(payment.id, setup["cfo"])

    def test_a_vendor_blocked_meanwhile_blocks_the_release(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])

        setup["vendor"].status = VendorStatus.BLOCKED
        db.flush()

        with pytest.raises(ValueError, match="not active"):
            service.release_payment(payment.id, setup["cfo"])


class TestTheBankFile:

    def _released(self, db, setup, amount="1000"):
        invoice = _approved_invoice(db, setup, amount)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])
        return service.release_payment(payment.id, setup["cfo"]), invoice

    def test_a_released_run_exports(self, db, setup):
        payment, invoice = self._released(db, setup)
        filename, content, digest = BankExportService(db).export_csv(
            payment.id, setup["cfo"]
        )

        assert filename == f"{payment.payment_number}.csv"
        assert "PK36SCBL0000001123456702" in content
        # The vendor cannot reconcile a receipt without knowing what it settles.
        assert invoice.invoice_number in content
        assert len(digest) == 64

    def test_an_unreleased_run_cannot_be_exported(self, db, setup):
        """An unauthorised instruction must never reach a bank."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        with pytest.raises(ValueError, match="Only a released run"):
            BankExportService(db).export_csv(payment.id, setup["cfo"])

    def test_the_hash_is_recorded_on_first_export(self, db, setup):
        payment, _ = self._released(db, setup)
        _, _, digest = BankExportService(db).export_csv(payment.id, setup["cfo"])

        db.refresh(payment)
        assert payment.bank_file_hash == digest
        assert payment.bank_file_generated_at is not None

    def test_re_exporting_is_stable_and_does_not_overwrite_the_original(self, db, setup):
        """Files get lost, so re-export is allowed — but the original hash
        stands, so a later export that differs is detectable."""
        payment, _ = self._released(db, setup)
        service = BankExportService(db)
        _, first, first_hash = service.export_csv(payment.id, setup["cfo"])
        _, second, second_hash = service.export_csv(payment.id, setup["cfo"])

        assert first == second
        assert first_hash == second_hash
        db.refresh(payment)
        assert payment.bank_file_hash == first_hash

    def test_the_export_is_audited(self, db, setup):
        payment, _ = self._released(db, setup)
        BankExportService(db).export_csv(payment.id, setup["cfo"])
        assert "bank_file_generated" in _actions(db, payment.id)


class TestItJoinsTheGovernanceLayer:

    def test_the_run_has_its_own_chain(self, db, setup):
        """A run may settle invoices from several chains, so it cannot inherit
        one; it carries its own and writes onto each invoice's."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        assert payment.correlation_id is not None

    def test_the_timeline_is_readable_by_a_payment_viewer(self, db, setup):
        from app.services.audit_service import AuditService

        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        timeline = AuditService(db).get_timeline("payment", payment.id, setup["cfo"])
        assert timeline["total_events"] >= 1

    def test_the_chain_verifies(self, db, setup):
        from app.services.audit_integrity import verify_object_chain

        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])
        service.release_payment(payment.id, setup["cfo"])

        assert verify_object_chain(db, "payment", payment.id)["verified"] is True


class TestAnInstructionMustBePayable:
    """A bank file is only an instruction if it says where the money goes and
    in what currency. Both were missing from the first real file generated —
    the columns were simply blank — which a bank rejects at best and silently
    drops at worst."""

    def test_a_vendor_with_no_bank_account_blocks_the_release(self, db, setup):
        setup["vendor"].iban = None
        setup["vendor"].bank_account_number = None
        db.flush()

        service = PaymentService(db)
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        service.submit_for_release(payment.id, setup["clerk"])

        with pytest.raises(ValueError, match="no bank account"):
            service.release_payment(payment.id, setup["cfo"])

    def test_the_run_carries_a_currency(self, db, setup):
        """Taken from the invoices when the caller does not say."""
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        assert payment.currency is not None

    def test_the_file_states_the_currency_on_every_line(self, db, setup):
        invoice = _approved_invoice(db, setup)
        service = PaymentService(db)
        payment = _prepared(db, setup, [invoice])
        service.submit_for_release(payment.id, setup["clerk"])
        service.release_payment(payment.id, setup["cfo"])

        _, content, _ = BankExportService(db).export_csv(payment.id, setup["cfo"])
        rows = [r for r in content.strip().split("\n")[1:] if r]
        assert rows
        for row in rows:
            cells = row.split(",")
            assert cells[-2].strip(), f"no currency on the line: {row}"
            # IBAN or account number — something to pay into.
            assert cells[5].strip() or cells[4].strip(), f"nowhere to pay: {row}"


class TestWhoActedIsLegible:
    """Maker-checker is the point of a payment run, so the screen has to name
    the two people. Resolved server-side rather than from the user directory:
    reading that needs users.view, which the clerks who prepare runs
    deliberately do not have — so the run would show them two raw UUIDs."""

    def test_the_preparer_is_named(self, db, setup):
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        assert payment.prepared_by_name
        # A name or email, never the raw id the screen would otherwise show.
        assert payment.prepared_by_name != str(payment.prepared_by)

    def test_the_releaser_is_named_once_released(self, db, setup):
        service = PaymentService(db)
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        service.submit_for_release(payment.id, setup["clerk"])
        released = service.release_payment(payment.id, setup["cfo"])

        assert released.released_by_name
        assert released.released_by_name != released.prepared_by_name

    def test_a_preparer_sees_both_names_without_directory_access(self, db, setup):
        """The clerk who prepared it has no users.view, and must still be able
        to read who released their run."""
        from app.core.roles import has_permission, PERM_VIEW_USERS

        assert has_permission(setup["clerk"]["role"], PERM_VIEW_USERS) is False

        service = PaymentService(db)
        payment = _prepared(db, setup, [_approved_invoice(db, setup)])
        service.submit_for_release(payment.id, setup["clerk"])
        service.release_payment(payment.id, setup["cfo"])

        seen = service.get_payment(payment.id, setup["clerk"])
        assert seen.prepared_by_name and seen.released_by_name
