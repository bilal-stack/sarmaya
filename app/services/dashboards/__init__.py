"""Reporting and metrics: the seven dashboards the Build Book names.

Build Book, Global Dashboards (lines 265-272). Each one answers a question
somebody actually asks, and each is computed from history the system already
keeps rather than from a new counter written at the time — because a counter
can drift from the events it counts, and the audit trail cannot.

  * **Executive Control Room** — what is stuck, why, and what it is worth.
  * **Approval Bottlenecks** — cycle time by step and by role.
  * **Exceptions Heatmap** — what is being blocked, and by which vendor.
  * **Policy Overrides** — who overrode a control, how often, for how much.
  * **Evidence Completeness** — what would fail an audit right now.
  * **Reconciliation Health** — money that left with nothing explaining it.
  * **Autopilot Health** — what the machine decided, and what was reverted.

Every figure is tenant-scoped by the session, like every other query here.

**On caching: none of these are cached, and that is a measured decision.**

Timed against 20,000 invoices and 60,000 audit entries — roughly a year of
real volume for the size of business this targets. The whole page took 885ms,
of which 544ms was one sequential scan: the audit trail had no index that
could serve "every entry with action X in the last N days", only one for
reading a single object's history. Migration 035 adds it, and the cycle-time
aggregation moved into Postgres rather than pulling one row per invoice into
Python to reduce. The page is now ~350ms at that volume, and the slowest panel
188ms.

A cache would have hidden the scan instead of fixing it, and bought a page
that is occasionally wrong about how much money is stuck — which is the one
thing this page must never be.

If it does need caching later, the split is already visible in the code and
should be respected: "what is stuck right now" must stay live and is cheap
because the set is small, while "what happened over ninety days" is expensive
but historical — yesterday's cycle times never change. That argues for a
materialised view refreshed on a schedule, not a blanket TTL over both.

**Why this is a package.** It was one module until it reached 2,588 lines
across seven personas, at which point the file was the thing standing between
a reader and the twenty lines they came for. The split is by *reader*, not by
mechanism: a CFO's two reports live together because they are read together,
and the shared arithmetic lives in _shared.py because putting it anywhere else
would make one persona import another for no reason.

`DashboardService` is unchanged as far as every caller is concerned — same
name, same methods, same import path. It is now composed from one mixin per
persona rather than being a single 25-method class.
"""
from app.services.dashboards._shared import (
    AGE_BUCKETS, BLOCK_REASONS, DashboardBase, _bucket, _median_days, _now,
    _reason_from_comment,
)
from app.services.dashboards.ap_treasury import ApTreasuryReports
from app.services.dashboards.cfo import CfoReports
from app.services.dashboards.executive import ExecutiveReports
from app.services.dashboards.health import HealthReports
from app.services.dashboards.hr import HrReports
from app.services.dashboards.procurement import ProcurementReports
from app.services.dashboards.supply_chain import SupplyChainReports


class DashboardService(
    ExecutiveReports,
    ApTreasuryReports,
    HealthReports,
    SupplyChainReports,
    HrReports,
    CfoReports,
    ProcurementReports,
):
    """Every report, on one object, exactly as before.

    The mixins are independent — none calls into another's reports — so the
    order here is presentational rather than load-bearing: it is the order the
    personas appear in the Build Book.
    """


#: Re-exported because tests and callers import them from here. Keeping the
#: names available at this path is what makes the split invisible to everything
#: outside this package.
__all__ = [
    "DashboardService", "DashboardBase", "AGE_BUCKETS", "BLOCK_REASONS",
    "_bucket", "_median_days", "_now", "_reason_from_comment",
]
