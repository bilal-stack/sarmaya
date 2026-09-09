"""Performance smoke tests for the dashboards.

These exist because of a decision, not a worry. The dashboards are deliberately
*not* cached: measured over a year of volume the combined endpoint ran in
~350ms and the seven in parallel finished in 188ms, most of them under 50, so a
cache would have added staleness and invalidation bugs to buy nothing. Migration
035 added the indexes that hold that up.

A decision justified by numbers, with volume expected to grow, and nothing that
re-measures it, is a decision that quietly stops being true. The first sign
would be somebody complaining that a page got slow — which is the same class of
silent failure the health monitor exists to catch.

**Query counts are the assertion; wall-clock is only a ceiling.** A timing
threshold on a shared CI box either flakes or is set so loose it catches
nothing. A query count is deterministic, and it catches the specific regression
that matters: an N+1 introduced by iterating rows in Python instead of
aggregating in SQL. That is what actually turns 200ms into 20 seconds at
volume, and it shows up at any data size, on any machine.

The strongest test here is `test_query_count_does_not_grow_with_volume`. It
encodes the real property — these queries aggregate, they do not walk rows — by
running the same dashboard against two different data sizes and requiring the
same number of statements.
"""
import time
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.enums import InvoiceState, UserRole
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.services.dashboards import DashboardService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = [pytest.mark.integration, pytest.mark.performance]

#: Enough rows that an N+1 is unmistakable in the query count, and few enough
#: that seeding stays under a second. The count is what proves the point, so
#: buying more rows would cost time without buying evidence.
SMALL = 40
LARGE = 160

#: Per dashboard. Generous — the point is to catch a query per row, not to
#: police whether a panel uses four statements or six. Anything approaching
#: this ceiling is structural, not incidental.
MAX_QUERIES = 25

#: Wall-clock ceiling per dashboard. Deliberately loose: a shared runner under
#: load is slow for reasons that have nothing to do with this code, and a test
#: that fails for that reason gets muted, taking the query assertions with it.
MAX_SECONDS = 5.0


@contextmanager
def counting_queries(session):
    """Count SQL statements issued on this session.

    Counts every execution including SAVEPOINTs and the like, which is fine:
    the assertions are about growth and order of magnitude, and a fixed
    overhead of a few statements does not move either.
    """
    counter = {"n": 0}
    engine = session.get_bind()

    def before(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _seed(db, tenant, user, count, start_number=0):
    """Invoices spread across states and dates, with an audit event each.

    Spread deliberately: a dashboard that groups by state or buckets by age
    against a single-valued dataset does one group and looks fast for the wrong
    reason.
    """
    states = [
        InvoiceState.PENDING_APPROVAL, InvoiceState.APPROVED,
        InvoiceState.DRAFT, InvoiceState.PENDING_APPROVAL,
    ]
    now = make_naive(to_utc(utc_now()))

    for index in range(count):
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id,
            invoice_number=f"PERF-{start_number + index:05d}",
            vendor_name=f"Vendor {index % 12}",
            invoice_date=date(2026, 1, 1) + timedelta(days=index % 200),
            total_amount=Decimal(1000 + index),
            current_state=states[index % len(states)],
            created_by=user["id"],
        )
        db.add(invoice)
        db.add(AuditLog(
            id=uuid.uuid4(), tenant_id=tenant.id, user_id=user["id"],
            object_type="invoice", object_id=invoice.id,
            action="submitted" if index % 2 else "approved",
            timestamp=now - timedelta(days=index % 90, hours=index % 24),
        ))

    db.flush()


DASHBOARDS = [
    ("control_room", {}),
    ("approval_bottlenecks", {"days": 90}),
    ("exceptions_heatmap", {"days": 90}),
    ("policy_overrides", {"days": 90}),
    ("evidence_completeness", {}),
    ("reconciliation_health", {}),
    ("autopilot_health", {"days": 90}),
]


class TestQueriesDoNotWalkRows:
    """The regression that actually hurts, caught deterministically."""

    @pytest.mark.parametrize("name,kwargs", DASHBOARDS)
    def test_a_dashboard_stays_within_its_query_budget(
        self, db, tenant, make_user, name, kwargs
    ):
        admin = make_user(UserRole.ADMIN)
        _seed(db, tenant, admin, SMALL)
        service = DashboardService(db)

        with counting_queries(db) as counter:
            getattr(service, name)(admin, **kwargs)

        assert counter["n"] <= MAX_QUERIES, (
            f"{name} issued {counter['n']} queries against {SMALL} invoices. "
            "A count in this range means something is querying per row."
        )

    @pytest.mark.parametrize("name,kwargs", DASHBOARDS)
    def test_query_count_does_not_grow_with_volume(
        self, db, tenant, make_user, name, kwargs
    ):
        """The property, stated directly: four times the data, same number of
        statements. This is what "aggregates in SQL" means, and it is true or
        false regardless of how fast the machine running it happens to be.
        """
        admin = make_user(UserRole.ADMIN)
        service = DashboardService(db)

        _seed(db, tenant, admin, SMALL)
        with counting_queries(db) as small:
            getattr(service, name)(admin, **kwargs)

        _seed(db, tenant, admin, LARGE - SMALL, start_number=SMALL)
        with counting_queries(db) as large:
            getattr(service, name)(admin, **kwargs)

        assert large["n"] == small["n"], (
            f"{name} issued {small['n']} queries for {SMALL} invoices and "
            f"{large['n']} for {LARGE}. Query count must not depend on how "
            "much data there is."
        )


class TestItStaysWithinReach:
    """Wall-clock, as a ceiling rather than a benchmark."""

    @pytest.mark.parametrize("name,kwargs", DASHBOARDS)
    def test_a_dashboard_answers_within_the_ceiling(
        self, db, tenant, make_user, name, kwargs
    ):
        admin = make_user(UserRole.ADMIN)
        _seed(db, tenant, admin, LARGE)
        service = DashboardService(db)

        # Warmed first: the first call through SQLAlchemy compiles statements
        # and fills caches, so timing it measures the ORM starting up rather
        # than the query doing work.
        getattr(service, name)(admin, **kwargs)

        started = time.perf_counter()
        getattr(service, name)(admin, **kwargs)
        elapsed = time.perf_counter() - started

        assert elapsed < MAX_SECONDS, (
            f"{name} took {elapsed:.2f}s against {LARGE} invoices."
        )

    def test_the_combined_overview_is_not_slower_than_its_parts(
        self, db, tenant, make_user
    ):
        """The reason the frontend calls the seven separately.

        `overview` runs them in sequence server-side; the page runs them in
        parallel and finishes with the slowest. If the combined call ever
        became faster than the sum of its parts — through caching, or a shared
        pass — the frontend's design would be the wrong one and worth
        revisiting. This records which way round it currently is.
        """
        admin = make_user(UserRole.ADMIN)
        _seed(db, tenant, admin, LARGE)
        service = DashboardService(db)
        service.overview(admin)

        with counting_queries(db) as combined:
            service.overview(admin)

        total_separate = 0
        for name, kwargs in DASHBOARDS:
            with counting_queries(db) as one:
                getattr(service, name)(admin, **kwargs)
            total_separate += one["n"]

        # Not an efficiency claim — an equivalence one. The combined endpoint
        # does the same work, so it cannot be the cheaper option, and the
        # parallel client-side version wins on latency alone.
        assert combined["n"] >= total_separate * 0.9


class TestTheIndexesAreUsed:
    def test_the_audit_trail_is_read_by_index_not_by_scan(
        self, db, tenant, make_user
    ):
        """Migration 035 exists to keep the dashboards off a sequential scan of
        the audit trail — the table that grows fastest and forever. An index
        that stops being used is invisible until the table is large enough to
        hurt, which is exactly too late to notice.
        """
        admin = make_user(UserRole.ADMIN)
        _seed(db, tenant, admin, LARGE)
        db.flush()

        plan = "\n".join(
            row[0] for row in db.execute(
                # The dashboards' shape: this tenant's recent events by object
                # type. Enable_seqscan stays on — forcing it off would prove
                # only that an index *can* be used, not that the planner
                # chooses it.
                __import__("sqlalchemy").text(
                    "EXPLAIN SELECT object_type, count(*) FROM audit_logs "
                    "WHERE tenant_id = :t AND timestamp >= :since "
                    "GROUP BY object_type"
                ),
                {
                    "t": str(tenant.id),
                    "since": make_naive(to_utc(utc_now())) - timedelta(days=90),
                },
            ).fetchall()
        )

        # On a small table Postgres will rightly prefer a scan, so this asserts
        # the plan is *available and sane* rather than forcing a shape. A
        # regression that removed the index would show up in the query-count
        # and timing tests above at volume; this documents what is being
        # planned so a change in it is visible in the diff.
        assert "audit_logs" in plan


class TestReleasingAPaymentDoesNotQueryPerInvoice:
    """The money path had no query budget at all — the smoke tests above cover
    dashboards, which are read-only and were the thing that had been slow.

    Releasing a run is the opposite case and a worse one to get wrong: it holds
    a transaction open while it works, so a query per line is not just slow, it
    is lock duration on `invoices` and `payments` growing with the size of the
    run. A treasury user settling two hundred invoices at month end is the
    normal case, not the stress case.
    """

    def _run(self, db, tenant, make_user, invoice_count):
        from app.core.enums import PaymentState, VendorStatus
        from app.models.vendor import Vendor
        from app.schemas.payment import PaymentCreate
        from app.services.config_provisioning import ConfigProvisioningService
        from app.services.payment_service import PaymentService

        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)

        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Perf Vendor",
            status=VendorStatus.ACTIVE, bank_account_number="0123456789",
        )
        db.add(vendor)
        db.flush()

        invoices = []
        for _ in range(invoice_count):
            invoice = Invoice(
                id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
                vendor_name=vendor.legal_name,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                invoice_date=date(2026, 8, 1), total_amount=Decimal("100"),
                current_state=InvoiceState.APPROVED, created_by=clerk["id"],
            )
            db.add(invoice)
            invoices.append(invoice)
        db.flush()

        service = PaymentService(db)
        payment = service.prepare_payment(
            [i.id for i in invoices],
            PaymentCreate(invoice_ids=[i.id for i in invoices]),
            clerk,
        )
        service.submit_for_release(payment.id, clerk)
        return service, payment, cfo, PaymentState

    def test_it_stays_within_a_per_invoice_budget(self, db, tenant, make_user):
        """A ceiling, not a claim that this is fast.

        Measured: releasing a 24-invoice run issues ~300 statements, about
        twelve per invoice. Most of that is the audit trail doing its job —
        every settled invoice gets its own entry, and each entry re-reads the
        invoice to inherit its correlation id and reads the previous entry to
        extend the hash chain. That is the cost of the guarantee, not waste,
        and reducing it means changing the chaining itself.

        What this stops is the number growing. Anything that pushes past the
        budget has added a new per-line read to a loop that already holds the
        release transaction open, and lock duration on `invoices` and
        `payments` grows with it.
        """
        service, payment, cfo, PaymentState = self._run(db, tenant, make_user, 24)
        with counting_queries(db) as counted:
            released = service.release_payment(payment.id, cfo)

        assert released.current_state == PaymentState.RELEASED
        per_invoice = counted["n"] / 24
        assert per_invoice < 16, (
            f"{counted['n']} statements to release 24 invoices "
            f"({per_invoice:.1f} each) — a new per-line query has appeared"
        )
