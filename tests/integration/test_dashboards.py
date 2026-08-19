"""The seven global dashboards.

Build Book Definition of Done: *"Reports and dashboards for that variant
shipped and validated against seed dataset."*

Validated means the numbers are checked against data whose answer is known in
advance. A dashboard tested only for "returns without erroring" is a dashboard
that will confidently report the wrong total for a year — and a wrong total on
the page a CFO reads is worse than no page, because nobody goes looking behind
a number that looks plausible.

So each test below builds a small, known situation and asserts the exact figure.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import InvoiceState, PaymentState, UserRole, VendorStatus
from app.models.audit_log import AuditLog
from app.models.bank_statement import BankStatement, BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.dashboards import DashboardService, _bucket
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _vendor(db, tenant_id, status=VendorStatus.ACTIVE, name=None):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id,
        legal_name=name or f"V-{uuid.uuid4().hex[:6]}", status=status,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _invoice(db, tenant_id, created_by, amount, state, *, vendor=None,
             entered_days_ago=1, **kw):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name if vendor else "Acme",
        vendor_id=vendor.id if vendor else None,
        invoice_date=date(2026, 8, 1), total_amount=Decimal(str(amount)),
        current_state=state, created_by=created_by,
        state_entered_at=_now() - timedelta(days=entered_days_ago),
        **kw,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _audit(db, tenant_id, user, obj_id, action, at, **kw):
    entry = AuditLog(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user["id"],
        user_email=user["email"], user_role=user["role"],
        object_type="invoice", object_id=obj_id, action=action,
        timestamp=at, custom_metadata={}, **kw,
    )
    db.add(entry)
    db.flush()
    return entry


class TestControlRoom:
    def test_it_totals_the_cash_behind_each_reason(self, db, tenant, make_user):
        """The figure the whole dashboard exists for. '48 items pending' and
        '4.2M pending' prompt different conversations."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, admin["id"], 100_000,
                 InvoiceState.PENDING_APPROVAL, vendor=vendor)
        _invoice(db, tenant.id, admin["id"], 250_000,
                 InvoiceState.PENDING_APPROVAL, vendor=vendor)
        _invoice(db, tenant.id, admin["id"], 40_000,
                 InvoiceState.APPROVED, vendor=vendor)

        result = DashboardService(db).control_room(admin)

        rows = {r["reason"]: r for r in result["blocked"]}
        assert rows["Awaiting approval"]["count"] == 2
        assert rows["Awaiting approval"]["amount"] == 350_000
        assert rows["Approved, not yet paid"]["amount"] == 40_000
        assert result["total_amount_stuck"] == 390_000
        assert result["total_items_stuck"] == 3

    def test_an_unverified_vendor_shows_as_its_own_reason(self, db, tenant, make_user):
        """The control working is still money stopped, and the reader needs to
        know which of the two they are looking at."""
        admin = make_user(UserRole.ADMIN)
        blocked_vendor = _vendor(db, tenant.id, VendorStatus.PENDING_VERIFICATION)
        _invoice(db, tenant.id, admin["id"], 75_000,
                 InvoiceState.VALIDATED, vendor=blocked_vendor)

        result = DashboardService(db).control_room(admin)

        rows = {r["reason"]: r for r in result["blocked"]}
        assert rows["Vendor not verified"]["count"] == 1
        assert rows["Vendor not verified"]["amount"] == 75_000

    def test_an_acknowledged_duplicate_stops_counting_as_stuck(
        self, db, tenant, make_user
    ):
        """It was reviewed and let through. Still counting it would mean the
        number never goes down no matter what anybody does."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        original = _invoice(db, tenant.id, admin["id"], 10_000,
                            InvoiceState.APPROVED, vendor=vendor)
        _invoice(db, tenant.id, admin["id"], 10_000, InvoiceState.PENDING_APPROVAL,
                 vendor=vendor, potential_duplicate_id=original.id,
                 duplicate_acknowledged=True)

        result = DashboardService(db).control_room(admin)

        assert "Possible duplicate" not in {r["reason"] for r in result["blocked"]}

    def test_nothing_stuck_is_reported_as_nothing(self, db, tenant, make_user):
        """An empty dashboard has to be legible as 'all clear' rather than as
        a page that failed to load."""
        admin = make_user(UserRole.ADMIN)

        result = DashboardService(db).control_room(admin)

        assert result["blocked"] == []
        assert result["total_amount_stuck"] == 0


class TestApprovalBottlenecks:
    def test_it_measures_from_the_audit_trail(self, db, tenant, make_user):
        """Cycle time is derived from what happened, not from a duration column
        that would only be as right as the code that last wrote it."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 50_000,
                           InvoiceState.APPROVED, vendor=vendor)

        submitted = _now() - timedelta(hours=10)
        _audit(db, tenant.id, admin, invoice.id, "submitted_for_approval", submitted)
        _audit(db, tenant.id, manager, invoice.id, "approved",
               submitted + timedelta(hours=6))

        result = DashboardService(db).approval_bottlenecks(admin)

        by_role = {r["role"]: r for r in result["by_role"]}
        assert by_role["manager"]["decisions"] == 1
        assert by_role["manager"]["median_hours"] == 6.0

    def test_the_median_is_not_dragged_by_one_slow_case(self, db, tenant, make_user):
        """One invoice that sat for three weeks pulls an average somewhere no
        real invoice ever was, which is why both are reported."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)

        for hours in (2, 2, 2, 500):
            invoice = _invoice(db, tenant.id, admin["id"], 1000,
                               InvoiceState.APPROVED, vendor=vendor)
            submitted = _now() - timedelta(hours=hours + 1)
            _audit(db, tenant.id, admin, invoice.id, "submitted_for_approval", submitted)
            _audit(db, tenant.id, manager, invoice.id, "approved",
                   submitted + timedelta(hours=hours))

        result = DashboardService(db).approval_bottlenecks(admin)
        manager_row = next(r for r in result["by_role"] if r["role"] == "manager")

        assert manager_row["median_hours"] == 2.0
        assert manager_row["average_hours"] > 100
        assert manager_row["slowest_hours"] == 500.0

    def test_undecided_items_are_bucketed_by_age(self, db, tenant, make_user):
        """The decided ones say how fast you were; these say what is happening
        now, and only the second can still be changed."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.PENDING_APPROVAL,
                 vendor=vendor, entered_days_ago=1)
        _invoice(db, tenant.id, admin["id"], 2000, InvoiceState.PENDING_APPROVAL,
                 vendor=vendor, entered_days_ago=45)

        result = DashboardService(db).approval_bottlenecks(admin)

        buckets = {b["bucket"]: b for b in result["still_waiting"]}
        assert buckets["0-2 days"]["count"] == 1
        assert buckets["over 30 days"]["count"] == 1
        assert buckets["over 30 days"]["amount"] == 2000


class TestExceptionsHeatmap:
    def test_it_groups_blocks_by_reason_and_vendor(self, db, tenant, make_user):
        """Every blocked action writes its reason, precisely so this can be
        answered without anybody having instrumented it in advance."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, name="Silverbrook Trading")
        invoice = _invoice(db, tenant.id, admin["id"], 1000,
                           InvoiceState.VALIDATED, vendor=vendor)

        for _ in range(3):
            _audit(db, tenant.id, admin, invoice.id, "approval_blocked", _now(),
                   after_value={"reason": "vendor_blocked",
                                "vendor_name": "Silverbrook Trading"})

        result = DashboardService(db).exceptions_heatmap(admin)

        assert result["total"] == 3
        assert result["by_reason"][0] == {"reason": "vendor_blocked", "count": 3}
        assert result["by_vendor"][0] == {"vendor": "Silverbrook Trading", "count": 3}


class TestPolicyOverrides:
    def test_it_counts_who_overrode_what(self, db, tenant, make_user):
        """Not a list of wrongdoing — overrides are legitimate and always carry
        a reason. One person holding most of them is the finding."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 5000,
                           InvoiceState.APPROVED, vendor=vendor)

        _audit(db, tenant.id, manager, invoice.id, "duplicate_acknowledged", _now(),
               comment="Vendor re-issued with a corrected tax line.")

        result = DashboardService(db).policy_overrides(admin)

        assert result["total"] == 1
        assert result["by_person"][0]["who"] == manager["email"]
        assert result["by_person"][0]["amount"] == 5000
        assert "corrected tax line" in result["recent"][0]["reason"]


class TestEvidenceCompleteness:
    def test_it_counts_invoices_with_nothing_behind_them(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.APPROVED, vendor=vendor)
        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.APPROVED, vendor=vendor)

        result = DashboardService(db).evidence_completeness(admin)

        assert result["invoices"] == 2
        assert result["missing_document"] == 2
        assert result["completeness_pct"] == 0.0

    def test_a_draft_is_not_counted_as_missing_evidence(self, db, tenant, make_user):
        """Nobody has claimed a draft is finished, so holding it to an audit
        standard would report a gap that is not one."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.DRAFT, vendor=vendor)

        assert DashboardService(db).evidence_completeness(admin)["missing_document"] == 0


class TestReconciliationHealth:
    def test_unexplained_debits_are_aged(self, db, tenant, make_user):
        """The oldest bucket is the point: a debit nobody has explained in a
        month is either a control failure or a payment made outside the
        system."""
        admin = make_user(UserRole.ADMIN)
        statement = BankStatement(
            id=uuid.uuid4(), tenant_id=tenant.id, statement_reference="STMT-1",
            source_format="csv", file_hash="x" * 64, imported_by=admin["id"],
        )
        db.add(statement)
        db.flush()
        for days_ago, amount in ((1, 5000), (40, 90_000)):
            db.add(BankStatementLine(
                id=uuid.uuid4(), tenant_id=tenant.id,
                bank_statement_id=statement.id, line_number=days_ago,
                value_date=(_now() - timedelta(days=days_ago)).date(),
                amount=Decimal(str(amount)), is_debit=True, description="UNKNOWN",
            ))
        db.flush()

        result = DashboardService(db).reconciliation_health(admin)

        assert result["unexplained_count"] == 2
        assert result["unexplained_amount"] == 95_000
        buckets = {b["bucket"]: b for b in result["aging"]}
        assert buckets["over 30 days"]["amount"] == 90_000
        assert buckets["0-2 days"]["amount"] == 5000

    def test_the_match_rate_counts_both_sides(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        statement = BankStatement(
            id=uuid.uuid4(), tenant_id=tenant.id, statement_reference="STMT-2",
            source_format="csv", file_hash="y" * 64, imported_by=admin["id"],
        )
        payment = Payment(
            id=uuid.uuid4(), tenant_id=tenant.id, payment_number="PAY-1",
            payment_date=date(2026, 8, 1), total_amount=Decimal("1000"),
            current_state=PaymentState.RELEASED, prepared_by=admin["id"],
        )
        db.add_all([statement, payment])
        db.flush()
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id, bank_statement_id=statement.id,
            line_number=1, value_date=date(2026, 8, 2), amount=Decimal("1000"),
            is_debit=True, matched_payment_id=payment.id,
        ))
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id, bank_statement_id=statement.id,
            line_number=2, value_date=date(2026, 8, 2), amount=Decimal("500"),
            is_debit=True,
        ))
        db.flush()

        result = DashboardService(db).reconciliation_health(admin)

        assert result["match_rate_pct"] == 50.0


class TestAutopilotHealth:
    def test_approvals_and_reversals_are_reported_together(self, db, tenant, make_user):
        """Autopilot approving a lot is only good news while the reversal rate
        stays near zero, so the two are read together or not at all."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        invoices = [
            _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.APPROVED,
                     vendor=vendor)
            for _ in range(4)
        ]
        for invoice in invoices:
            _audit(db, tenant.id, admin, invoice.id, "autopilot_approved", _now())
        _audit(db, tenant.id, admin, invoices[0].id, "autopilot_reverted", _now())

        result = DashboardService(db).autopilot_health(admin)

        assert result["auto_approved"] == 4
        assert result["reverted"] == 1
        assert result["reversal_rate_pct"] == 25.0


class TestAccess:
    def test_a_role_that_cannot_read_invoices_cannot_read_the_aggregates(
        self, db, tenant, make_user
    ):
        """Nothing here should expose a figure somebody could not reach by
        opening the records themselves."""
        from app.core.enums import UserRole as R

        outsider = make_user(R.SYSTEM)
        outsider["role"] = "approver"   # holds invoices.view
        assert DashboardService(db).control_room(outsider)

        blocked = make_user(R.SYSTEM)
        blocked["role"] = "nobody"
        with pytest.raises(PermissionError):
            DashboardService(db).control_room(blocked)


class TestBuckets:
    @pytest.mark.parametrize("days,expected", [
        (0, "0-2 days"), (1.9, "0-2 days"), (2, "3-7 days"),
        (6.9, "3-7 days"), (7, "8-30 days"), (29.9, "8-30 days"),
        (30, "over 30 days"), (400, "over 30 days"),
    ])
    def test_one_ladder_everywhere(self, days, expected):
        """Two dashboards disagreeing about what 'old' means is the kind of
        thing nobody notices and everybody argues about later."""
        assert _bucket(days) == expected
