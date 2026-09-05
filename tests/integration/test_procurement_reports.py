"""RFQ cycle time and sourcing savings, checked against known answers.

Build Book, Procurement Leadership. Two things here are easy to get wrong in a
way that still renders, so both get their own class:

  * **The stages are separate on purpose.** Three of the four are delays we
    control and one is a window we chose to give vendors. A single "cycle
    time" average hides a fortnight of nobody deciding behind a generous
    vendor window, so TestTheStagesAreSeparate builds exactly that shape and
    asserts the slow stage is visible on its own.
  * **A saving needs something to have been saved against.** An RFQ awarded on
    one quote did not save anything, it just happened. Folding those in at
    zero would dilute the rate with awards that never had a comparison, so
    they are counted separately and TestSavingsNeedAnAlternative proves it.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.enums import (
    QuoteState, RequisitionState, RFQState, UserRole, VendorStatus,
)
from app.models.audit_log import AuditLog
from app.models.requisition import PurchaseRequisition
from app.models.rfq import RFQ, Quote, RFQVendor
from app.models.vendor import Vendor
from app.services.dashboards import DashboardService, _median_days
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _buyer(make_user):
    return make_user(UserRole.AP_CLERK)


def _vendor(db, tenant_id, name=None):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id,
        legal_name=name or f"V-{uuid.uuid4().hex[:6]}", status=VendorStatus.ACTIVE,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _requisition(db, tenant_id, created_by, estimate):
    req = PurchaseRequisition(
        id=uuid.uuid4(), tenant_id=tenant_id,
        requisition_number=f"REQ-{uuid.uuid4().hex[:6]}",
        title="Laptops", justification="Team growth",
        requested_date=_now().date(),
        estimated_amount=Decimal(str(estimate)),
        current_state=RequisitionState.APPROVED.value,
        created_by=created_by, state_entered_at=_now(),
    )
    db.add(req)
    db.flush()
    return req


def _rfq(db, tenant_id, created_by, *, requisition=None, state=RFQState.ISSUED,
         closes_in_days=None, correlation_id=None, awarded_quote_id=None,
         estimate=10000):
    """An RFQ. `requisition_id` is NOT NULL on the table — every RFQ comes
    from a need somebody wrote down — so one is created when not supplied,
    which is also why the estimate baseline is always available."""
    requisition = requisition or _requisition(db, tenant_id, created_by, estimate)
    rfq = RFQ(
        id=uuid.uuid4(), tenant_id=tenant_id,
        rfq_number=f"RFQ-{uuid.uuid4().hex[:6]}", title="Laptops",
        requisition_id=requisition.id,
        current_state=state.value if hasattr(state, "value") else state,
        closes_at=(
            None if closes_in_days is None
            else _now() + timedelta(days=closes_in_days)
        ),
        correlation_id=correlation_id or uuid.uuid4(),
        awarded_quote_id=awarded_quote_id,
        created_by=created_by, state_entered_at=_now(),
    )
    db.add(rfq)
    db.flush()
    return rfq


def _quote(db, tenant_id, rfq, vendor, amount, *, captured_by,
           compliant=True, state=QuoteState.RECEIVED):
    quote = Quote(
        id=uuid.uuid4(), tenant_id=tenant_id, rfq_id=rfq.id,
        vendor_id=vendor.id, vendor_name=vendor.legal_name,
        total_amount=Decimal(str(amount)), is_compliant=compliant,
        current_state=state.value if hasattr(state, "value") else state,
        captured_by=captured_by,
    )
    db.add(quote)
    db.flush()
    return quote


def _event(db, tenant_id, user, object_type, object_id, action, at,
           correlation_id=None):
    """One point on the trail. Cycle times are measured from these rather than
    from the RFQ's own timestamp columns, so the tests write these."""
    db.add(AuditLog(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user["id"],
        user_email=user["email"], user_role=user["role"],
        object_type=object_type, object_id=object_id, action=action,
        timestamp=at, correlation_id=correlation_id, custom_metadata={},
    ))
    db.flush()


def _lifecycle(db, tenant, user, rfq, *, draft_days=1, window_days=7,
               decide_days=2, po_days=1):
    """A whole RFQ life, laid out backwards from now so every stage is inside
    the report window."""
    total = draft_days + window_days + decide_days + po_days
    start = _now() - timedelta(days=total)
    marks = [
        ("created", start),
        ("issued", start + timedelta(days=draft_days)),
        ("closed", start + timedelta(days=draft_days + window_days)),
        ("awarded",
         start + timedelta(days=draft_days + window_days + decide_days)),
    ]
    for action, at in marks:
        _event(db, tenant.id, user, "rfq", rfq.id, action, at)
    _event(
        db, tenant.id, user, "purchase_order", uuid.uuid4(),
        "created_from_award", start + timedelta(days=total),
        correlation_id=rfq.correlation_id,
    )


# --- Cycle time --------------------------------------------------------------

class TestTheStagesAreSeparate:
    """The reason this is four numbers and not one."""

    def test_a_slow_decision_is_visible_on_its_own(self, db, tenant, make_user):
        """Two days of drafting, a week of vendor window, then three weeks of
        nobody picking. Averaged into one figure that reads as a moderately
        slow cycle; split out, it reads as a decision nobody is making."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"])
        _lifecycle(db, tenant, user, rfq,
                   draft_days=2, window_days=7, decide_days=21, po_days=1)

        report = DashboardService(db).rfq_cycle_time(user)

        assert _stage(report, "draft_to_issued")["median_days"] == 2.0
        assert _stage(report, "issued_to_closed")["median_days"] == 7.0
        assert _stage(report, "closed_to_awarded")["median_days"] == 21.0
        assert _stage(report, "awarded_to_po")["median_days"] == 1.0

    def test_the_stage_we_do_not_control_is_labelled_as_such(
        self, db, tenant, make_user
    ):
        """issued_to_closed is a window somebody chose, not a delay. Reading it
        as a delay would push a buyer to shorten the time vendors get, which
        is the opposite of the improvement."""
        user = _buyer(make_user)
        report = DashboardService(db).rfq_cycle_time(user)

        window = _stage(report, "issued_to_closed")
        assert "choice, not a delay" in window["note"]
        assert all(
            "Ours." in s["note"] for s in report["stages"]
            if s["stage"] != "issued_to_closed"
        )

    def test_every_stage_carries_a_human_label(self, db, tenant, make_user):
        """The audit actions are internal vocabulary. An earlier version of
        the page rendered the stage as "awarded to created_from_award", which
        is a database action name in front of a procurement lead. The label is
        carried in the payload rather than derived on the client, and the raw
        actions stay alongside it so somebody reconciling against the trail can
        still see exactly what was measured."""
        user = _buyer(make_user)
        report = DashboardService(db).rfq_cycle_time(user)

        for stage in report["stages"]:
            assert stage["label"]
            assert "_" not in stage["label"], (
                f"{stage['stage']} shows the raw action name {stage['label']!r}"
            )
            # Still present, for reconciliation against the audit trail.
            assert stage["from"] and stage["to"]

    def test_the_po_stage_spans_two_object_types(self, db, tenant, make_user):
        """The award is on the RFQ and the purchase order is its own record,
        joined by correlation id. If that join breaks, the stage silently
        reports nothing rather than erroring."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"])
        _lifecycle(db, tenant, user, rfq, po_days=4)

        report = DashboardService(db).rfq_cycle_time(user)

        assert _stage(report, "awarded_to_po")["count"] == 1
        assert _stage(report, "awarded_to_po")["median_days"] == 4.0

    def test_an_incomplete_rfq_contributes_only_the_stages_it_reached(
        self, db, tenant, make_user
    ):
        """Still out for quotes. It has a draft_to_issued and nothing after,
        and counting it as a zero anywhere else would drag those medians
        toward a speed nobody achieved."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"])
        start = _now() - timedelta(days=5)
        _event(db, tenant.id, user, "rfq", rfq.id, "created", start)
        _event(db, tenant.id, user, "rfq", rfq.id, "issued",
               start + timedelta(days=3))

        report = DashboardService(db).rfq_cycle_time(user)

        assert _stage(report, "draft_to_issued")["count"] == 1
        assert _stage(report, "closed_to_awarded")["count"] == 0
        assert _stage(report, "closed_to_awarded")["median_days"] is None

    def test_it_reports_the_worst_as_well_as_the_median(
        self, db, tenant, make_user
    ):
        """A median says what usually happens. The worst case is what somebody
        complained about."""
        user = _buyer(make_user)
        for decide in (1, 2, 30):
            rfq = _rfq(db, tenant.id, user["id"])
            _lifecycle(db, tenant, user, rfq, decide_days=decide)

        report = DashboardService(db).rfq_cycle_time(user)

        assert _stage(report, "closed_to_awarded")["median_days"] == 2.0
        assert _stage(report, "closed_to_awarded")["worst_days"] == 30.0


class TestTheMedianHelper:
    def test_it_takes_the_middle_of_an_odd_list(self):
        assert _median_days([24.0, 48.0, 240.0]) == 2.0

    def test_it_averages_the_middle_pair_of_an_even_list(self):
        assert _median_days([24.0, 48.0, 72.0, 96.0]) == 2.5

    def test_it_is_not_dragged_by_one_outlier(self):
        """The reason it is a median. One RFQ that sat over a holiday would
        put a mean somewhere nobody recognises."""
        assert _median_days([24.0, 24.0, 24.0, 24.0, 8760.0]) == 1.0

    def test_nothing_measured_is_none_rather_than_zero(self):
        """Zero would read as instantaneous."""
        assert _median_days([]) is None


# --- Competition -------------------------------------------------------------

class TestWhetherAnybodyCompeted:
    def test_it_reports_the_response_rate(self, db, tenant, make_user):
        """Invited and answered are different numbers, and the gap is the one
        a procurement lead can act on."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"])
        vendors = [_vendor(db, tenant.id) for _ in range(4)]
        for vendor in vendors:
            db.add(RFQVendor(
                id=uuid.uuid4(), tenant_id=tenant.id, rfq_id=rfq.id,
                vendor_id=vendor.id, vendor_name=vendor.legal_name,
            ))
        db.flush()
        for vendor in vendors[:2]:
            _quote(db, tenant.id, rfq, vendor, 1000, captured_by=user["id"])

        competition = DashboardService(db).rfq_cycle_time(user)["competition"]

        assert competition["invited"] == 4
        assert competition["quoted"] == 2
        assert competition["response_rate_pct"] == 50.0

    def test_an_award_on_one_quote_is_counted(self, db, tenant, make_user):
        """Not necessarily wrong — sole supply is real — but it is the case
        somebody should be able to point at, and a cycle-time average never
        surfaces it."""
        user = _buyer(make_user)
        vendor = _vendor(db, tenant.id)
        rfq = _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED)
        quote = _quote(db, tenant.id, rfq, vendor, 1000, captured_by=user["id"])
        rfq.awarded_quote_id = quote.id
        db.flush()

        competition = DashboardService(db).rfq_cycle_time(user)["competition"]

        assert competition["single_quote_awards"] == 1
        assert competition["awarded_with_competition"] == 0

    def test_a_contested_award_is_counted_the_other_way(
        self, db, tenant, make_user
    ):
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED)
        winner = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 900,
                        captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 1100,
               captured_by=user["id"])
        rfq.awarded_quote_id = winner.id
        db.flush()

        competition = DashboardService(db).rfq_cycle_time(user)["competition"]

        assert competition["single_quote_awards"] == 0
        assert competition["awarded_with_competition"] == 1


# --- Savings -----------------------------------------------------------------

class TestSavingsAgainstTheDeclaredEstimate:
    def test_it_measures_against_what_was_committed_before_quoting(
        self, db, tenant, make_user
    ):
        """The requisition estimate is written before any vendor quotes, so it
        cannot be adjusted afterwards to flatter the saving."""
        user = _buyer(make_user)
        req = _requisition(db, tenant.id, user["id"], 10000)
        rfq = _rfq(db, tenant.id, user["id"], requisition=req,
                   state=RFQState.AWARDED)
        winner = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 8000,
                        captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 9500,
               captured_by=user["id"])
        rfq.awarded_quote_id = winner.id
        db.flush()

        savings = DashboardService(db).rfq_cycle_time(user)["savings"]

        assert savings["awarded_value"] == 8000.0
        assert savings["vs_estimate"] == 2000.0
        assert savings["vs_estimate_pct"] == 20.0

    def test_overspending_the_estimate_shows_as_a_negative(
        self, db, tenant, make_user
    ):
        """A report that could only show savings would be an advertisement."""
        user = _buyer(make_user)
        req = _requisition(db, tenant.id, user["id"], 5000)
        rfq = _rfq(db, tenant.id, user["id"], requisition=req,
                   state=RFQState.AWARDED)
        winner = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 6000,
                        captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 7000,
               captured_by=user["id"])
        rfq.awarded_quote_id = winner.id
        db.flush()

        savings = DashboardService(db).rfq_cycle_time(user)["savings"]

        assert savings["vs_estimate"] == -1000.0
        assert savings["vs_estimate_pct"] == -20.0


class TestSavingsNeedAnAlternative:
    def test_a_single_quote_award_is_excluded_and_counted(
        self, db, tenant, make_user
    ):
        """Nothing was saved against — it just happened. Folding it in at zero
        would dilute the rate with awards that never had a comparison."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED)
        quote = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 1000,
                       captured_by=user["id"])
        rfq.awarded_quote_id = quote.id
        db.flush()

        savings = DashboardService(db).rfq_cycle_time(user)["savings"]

        assert savings["awards_with_no_comparison"] == 1
        assert savings["vs_highest_quote"] is None

    def test_it_measures_against_the_worst_alternative_on_the_table(
        self, db, tenant, make_user
    ):
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED)
        winner = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 700,
                        captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 1000,
               captured_by=user["id"])
        rfq.awarded_quote_id = winner.id
        db.flush()

        savings = DashboardService(db).rfq_cycle_time(user)["savings"]

        assert savings["vs_highest_quote"] == 300.0
        assert savings["vs_highest_quote_pct"] == 30.0
        assert savings["awards_with_no_comparison"] == 0

    def test_a_non_compliant_quote_is_not_an_alternative(
        self, db, tenant, make_user
    ):
        """Nobody could have bought it, so measuring a saving against it would
        be measuring against something that was never available — the easiest
        way to manufacture a savings figure."""
        user = _buyer(make_user)
        rfq = _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED)
        winner = _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 700,
                        captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 900,
               captured_by=user["id"])
        _quote(db, tenant.id, rfq, _vendor(db, tenant.id), 99999,
               captured_by=user["id"], compliant=False)
        rfq.awarded_quote_id = winner.id
        db.flush()

        savings = DashboardService(db).rfq_cycle_time(user)["savings"]

        # Against 900, the highest compliant quote — not the 99999 nobody
        # could have accepted.
        assert savings["vs_highest_quote"] == 200.0


# --- Overdue -----------------------------------------------------------------

class TestRfqsPastTheirCloseDate:
    def test_an_issued_rfq_past_its_close_date_is_listed(
        self, db, tenant, make_user
    ):
        """Nothing errors when this happens. The RFQ stays open, the
        requisition behind it stays unmet, and only a report shows it."""
        user = _buyer(make_user)
        _rfq(db, tenant.id, user["id"], state=RFQState.ISSUED,
             closes_in_days=-9)

        overdue = DashboardService(db).rfq_cycle_time(user)["overdue_open"]

        assert len(overdue) == 1
        assert overdue[0]["closed_days_ago"] == pytest.approx(9.0, abs=0.1)

    def test_an_rfq_still_inside_its_window_is_not(self, db, tenant, make_user):
        user = _buyer(make_user)
        _rfq(db, tenant.id, user["id"], state=RFQState.ISSUED, closes_in_days=3)

        assert DashboardService(db).rfq_cycle_time(user)["overdue_open"] == []

    def test_an_awarded_rfq_is_not_overdue_however_old(
        self, db, tenant, make_user
    ):
        """It closed by being decided. Listing it would make a finished job
        look like a stuck one."""
        user = _buyer(make_user)
        _rfq(db, tenant.id, user["id"], state=RFQState.AWARDED,
             closes_in_days=-40)

        assert DashboardService(db).rfq_cycle_time(user)["overdue_open"] == []


class TestPermissions:
    def test_it_reads_with_the_requisition_gate_not_sourcing_manage(
        self, db, tenant, make_user
    ):
        """A CFO and an auditor hold requisitions.view and not
        sourcing.manage. Gating on the permission to *run* an RFQ would shut
        out the readers this report exists for."""
        for role in (UserRole.CFO, UserRole.AUDITOR, UserRole.MANAGER):
            user = make_user(role)
            assert DashboardService(db).rfq_cycle_time(user) is not None

    def test_the_api_serves_a_procurement_reader(
        self, client, as_user, make_user
    ):
        as_user(make_user(UserRole.CFO))

        assert client.get("/api/v1/dashboard/rfq-cycle-time").status_code == 200


def _stage(report, name):
    return next(s for s in report["stages"] if s["stage"] == name)
