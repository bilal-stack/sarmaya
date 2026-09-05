"""The two CFO reports, checked against data whose answer is known in advance.

Build Book Definition of Done: *"Reports and dashboards for that variant
shipped and validated against seed dataset."* Validated means the number is
checked, not that the endpoint returned without erroring — a report tested only
for "does not throw" is one that will confidently state a wrong total, and a
wrong total on the page a CFO reads is worse than no page, because nobody looks
behind a number that looks plausible.

Two properties get most of the attention here, because both are places where a
report can be wrong while looking right:

  * AP aging is aged against the **due date**, not against how long a record
    has been sitting. TestItAgesAgainstTheDueDate builds the pair that
    distinguishes them — a new invoice already overdue, and an old one not due
    for months — because every other test passes under either definition.
  * Neither report drops what it cannot classify. Invoices with no due date and
    spend with no GL account stay in the denominator and get named, so the
    reports cannot describe a well-behaved fraction while appearing complete.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.enums import InvoiceState, PaymentState, UserRole, VendorStatus
from app.models.bank_statement import BankStatement, BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentLine
from app.models.vendor import Vendor
from app.services.dashboards import DashboardService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _today():
    return _now().date()


def _vendor(db, tenant_id, name=None):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id,
        legal_name=name or f"V-{uuid.uuid4().hex[:6]}", status=VendorStatus.ACTIVE,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _invoice(db, tenant_id, created_by, amount, state, *, vendor=None,
             due_in_days=None, invoice_date=None, **kw):
    """An invoice. `due_in_days` is relative to today — negative is overdue."""
    due = None if due_in_days is None else _today() + timedelta(days=due_in_days)
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name if vendor else "Acme",
        vendor_id=vendor.id if vendor else None,
        invoice_date=invoice_date or _today(),
        due_date=due,
        total_amount=Decimal(str(amount)), current_state=state,
        created_by=created_by, state_entered_at=_now(),
        **kw,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _payment(db, tenant_id, prepared_by, state, invoice, amount):
    payment = Payment(
        id=uuid.uuid4(), tenant_id=tenant_id,
        payment_number=f"PAY-{uuid.uuid4().hex[:6]}",
        payment_date=_today(), current_state=state,
        total_amount=Decimal(str(amount)),
        prepared_by=prepared_by, state_entered_at=_now(),
    )
    db.add(payment)
    db.flush()
    db.add(PaymentLine(
        id=uuid.uuid4(), tenant_id=tenant_id, payment_id=payment.id,
        invoice_id=invoice.id, vendor_id=invoice.vendor_id,
        vendor_name=invoice.vendor_name, line_number=1,
        amount=Decimal(str(amount)),
    ))
    db.flush()
    return payment


def _cfo(make_user):
    return make_user(UserRole.CFO)


# --- AP aging ----------------------------------------------------------------

class TestItAgesAgainstTheDueDate:
    """The distinction the whole report rests on. Every other assertion here
    would pass under either definition; these two would not."""

    def test_a_new_invoice_can_already_be_overdue(self, db, tenant, make_user):
        """Entered today, on terms that expired last month. Aging by how long
        the record has existed would call this current."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 5000, InvoiceState.APPROVED.value,
                 due_in_days=-45, invoice_date=_today())

        report = DashboardService(db).ap_aging(cfo)

        assert report["total_overdue"] == 5000.0
        assert _bucket(report, "31-60 days overdue")["count"] == 1

    def test_an_old_invoice_can_be_nowhere_near_due(self, db, tenant, make_user):
        """Entered six months ago on long terms. Aging by record age would
        call this the most urgent thing on the page."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 9000, InvoiceState.APPROVED.value,
                 due_in_days=60, invoice_date=_today() - timedelta(days=180))

        report = DashboardService(db).ap_aging(cfo)

        assert report["total_overdue"] == 0.0
        assert _bucket(report, "not yet due")["amount"] == 9000.0

    def test_due_today_is_not_yet_overdue(self, db, tenant, make_user):
        """The boundary. Something due today has until the end of it."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 100, InvoiceState.APPROVED.value,
                 due_in_days=0)

        report = DashboardService(db).ap_aging(cfo)

        assert report["total_overdue"] == 0.0
        assert _bucket(report, "not yet due")["count"] == 1


class TestTheBuckets:
    def test_each_invoice_lands_in_the_band_its_lateness_names(
        self, db, tenant, make_user
    ):
        cfo = _cfo(make_user)
        for days_late, amount in ((-5, 100), (15, 200), (45, 400),
                                  (75, 800), (200, 1600)):
            _invoice(db, tenant.id, cfo["id"], amount,
                     InvoiceState.APPROVED.value, due_in_days=-days_late)

        report = DashboardService(db).ap_aging(cfo)

        assert _bucket(report, "not yet due")["amount"] == 100.0
        assert _bucket(report, "1-30 days overdue")["amount"] == 200.0
        assert _bucket(report, "31-60 days overdue")["amount"] == 400.0
        assert _bucket(report, "61-90 days overdue")["amount"] == 800.0
        assert _bucket(report, "over 90 days overdue")["amount"] == 1600.0
        assert report["total_payable"] == 3100.0
        assert report["total_overdue"] == 3000.0

    def test_the_buckets_sum_to_the_balance(self, db, tenant, make_user):
        """A breakdown that does not add up to its own total is the failure
        somebody spots in a board meeting."""
        cfo = _cfo(make_user)
        for days in (-30, 10, 40, 100):
            _invoice(db, tenant.id, cfo["id"], 250,
                     InvoiceState.APPROVED.value, due_in_days=-days)

        report = DashboardService(db).ap_aging(cfo)

        banded = sum(b["amount"] for b in report["aging"])
        assert banded + report["no_due_date"]["amount"] == report["total_payable"]

    def test_only_unsettled_invoices_are_a_payable(self, db, tenant, make_user):
        """Paid money has left and a rejection was never owed. Counting either
        would overstate the liability."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 700, InvoiceState.APPROVED.value,
                 due_in_days=-10)
        _invoice(db, tenant.id, cfo["id"], 900, InvoiceState.PAID.value,
                 due_in_days=-10)
        _invoice(db, tenant.id, cfo["id"], 1100, InvoiceState.REJECTED.value,
                 due_in_days=-10)
        _invoice(db, tenant.id, cfo["id"], 1300, InvoiceState.DRAFT.value,
                 due_in_days=-10)

        report = DashboardService(db).ap_aging(cfo)

        assert report["total_payable"] == 700.0


class TestMissingDueDatesAreNotHidden:
    def test_they_are_counted_separately_rather_than_called_current(
        self, db, tenant, make_user
    ):
        """The column is nullable. Bucketing missing as "not yet due" would
        move a possibly-overdue balance into the comfortable column, and the
        missing dates are themselves the thing worth knowing — nothing can
        chase what has no date."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 4000, InvoiceState.APPROVED.value,
                 due_in_days=None)

        report = DashboardService(db).ap_aging(cfo)

        assert report["no_due_date"] == {"count": 1, "amount": 4000.0}
        assert _bucket(report, "not yet due")["amount"] == 0.0
        # Still owed, so still in the balance.
        assert report["total_payable"] == 4000.0

    def test_they_do_not_count_as_overdue_either(self, db, tenant, make_user):
        """Unknown is unknown. Calling it late would be as wrong as calling it
        current, just in the other direction."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 4000, InvoiceState.APPROVED.value,
                 due_in_days=None)

        assert DashboardService(db).ap_aging(cfo)["total_overdue"] == 0.0


class TestTheExceptionFunnel:
    def test_an_approved_invoice_nobody_scheduled_shows_in_that_stage(
        self, db, tenant, make_user
    ):
        """The stage that quietly grows: agreed to, and then forgotten."""
        cfo = _cfo(make_user)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, cfo["id"], 3000, InvoiceState.APPROVED.value,
                 vendor=vendor, due_in_days=-5)

        report = DashboardService(db).ap_aging(cfo)

        assert _stage(report, "approved_not_scheduled")["amount"] == 3000.0
        assert _stage(report, "scheduled_not_released")["amount"] == 0.0

    def test_once_scheduled_it_moves_out_of_that_stage(
        self, db, tenant, make_user
    ):
        """Counted from the payment lines rather than a status column, so an
        invoice in a run is in a run whatever anything else says."""
        cfo = _cfo(make_user)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, cfo["id"], 3000,
                           InvoiceState.APPROVED.value, vendor=vendor,
                           due_in_days=-5)
        _payment(db, tenant.id, cfo["id"], PaymentState.PENDING_RELEASE.value,
                 invoice, 3000)

        report = DashboardService(db).ap_aging(cfo)

        assert _stage(report, "approved_not_scheduled")["amount"] == 0.0
        assert _stage(report, "scheduled_not_released")["amount"] == 3000.0

    def test_released_money_not_on_a_statement_is_its_own_stage(
        self, db, tenant, make_user
    ):
        """Instructed but unconfirmed. The gap between what the ledger says
        left and what the bank shows leaving."""
        cfo = _cfo(make_user)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, cfo["id"], 2500,
                           InvoiceState.APPROVED.value, vendor=vendor,
                           due_in_days=-5)
        _payment(db, tenant.id, cfo["id"], PaymentState.RELEASED.value,
                 invoice, 2500)

        report = DashboardService(db).ap_aging(cfo)

        assert _stage(report, "released_not_cleared")["amount"] == 2500.0

    def test_a_reconciled_payment_leaves_the_funnel(self, db, tenant, make_user):
        """Once the bank confirms it, it is gone. A funnel that never empties
        is a list."""
        cfo = _cfo(make_user)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, cfo["id"], 2500,
                           InvoiceState.APPROVED.value, vendor=vendor,
                           due_in_days=-5)
        payment = _payment(db, tenant.id, cfo["id"],
                           PaymentState.RELEASED.value, invoice, 2500)

        statement = BankStatement(
            id=uuid.uuid4(), tenant_id=tenant.id,
            statement_reference=f"ST-{uuid.uuid4().hex[:6]}",
            account_identifier="0001", source_format="csv",
            statement_date=_today(), file_hash=uuid.uuid4().hex,
            imported_by=cfo["id"],
        )
        db.add(statement)
        db.flush()
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id,
            bank_statement_id=statement.id, line_number=1,
            value_date=_today(), amount=Decimal("2500"), is_debit=True,
            description="paid", matched_payment_id=payment.id,
        ))
        db.flush()

        report = DashboardService(db).ap_aging(cfo)

        assert _stage(report, "released_not_cleared")["amount"] == 0.0

    def test_the_stages_are_in_pipeline_order(self, db, tenant, make_user):
        """The funnel is read top to bottom. Presenting it out of order would
        make a growing middle stage look like a shrinking one."""
        report = DashboardService(db).ap_aging(_cfo(make_user))

        assert [s["stage"] for s in report["funnel"]] == [
            "awaiting_approval", "approved_not_scheduled",
            "scheduled_not_released", "released_not_cleared",
        ]


# --- Spend analytics ---------------------------------------------------------

class TestSpendIsWhatWasAgreedTo:
    def test_an_unapproved_invoice_is_not_spend(self, db, tenant, make_user):
        """It is a claim. Counting it would let anybody inflate the spend
        figure by entering an invoice."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 5000,
                 InvoiceState.PENDING_APPROVAL.value)

        assert DashboardService(db).spend_analytics(cfo)["total_spend"] == 0.0

    def test_approved_and_paid_both_count(self, db, tenant, make_user):
        """Approval is the commitment; payment is its settlement. Counting
        only paid would report a spend figure that lags reality by the length
        of the payment run."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 400, InvoiceState.APPROVED.value)
        _invoice(db, tenant.id, cfo["id"], 600, InvoiceState.PAID.value)

        assert DashboardService(db).spend_analytics(cfo)["total_spend"] == 1000.0

    def test_it_respects_the_window(self, db, tenant, make_user):
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 700, InvoiceState.PAID.value,
                 invoice_date=_today() - timedelta(days=10))
        _invoice(db, tenant.id, cfo["id"], 900, InvoiceState.PAID.value,
                 invoice_date=_today() - timedelta(days=400))

        report = DashboardService(db).spend_analytics(cfo, days=30)

        assert report["total_spend"] == 700.0


class TestTheDimensionsThatExist:
    def test_it_totals_by_vendor(self, db, tenant, make_user):
        cfo = _cfo(make_user)
        big = _vendor(db, tenant.id, name="Big Supplier")
        small = _vendor(db, tenant.id, name="Small Supplier")
        _invoice(db, tenant.id, cfo["id"], 800, InvoiceState.PAID.value, vendor=big)
        _invoice(db, tenant.id, cfo["id"], 200, InvoiceState.PAID.value, vendor=big)
        _invoice(db, tenant.id, cfo["id"], 150, InvoiceState.PAID.value, vendor=small)

        report = DashboardService(db).spend_analytics(cfo)

        top = report["by_vendor"][0]
        assert top["key"] == "Big Supplier"
        assert top["amount"] == 1000.0 and top["count"] == 2
        assert report["vendor_count"] == 2

    def test_it_totals_by_gl_account_and_cost_centre(self, db, tenant, make_user):
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 300, InvoiceState.PAID.value,
                 gl_account_code="6000", cost_center="OPS")
        _invoice(db, tenant.id, cfo["id"], 500, InvoiceState.PAID.value,
                 gl_account_code="6000", cost_center="ENG")

        report = DashboardService(db).spend_analytics(cfo)

        assert report["by_gl_account"][0] == {"key": "6000", "count": 2, "amount": 800.0}
        assert {c["key"] for c in report["by_cost_centre"]} == {"OPS", "ENG"}

    def test_concentration_is_one_number(self, db, tenant, make_user):
        """Negotiating position on one side, single-supplier exposure on the
        other. Both are read off one figure, not by adding up a table."""
        cfo = _cfo(make_user)
        whale = _vendor(db, tenant.id, name="Whale")
        _invoice(db, tenant.id, cfo["id"], 700, InvoiceState.PAID.value, vendor=whale)
        for i in range(5):
            other = _vendor(db, tenant.id, name=f"Other {i}")
            _invoice(db, tenant.id, cfo["id"], 60, InvoiceState.PAID.value,
                     vendor=other)

        report = DashboardService(db).spend_analytics(cfo)

        # 700 + 60*4 of the top five, out of 1000.
        assert report["vendor_count"] == 6
        assert report["top_5_vendor_share_pct"] == 94.0

    def test_it_is_withheld_when_there_are_five_or_fewer_vendors(
        self, db, tenant, make_user
    ):
        """It would be 100% by arithmetic, and a headline 100% reads as an
        alarm about exposure rather than a fact about the size of the supplier
        list. Withheld rather than reported, the same way match rate is on the
        AP/Treasury report — a number that cannot be wrong and cannot be
        informative is worse than its absence."""
        cfo = _cfo(make_user)
        for i in range(3):
            vendor = _vendor(db, tenant.id, name=f"V{i}")
            _invoice(db, tenant.id, cfo["id"], 100, InvoiceState.PAID.value,
                     vendor=vendor)

        report = DashboardService(db).spend_analytics(cfo)

        assert report["vendor_count"] == 3
        assert report["top_5_vendor_share_pct"] is None

    def test_there_is_no_category_breakdown_and_it_says_so(
        self, db, tenant, make_user
    ):
        """Invoices carry no category, so a category chart would be invented
        here rather than measured — the same reasoning the approval matrix
        applies to its own missing axis."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 100, InvoiceState.PAID.value)

        assert DashboardService(db).spend_analytics(cfo)["by_category"] is None


class TestUnclassifiedSpendStaysInTheDenominator:
    def test_it_is_named_rather_than_dropped(self, db, tenant, make_user):
        """A breakdown that silently omits the unclassified reads as complete
        while describing a fraction — and the fraction it describes is the
        well-behaved one. The unclassified balance is the part nobody has had
        to justify, which is the part worth seeing."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 250, InvoiceState.PAID.value,
                 gl_account_code="6000", cost_center="OPS")
        _invoice(db, tenant.id, cfo["id"], 750, InvoiceState.PAID.value)

        report = DashboardService(db).spend_analytics(cfo)

        assert report["total_spend"] == 1000.0
        assert report["unclassified"]["no_gl_account"] == 750.0
        assert report["unclassified"]["no_gl_account_pct"] == 75.0
        assert report["unclassified"]["no_cost_centre"] == 750.0

    def test_the_classified_breakdown_excludes_it(self, db, tenant, make_user):
        """The two figures are complementary, so the account table plus the
        unclassified figure is the total. A reader must be able to add them."""
        cfo = _cfo(make_user)
        _invoice(db, tenant.id, cfo["id"], 250, InvoiceState.PAID.value,
                 gl_account_code="6000")
        _invoice(db, tenant.id, cfo["id"], 750, InvoiceState.PAID.value)

        report = DashboardService(db).spend_analytics(cfo)

        classified = sum(a["amount"] for a in report["by_gl_account"])
        assert classified + report["unclassified"]["no_gl_account"] == \
            report["total_spend"]


class TestPermissions:
    def test_both_read_with_the_invoice_gate(self, db, tenant, make_user):
        """Aggregating records anybody with invoices.view could open one by
        one, so the gate is the same one."""
        clerk = make_user(UserRole.AP_CLERK)

        assert DashboardService(db).ap_aging(clerk) is not None
        assert DashboardService(db).spend_analytics(clerk) is not None

    def test_the_api_serves_a_cfo(self, client, as_user, make_user):
        as_user(_cfo(make_user))

        assert client.get("/api/v1/dashboard/ap-aging").status_code == 200
        assert client.get("/api/v1/dashboard/spend-analytics").status_code == 200


# --- helpers -----------------------------------------------------------------

def _bucket(report, label):
    return next(b for b in report["aging"] if b["bucket"] == label)


def _stage(report, name):
    return next(s for s in report["funnel"] if s["stage"] == name)
