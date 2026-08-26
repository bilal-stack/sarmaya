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
from app.models.watchlist_alert import WatchlistAlert
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


class TestInvoiceThroughput:
    def test_it_measures_capture_to_paid_from_the_trail(self, db, tenant, make_user):
        """Not from a stored duration: the trail is what happened, and a
        duration column is only as right as the code that last wrote it."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 1000,
                           InvoiceState.PAID, vendor=vendor)

        start = _now() - timedelta(days=5)
        _audit(db, tenant.id, admin, invoice.id, "created", start)
        _audit(db, tenant.id, admin, invoice.id, "marked_paid",
               start + timedelta(hours=48))

        result = DashboardService(db).invoice_throughput(admin)

        assert result["captured"] == 1
        assert result["settled"] == 1
        assert result["capture_to_paid_hours"]["median"] == pytest.approx(48, abs=0.5)

    def test_rework_is_grouped_by_reason(self, db, tenant, make_user):
        """A state count reports a re-approved invoice as one approval. The
        reasons are the only form of this figure anybody can act on."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 1000,
                           InvoiceState.APPROVED, vendor=vendor)
        _audit(db, tenant.id, admin, invoice.id, "created", _now() - timedelta(days=2))
        _audit(db, tenant.id, admin, invoice.id, "rejected", _now(),
               comment="Wrong cost centre")
        _audit(db, tenant.id, admin, invoice.id, "rejected", _now(),
               comment="Wrong cost centre")
        _audit(db, tenant.id, admin, invoice.id, "approval_blocked", _now(),
               after_value={"reason": "sod_self_approval"})

        result = DashboardService(db).invoice_throughput(admin)

        assert result["rework_events"] == 3
        top = result["rework_drivers"][0]
        assert top["reason"] == "Wrong cost centre" and top["count"] == 2

    def test_match_rate_is_null_rather_than_invented(self, db, tenant, make_user):
        """Three-way match is computed on demand and never stored, so there is
        no record of what an invoice matched when it was approved. A rate
        recomputed today against goods receipts that have since changed would
        be a different number wearing the same name."""
        admin = make_user(UserRole.ADMIN)

        assert DashboardService(db).invoice_throughput(admin)["match_rate_pct"] is None

    def test_no_activity_does_not_divide_by_zero(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        result = DashboardService(db).invoice_throughput(admin)
        assert result["captured"] == 0
        assert result["rework_rate_pct"] == 0.0
        assert result["capture_to_paid_hours"]["median"] is None


class TestPaymentRunStatus:
    def _payment(self, db, tenant, user, state, **kw):
        payment = Payment(
            id=uuid.uuid4(), tenant_id=tenant.id,
            payment_number=f"PAY-{uuid.uuid4().hex[:6]}",
            payment_date=date(2026, 8, 1), total_amount=Decimal("5000"),
            current_state=state, prepared_by=user["id"], **kw,
        )
        db.add(payment)
        db.flush()
        return payment

    def test_it_totals_each_state_with_its_value(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        self._payment(db, tenant, admin, PaymentState.DRAFT)
        self._payment(db, tenant, admin, PaymentState.DRAFT)
        self._payment(db, tenant, admin, PaymentState.RELEASED,
                      released_at=_now(), bank_file_generated_at=_now())

        result = DashboardService(db).payment_run_status(admin)

        states = {r["state"]: r for r in result["by_state"]}
        assert states["draft"]["count"] == 2
        assert states["draft"]["value"] == 10000

    def test_a_released_run_with_no_bank_file_is_surfaced(
        self, db, tenant, make_user
    ):
        """Authorised, and nothing has been handed to the bank."""
        admin = make_user(UserRole.ADMIN)
        self._payment(db, tenant, admin, PaymentState.RELEASED,
                      released_at=_now(), bank_file_generated_at=None)

        result = DashboardService(db).payment_run_status(admin)

        assert len(result["awaiting_bank_file"]) == 1
        assert result["unreconciled_after_release"] == []

    def test_a_released_run_never_seen_on_a_statement_is_aged(
        self, db, tenant, make_user
    ):
        """The closest this system gets to "failed", named for what it
        observed rather than what it infers."""
        admin = make_user(UserRole.ADMIN)
        self._payment(db, tenant, admin, PaymentState.RELEASED,
                      released_at=_now() - timedelta(days=9),
                      bank_file_generated_at=_now() - timedelta(days=9))

        rows = DashboardService(db).payment_run_status(admin)["unreconciled_after_release"]

        assert len(rows) == 1
        assert rows[0]["age_days"] == pytest.approx(9, abs=0.5)

    def test_failed_and_reissued_are_declared_absent_not_zero(
        self, db, tenant, make_user
    ):
        """Reporting zero failures would be read as "none failed" rather than
        "we cannot see" — Sarmaya never moves money."""
        admin = make_user(UserRole.ADMIN)

        absent = DashboardService(db).payment_run_status(admin)["not_reported"]

        assert "failed" in absent and "reissued" in absent

    def test_it_reads_with_payments_view_not_the_dashboard_permission(
        self, db, tenant, make_user
    ):
        """A manager can open every invoice this touches and still cannot open
        a payment run, so they must not read its value here either."""
        manager = make_user(UserRole.MANAGER)
        with pytest.raises(PermissionError):
            DashboardService(db).payment_run_status(manager)

    def test_a_clerk_can_read_it(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        assert DashboardService(db).payment_run_status(clerk)["by_state"] == []


class TestDuplicateAndAnomaly:
    def test_it_separates_held_back_from_paid_anyway(self, db, tenant, make_user):
        """"Prevented" is the number most easily overclaimed, so it counts what
        the flag actually held back rather than what might have been lost."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        original = _invoice(db, tenant.id, admin["id"], 1000,
                            InvoiceState.PAID, vendor=vendor)

        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.CANCELLED,
                 vendor=vendor, potential_duplicate_id=original.id)
        _invoice(db, tenant.id, admin["id"], 2000, InvoiceState.PAID,
                 vendor=vendor, potential_duplicate_id=original.id,
                 duplicate_acknowledged=True)
        _invoice(db, tenant.id, admin["id"], 500, InvoiceState.PENDING_APPROVAL,
                 vendor=vendor, potential_duplicate_id=original.id)

        result = DashboardService(db).duplicate_and_anomaly(admin)

        assert result["flagged"] == 3
        assert result["stopped"] == 1
        assert result["paid_anyway"] == 1
        assert result["still_held"] == 1
        assert result["value_held_back"] == 1500      # cancelled + still open
        assert result["value_paid_anyway"] == 2000

    def test_watchlist_hits_split_open_from_acknowledged(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        for acknowledged in (True, False, False):
            db.add(WatchlistAlert(
                id=uuid.uuid4(), tenant_id=tenant.id,
                category="vendor_bank_change", severity="high",
                object_type="vendor", object_id=uuid.uuid4(),
                summary="Bank details changed",
                acknowledged_at=_now() if acknowledged else None,
            ))
        db.flush()

        rows = DashboardService(db).duplicate_and_anomaly(admin)["watchlist"]

        assert len(rows) == 1
        assert rows[0]["count"] == 3
        assert rows[0]["acknowledged"] == 1
        assert rows[0]["open"] == 2

    def test_an_unflagged_invoice_is_not_counted(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _invoice(db, tenant.id, admin["id"], 1000, InvoiceState.PAID, vendor=vendor)

        assert DashboardService(db).duplicate_and_anomaly(admin)["flagged"] == 0


class TestSodViolations:
    """The one report here whose empty state is good news — and the reason it
    is worth having. A control that has never fired looks, from outside,
    exactly like a control that was never wired up."""

    def test_it_separates_a_self_approval_from_a_clerical_block(
        self, db, tenant, make_user
    ):
        """Both are refusals; only one is a segregation failure. Merged into a
        single number, a rise in missing vendor links would read as a rise in
        attempted self-dealing."""
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 5000,
                           InvoiceState.PENDING_APPROVAL, vendor=vendor)

        _audit(db, tenant.id, manager, invoice.id, "approval_blocked", _now(),
               after_value={"reason": "sod_self_approval"})
        _audit(db, tenant.id, manager, invoice.id, "approval_blocked", _now(),
               after_value={"reason": "no_vendor_link"})

        result = DashboardService(db).sod_violations(admin)

        assert result["total_blocked"] == 2
        assert result["sod_blocked"] == 1
        assert result["other_blocked"] == 1

    def test_it_names_who_was_refused(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        manager = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 5000,
                           InvoiceState.PENDING_APPROVAL, vendor=vendor)

        _audit(db, tenant.id, manager, invoice.id, "approval_blocked", _now(),
               after_value={"reason": "sod_self_approval"})

        result = DashboardService(db).sod_violations(admin)

        person = result["by_person"][0]
        assert person["who"] == manager["email"]
        assert person["sod_count"] == 1
        assert result["recent"][0]["label"] == "Tried to approve their own record"

    def test_every_kind_of_refusal_is_collected_not_just_invoices(
        self, db, tenant, make_user
    ):
        """The filter matches the "_blocked" suffix rather than a list of known
        actions, so a refusal added to a new module appears here without
        anybody remembering to register it. That is the whole failure mode a
        hand-maintained list has."""
        admin = make_user(UserRole.ADMIN)
        cfo = make_user(UserRole.CFO)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 5000,
                           InvoiceState.APPROVED, vendor=vendor)

        for action, reason in [
            ("release_blocked", "self_release"),
            ("reconciliation_blocked", "self_reconciliation"),
            ("vendor_activation_blocked", "sod_self_activation"),
            ("bank_change_approval_blocked", "self_approval"),
            # Invented on purpose: a module that does not exist yet.
            ("shipment_dispatch_blocked", "self_dispatch"),
        ]:
            _audit(db, tenant.id, cfo, invoice.id, action, _now(),
                   after_value={"reason": reason})

        result = DashboardService(db).sod_violations(admin)

        assert result["total_blocked"] == 5
        # The four known ones classify as SoD; the invented reason is reported
        # as itself rather than dropped or guessed at.
        assert result["sod_blocked"] == 4
        reasons = {r["reason"] for r in result["by_reason"]}
        assert "self_dispatch" in reasons

    def test_an_ordinary_approval_is_not_a_refusal(self, db, tenant, make_user):
        """The report must not turn normal work into a security finding."""
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        invoice = _invoice(db, tenant.id, admin["id"], 5000,
                           InvoiceState.APPROVED, vendor=vendor)
        _audit(db, tenant.id, admin, invoice.id, "approved", _now())

        assert DashboardService(db).sod_violations(admin)["total_blocked"] == 0

    def test_nothing_refused_is_reported_as_nothing_refused(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)

        result = DashboardService(db).sod_violations(admin)

        assert result["total_blocked"] == 0
        assert result["by_person"] == []
        assert result["by_reason"] == []

    def test_it_reads_with_audit_view_not_the_dashboard_permission(
        self, db, tenant, make_user
    ):
        """A manager can open every invoice this aggregates and still must not
        read "who tried to do something they were not allowed to"."""
        manager = make_user(UserRole.MANAGER)
        with pytest.raises(PermissionError):
            DashboardService(db).sod_violations(manager)

    def test_an_auditor_can_read_it(self, db, tenant, make_user):
        auditor = make_user(UserRole.AUDITOR)
        assert DashboardService(db).sod_violations(auditor)["total_blocked"] == 0


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
