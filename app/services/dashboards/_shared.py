"""What every persona report needs: the constants, the helpers, and the base.

Split out of the single dashboards.py when that file reached 2,588 lines.
The class here carries only the two things every report uses — the session
and the default permission gate — so that a persona module can be read
without the other six being in the way.
"""
import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.roles import has_permission, PERM_VIEW_INVOICE
from app.utils.datetime_helpers import utc_now, to_utc, make_naive


logger = logging.getLogger(__name__)

#: Aging buckets, in days. The same ladder everywhere so two dashboards never
#: disagree about what "old" means.
AGE_BUCKETS = [(0, 2, "0-2 days"), (2, 7, "3-7 days"),
               (7, 30, "8-30 days"), (30, None, "over 30 days")]


#: Why a governance refusal fired, and whether it was a segregation-of-duties
#: failure specifically. The audit action says *where* something was refused
#: (approval_blocked, release_blocked, ...); the reason says *why*, and only
#: some of those are SoD. An unrecognised reason is reported as itself rather
#: than dropped or guessed at — a refusal nobody has classified yet is still a
#: refusal somebody should see.
BLOCK_REASONS = {
    "sod_self_approval": {
        "label": "Tried to approve their own record", "is_sod": True,
    },
    "self_approval": {
        "label": "Tried to approve their own record", "is_sod": True,
    },
    "self_release": {
        "label": "Tried to release a payment run they prepared", "is_sod": True,
    },
    "self_reconciliation": {
        "label": "Tried to reconcile a payment they released", "is_sod": True,
    },
    "sod_self_activation": {
        "label": "Tried to activate a vendor they created", "is_sod": True,
    },
    "first_payment_after_bank_change": {
        # DR-032's other half: the second signature on a bank change means
        # nothing if the same person then releases the first payment to it.
        "label": "Tried to pay a vendor whose bank details they changed",
        "is_sod": True,
    },
    "over_approval_limit": {
        # Authority, not separation — one person acting beyond their own
        # ceiling rather than two roles collapsing into one.
        "label": "Acted beyond their approval limit", "is_sod": False,
    },
    "no_vendor_link": {
        "label": "Record not linked to a vendor", "is_sod": False,
    },
    "vendor_missing": {
        "label": "Linked vendor no longer exists", "is_sod": False,
    },
}


def _reason_from_comment(comment) -> str:
    """Older entries carry the reason only in "Blocked: <reason>"."""
    if comment and comment.startswith("Blocked: "):
        return comment[len("Blocked: "):].strip()
    return (comment or "unspecified").strip()


def _now():
    return make_naive(to_utc(utc_now()))


def _median_days(hours: List[float]) -> Optional[float]:
    """Median of a list of durations, in days.

    Python rather than percentile_cont, unlike the other cycle-time reports:
    these durations span two object types (an RFQ and the purchase order
    raised from it) and are assembled in application code, so there is no one
    result set to take a percentile over.

    Median rather than mean, for the reason approval_bottlenecks already
    states: one RFQ that sat over a holiday drags an average somewhere nobody
    recognises.
    """
    if not hours:
        return None
    ordered = sorted(hours)
    mid = len(ordered) // 2
    middle = (
        ordered[mid] if len(ordered) % 2
        else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    return round(middle / 24.0, 1)


def _bucket(days: float) -> str:
    for low, high, label in AGE_BUCKETS:
        if days >= low and (high is None or days < high):
            return label
    return AGE_BUCKETS[-1][2]


class DashboardBase:
    """Session and the default gate. Composed into DashboardService."""

    def __init__(self, db: Session):
        self.db = db

    def _require(self, current_user: dict) -> None:
        # Reading aggregates is reading the underlying records, so the gate is
        # the same one. Nothing here exposes a figure a viewer could not reach
        # by opening the records themselves.
        if not has_permission(current_user["role"], PERM_VIEW_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view dashboards"
            )

