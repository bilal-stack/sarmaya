"""A vendor's account number is a credential, not a field.

DR-032 made *changing* bank details a dual-approved act with a cooling period,
while *reading* them stayed open to every role holding `vendors.view` — five of
them, including the read-only auditor, the account most easily obtained and the
one whose compromise looks harmless. The full IBAN is exactly the reconnaissance
a payment redirection needs, so the two postures did not match.

The same identifiers appear on three surfaces: the vendor record, the bank
change (old and new together, which is worse), and the destination copied onto
each payment line. Masking one and not the others just moves the leak, so the
sweep at the bottom fails on any response that carries a full value.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import (
    BankChangeState, PaymentState, UserRole, VendorStatus,
)
from app.core.roles import (
    ROLE_PERMISSIONS, PERM_VIEW_BANK_DETAILS, PERM_VIEW_VENDORS,
)
from app.models.payment import Payment, PaymentLine
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.vendor_bank_change import VendorBankChange
from app.utils.masking import mask_account

pytestmark = pytest.mark.integration

FULL_IBAN = "PK36SCBL0000001123456702"
FULL_ACCOUNT = "0000001123456702"
NEW_IBAN = "PK24ALFH0000009988776655"


def _vendor(db, tenant_id):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id, legal_name="Orion Supplies Ltd",
        status=VendorStatus.ACTIVE, iban=FULL_IBAN,
        bank_account_number=FULL_ACCOUNT, bank_name="Standard Chartered",
    )
    db.add(vendor)
    db.flush()
    return vendor


class TestTheMask:
    def test_it_keeps_the_last_four_so_an_account_stays_identifiable(self):
        """A reviewer matching a payment against a remittance advice needs to
        tell two accounts apart without being handed either."""
        assert mask_account(FULL_IBAN) == "••••6702"
        assert mask_account(FULL_IBAN)[-4:] == FULL_IBAN[-4:]

    def test_a_short_value_is_hidden_entirely(self):
        """Showing the last four of a six-character value gives away most of it."""
        assert set(mask_account("123456")) == {"•"}

    def test_nothing_is_invented_for_a_vendor_with_no_details(self):
        assert mask_account(None) is None
        assert mask_account("") == ""


class TestWhoHoldsThePermission:
    def test_the_auditor_does_not(self):
        """The whole point. Read-only oversight needs to see *that* details
        changed, which the audit trail gives it — not the live credential."""
        assert PERM_VIEW_VENDORS in ROLE_PERMISSIONS[UserRole.AUDITOR.value]
        assert PERM_VIEW_BANK_DETAILS not in ROLE_PERMISSIONS[UserRole.AUDITOR.value]

    @pytest.mark.parametrize("role", [
        UserRole.ADMIN, UserRole.AP_CLERK, UserRole.MANAGER, UserRole.CFO,
    ])
    def test_the_roles_that_act_on_payment_details_do(self, role):
        """The clerk maintains vendor records and prepares runs; the manager
        and CFO approve bank changes and must compare old against new."""
        assert PERM_VIEW_BANK_DETAILS in ROLE_PERMISSIONS[role.value]


class TestTheVendorRecord:
    def test_the_auditor_sees_only_the_last_four(
        self, db, tenant, client, as_user, make_user
    ):
        vendor = _vendor(db, tenant.id)
        as_user(make_user(UserRole.AUDITOR))

        body = client.get(f"/api/v1/vendors/{vendor.id}").json()

        assert body["iban"] == "••••6702"
        assert body["bank_account_number"] == "••••6702"
        assert body["bank_details_visible"] is False

    def test_the_clerk_who_maintains_it_sees_it_in_full(
        self, db, tenant, client, as_user, make_user
    ):
        vendor = _vendor(db, tenant.id)
        as_user(make_user(UserRole.AP_CLERK))

        body = client.get(f"/api/v1/vendors/{vendor.id}").json()

        assert body["iban"] == FULL_IBAN
        assert body["bank_details_visible"] is True


class TestTheBankChange:
    def test_the_auditor_sees_neither_side_of_it(
        self, db, tenant, client, as_user, make_user
    ):
        """Worse than the vendor record: a change carries the old account and
        the new one together, which is the whole substitution."""
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)
        db.add(VendorBankChange(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            reason="Vendor emailed new details.", old_iban=FULL_IBAN,
            new_iban=NEW_IBAN, current_state=BankChangeState.PENDING_APPROVAL,
            requested_by=clerk["id"],
        ))
        db.flush()
        as_user(make_user(UserRole.AUDITOR))

        body = client.get("/api/v1/vendors/bank-changes").json()

        assert body, "the auditor can list changes, so this must be masked"
        assert body[0]["old_iban"] == "••••6702"
        assert body[0]["new_iban"] == "••••6655"
        assert body[0]["bank_details_visible"] is False

    def test_the_approver_sees_both_sides_in_full(
        self, db, tenant, client, as_user, make_user
    ):
        """Masking the approver would break the control it protects: comparing
        the new account against the old is the review."""
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)
        db.add(VendorBankChange(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            reason="Vendor emailed new details.", old_iban=FULL_IBAN,
            new_iban=NEW_IBAN, current_state=BankChangeState.PENDING_APPROVAL,
            requested_by=clerk["id"],
        ))
        db.flush()
        as_user(make_user(UserRole.MANAGER))

        body = client.get("/api/v1/vendors/bank-changes").json()

        assert body[0]["old_iban"] == FULL_IBAN
        assert body[0]["new_iban"] == NEW_IBAN


class TestThePaymentLine:
    def test_the_auditor_sees_a_masked_destination(
        self, db, tenant, client, as_user, make_user
    ):
        """The auditor holds payments.view, and the destination is copied onto
        the line at preparation — the same credential by another route."""
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number="INV-MASK",
            vendor_name=vendor.legal_name, vendor_id=vendor.id,
            invoice_date=date(2026, 1, 1), total_amount=Decimal("5000"),
            current_state="approved", created_by=clerk["id"],
        )
        payment = Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-MASK",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=clerk["id"],
        )
        db.add_all([invoice, payment])
        db.flush()
        db.add(PaymentLine(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_id=payment.id,
            line_number=1, invoice_id=invoice.id, amount=Decimal("5000"),
            vendor_id=vendor.id, vendor_name=vendor.legal_name,
            iban=FULL_IBAN, bank_account_number=FULL_ACCOUNT,
        ))
        db.flush()
        as_user(make_user(UserRole.AUDITOR))

        body = client.get(f"/api/v1/payments/{payment.id}").json()

        assert body["lines"], "no line to check"
        assert body["lines"][0]["iban"] == "••••6702"
        assert body["lines"][0]["bank_account_number"] == "••••6702"
        assert body["bank_details_visible"] is False


class TestNoSurfaceLeaksIt:
    """The guard against the next endpoint.

    Three separate surfaces carried this value and each was added in good faith
    by someone thinking about their own module. A per-endpoint assertion would
    not have caught the third, so this walks everything a read-only role can
    reach and fails if the full value appears anywhere in the response.
    """

    def test_no_readable_endpoint_returns_a_full_account_number(
        self, db, tenant, client, as_user, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number="INV-SWEEP",
            vendor_name=vendor.legal_name, vendor_id=vendor.id,
            invoice_date=date(2026, 1, 1), total_amount=Decimal("5000"),
            current_state="approved", created_by=clerk["id"],
        )
        payment = Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-SWEEP",
            payment_date=date(2026, 8, 3), total_amount=Decimal("5000"),
            current_state=PaymentState.PENDING_RELEASE, prepared_by=clerk["id"],
        )
        db.add_all([invoice, payment])
        db.flush()
        db.add(PaymentLine(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_id=payment.id,
            line_number=1, invoice_id=invoice.id, amount=Decimal("5000"),
            vendor_id=vendor.id, vendor_name=vendor.legal_name,
            iban=FULL_IBAN, bank_account_number=FULL_ACCOUNT,
        ))
        db.add(VendorBankChange(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            reason="Vendor emailed new details.", old_iban=FULL_IBAN,
            new_iban=NEW_IBAN, current_state=BankChangeState.PENDING_APPROVAL,
            requested_by=clerk["id"],
        ))
        db.flush()

        as_user(make_user(UserRole.AUDITOR))
        paths = [
            "/api/v1/vendors/",
            f"/api/v1/vendors/{vendor.id}",
            "/api/v1/vendors/bank-changes",
            "/api/v1/payments",
            f"/api/v1/payments/{payment.id}",
            "/api/v1/inbox",
            f"/api/v1/audit/timeline/vendor/{vendor.id}",
        ]

        leaked = []
        for path in paths:
            response = client.get(path)
            if response.status_code != 200:
                continue
            for secret in (FULL_IBAN, NEW_IBAN, FULL_ACCOUNT):
                if secret in response.text:
                    leaked.append((path, secret))

        assert not leaked, f"full account numbers returned to an auditor: {leaked}"
