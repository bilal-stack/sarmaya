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
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.core.enums import (
    InvoiceState, PaymentState, RFQState, VendorStatus,
)
from app.core.roles import has_permission, PERM_VIEW_INVOICE
from app.models.ai_action_log import AIActionLog
from app.models.audit_log import AuditLog
from app.models.bank_statement import BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentLine
from app.models.vendor import Vendor
from app.models.rfq import RFQ, RFQVendor, Quote
from app.models.requisition import PurchaseRequisition
from app.models.inventory import (
    MOVE_ISSUE, Item, StockBalance, StockMovement,
)
from app.models.watchlist_alert import WatchlistAlert
from app.utils.datetime_helpers import utc_now, to_utc, make_naive
from app.utils.money import money_to_float

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


class DashboardService:
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

    # --- 1. Executive Control Room ------------------------------------------

    def control_room(self, current_user: dict) -> Dict:
        """What is stuck, why it is stuck, and the cash behind it.

        The one dashboard to read if you read one. Every row is a reason
        something is not moving, with what it is worth — because "48 items
        pending" and "4.2M pending" prompt different conversations, and only
        the second one gets a meeting.
        """
        self._require(current_user)
        now = _now()
        blocked: List[Dict] = []

        # Waiting on a human decision.
        pending = (
            self.db.query(
                func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0),
                func.min(Invoice.state_entered_at),
            )
            .filter(Invoice.current_state == InvoiceState.PENDING_APPROVAL.value)
            .one()
        )
        if pending[0]:
            blocked.append(self._stuck_row(
                "Awaiting approval", pending, now,
                "Somebody has to decide. Nothing else is wrong with these.",
                "/ai-tools/inbox",
            ))

        # Held by the vendor gate — a control working, but still money stopped.
        vendor_blocked = (
            self.db.query(
                func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0),
                func.min(Invoice.state_entered_at),
            )
            .join(Vendor, Vendor.id == Invoice.vendor_id)
            .filter(
                Invoice.current_state.in_([
                    InvoiceState.VALIDATED.value, InvoiceState.PENDING_APPROVAL.value,
                ]),
                Vendor.status != VendorStatus.ACTIVE,
            )
            .one()
        )
        if vendor_blocked[0]:
            blocked.append(self._stuck_row(
                "Vendor not verified", vendor_blocked, now,
                "The control is working; the vendor needs verifying before any "
                "of this can be approved or paid.",
                "/ai-tools/vendors",
            ))

        # Flagged as duplicates and not yet resolved either way.
        duplicates = (
            self.db.query(
                func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0),
                func.min(Invoice.state_entered_at),
            )
            .filter(
                Invoice.potential_duplicate_id.isnot(None),
                Invoice.duplicate_acknowledged.is_(False),
                Invoice.current_state.notin_([
                    InvoiceState.PAID.value, InvoiceState.REJECTED.value,
                    InvoiceState.CANCELLED.value,
                ]),
            )
            .one()
        )
        if duplicates[0]:
            blocked.append(self._stuck_row(
                "Possible duplicate", duplicates, now,
                "Pay these before clearing the flag and you pay twice.",
                "/ai-tools/inbox",
            ))

        # Approved, unpaid, not yet on a run: the money is committed and idle.
        awaiting_payment = (
            self.db.query(
                func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0),
                func.min(Invoice.state_entered_at),
            )
            .filter(Invoice.current_state == InvoiceState.APPROVED.value)
            .one()
        )
        if awaiting_payment[0]:
            blocked.append(self._stuck_row(
                "Approved, not yet paid", awaiting_payment, now,
                "Cleared to pay. Whether that is a problem depends on the due "
                "dates, not on this number alone.",
                "/ai-tools/payments",
            ))

        # Runs waiting on a second signature.
        runs = (
            self.db.query(
                func.count(Payment.id), func.coalesce(func.sum(Payment.total_amount), 0),
                func.min(Payment.state_entered_at),
            )
            .filter(Payment.current_state == PaymentState.PENDING_RELEASE.value)
            .one()
        )
        if runs[0]:
            blocked.append(self._stuck_row(
                "Payment runs awaiting release", runs, now,
                "Prepared and waiting for a second person. The last gate before "
                "money leaves.",
                "/ai-tools/payments",
            ))

        blocked.sort(key=lambda r: -r["amount"])
        return {
            "total_amount_stuck": round(sum(r["amount"] for r in blocked), 2),
            "total_items_stuck": sum(r["count"] for r in blocked),
            "blocked": blocked,
            "paid_last_30_days": self._paid_recently(30),
        }

    def _stuck_row(self, label, row, now, note, link) -> Dict:
        count, amount, oldest = row
        age_days = (now - oldest).total_seconds() / 86400 if oldest else 0
        return {
            "reason": label,
            "count": int(count or 0),
            "amount": money_to_float(amount or 0),
            "oldest_days": round(age_days, 1),
            "note": note,
            "link": link,
        }

    def _paid_recently(self, days: int) -> Dict:
        since = _now() - timedelta(days=days)
        row = (
            self.db.query(
                func.count(Payment.id), func.coalesce(func.sum(Payment.total_amount), 0)
            )
            .filter(
                Payment.current_state == PaymentState.RELEASED.value,
                Payment.released_at >= since,
            )
            .one()
        )
        return {"runs": int(row[0] or 0), "amount": money_to_float(row[1] or 0)}

    # --- 2. Approval Bottlenecks --------------------------------------------

    def approval_bottlenecks(self, current_user: dict, days: int = 90) -> Dict:
        """How long each step actually takes, and who it waits on.

        Measured from the audit trail rather than from a stored duration: the
        trail is what happened, and a duration column would only be as right as
        the code that last wrote it.

        Reported as median as well as mean, because one invoice that sat for
        three weeks drags an average somewhere no real invoice ever was.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        submitted = (
            self.db.query(
                AuditLog.object_id.label("object_id"),
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action == "submitted_for_approval",
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.object_id)
            .subquery()
        )
        decided = (
            self.db.query(
                AuditLog.object_id.label("object_id"),
                func.min(AuditLog.timestamp).label("at"),
                func.min(AuditLog.user_role).label("role"),
                func.min(AuditLog.action).label("action"),
            )
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action.in_(["approved", "rejected"]),
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.object_id)
            .subquery()
        )

        # Aggregated in Postgres, not in Python. Returning every decision in
        # the window and reducing it here costs one row per invoice over the
        # wire — fine at a hundred, measurably slow at twenty thousand, and the
        # transfer is the part that grows. percentile_cont gives the median
        # directly, which is the figure that actually matters: one invoice that
        # sat for three weeks drags a mean somewhere no real invoice ever was.
        hours = func.extract("epoch", decided.c.at - submitted.c.at) / 3600.0
        rows = (
            self.db.query(
                decided.c.role,
                func.count().label("decisions"),
                func.percentile_cont(0.5).within_group(hours).label("median"),
                func.avg(hours).label("average"),
                func.max(hours).label("slowest"),
            )
            .join(submitted, submitted.c.object_id == decided.c.object_id)
            .filter(decided.c.at >= submitted.c.at)
            .group_by(decided.c.role)
            .order_by(func.count().desc())
            .all()
        )

        steps = [
            {
                "step": "approval",
                "role": role or "unknown",
                "decisions": int(decisions),
                "median_hours": round(float(median or 0), 1),
                "average_hours": round(float(average or 0), 1),
                "slowest_hours": round(float(slowest or 0), 1),
            }
            for role, decisions, median, average, slowest in rows
        ]

        return {
            "window_days": days,
            "by_role": steps,
            "still_waiting": self._waiting_distribution(),
        }

    def _waiting_distribution(self) -> List[Dict]:
        """How long the *undecided* ones have been sitting.

        The completed ones tell you how fast you were; these tell you what is
        happening now, and only the second can still be changed.
        """
        now = _now()
        rows = (
            self.db.query(Invoice.state_entered_at, Invoice.total_amount)
            .filter(Invoice.current_state == InvoiceState.PENDING_APPROVAL.value)
            .all()
        )
        buckets: Dict[str, Dict] = {
            label: {"bucket": label, "count": 0, "amount": 0.0}
            for _, _, label in AGE_BUCKETS
        }
        for entered, amount in rows:
            if entered is None:
                continue
            label = _bucket((now - entered).total_seconds() / 86400)
            buckets[label]["count"] += 1
            buckets[label]["amount"] += money_to_float(amount or 0)
        for bucket in buckets.values():
            bucket["amount"] = round(bucket["amount"], 2)
        return list(buckets.values())

    # --- 3. Exceptions Heatmap ----------------------------------------------

    def exceptions_heatmap(self, current_user: dict, days: int = 90) -> Dict:
        """What is being refused, and by whom it is being caused.

        Every blocked action in this system writes an audit entry naming its
        reason, precisely so this question can be answered without anybody
        having instrumented it in advance.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        rows = (
            self.db.query(AuditLog.action, AuditLog.comment, AuditLog.after_value)
            .filter(
                AuditLog.timestamp >= since,
                AuditLog.action.like("%blocked%"),
            )
            .all()
        )

        by_reason: Dict[str, int] = {}
        by_vendor: Dict[str, int] = {}
        for action, comment, after in rows:
            payload = after or {}
            reason = payload.get("reason") or (comment or action)
            by_reason[reason] = by_reason.get(reason, 0) + 1
            vendor = payload.get("vendor_name")
            if vendor:
                by_vendor[vendor] = by_vendor.get(vendor, 0) + 1

        return {
            "window_days": days,
            "total": len(rows),
            "by_reason": [
                {"reason": k, "count": v}
                for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])
            ],
            "by_vendor": [
                {"vendor": k, "count": v}
                for k, v in sorted(by_vendor.items(), key=lambda kv: -kv[1])[:10]
            ],
        }

    # --- 4. Policy Overrides -------------------------------------------------

    def policy_overrides(self, current_user: dict, days: int = 90) -> Dict:
        """Who set a control aside, how often, and for how much.

        Not a list of wrongdoing. Overrides are legitimate and the system asks
        for a reason every time; the point of counting them is that a rising
        rate, or one person holding most of them, is worth a conversation
        nobody would otherwise think to have.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        overrides = (
            self.db.query(
                AuditLog.action, AuditLog.user_email, AuditLog.comment,
                AuditLog.object_id, AuditLog.timestamp,
            )
            .filter(
                AuditLog.timestamp >= since,
                AuditLog.action.in_([
                    "duplicate_acknowledged",   # paid anyway, with a reason
                    "bank_change_approved",     # a payment destination moved
                    "mfa_reset",                # a second factor cleared
                    "awarded",                  # possibly not the cheapest quote
                ]),
            )
            .order_by(AuditLog.timestamp.desc())
            .all()
        )

        amounts = dict(
            self.db.query(Invoice.id, Invoice.total_amount)
            .filter(Invoice.id.in_([o.object_id for o in overrides] or [None]))
            .all()
        )

        by_person: Dict[str, Dict] = {}
        items = []
        for action, email, comment, object_id, at in overrides:
            amount = money_to_float(amounts.get(object_id) or 0)
            person = by_person.setdefault(
                email or "unknown", {"who": email or "unknown", "count": 0, "amount": 0.0}
            )
            person["count"] += 1
            person["amount"] += amount
            items.append({
                "action": action, "who": email, "reason": comment,
                "amount": amount, "at": at.isoformat() if at else None,
            })

        for person in by_person.values():
            person["amount"] = round(person["amount"], 2)

        return {
            "window_days": days,
            "total": len(items),
            "by_person": sorted(by_person.values(), key=lambda p: -p["count"]),
            "recent": items[:25],
        }

    # --- AP / Treasury -------------------------------------------------------

    def invoice_throughput(self, current_user: dict, days: int = 90) -> Dict:
        """Capture to paid, and what sends work backwards.

        Build Book, AP/Treasury: "Invoice throughput: capture to post, match
        rate, exception rate, and rework drivers."

        Cycle time is measured from the audit trail rather than from stored
        timestamps, the same choice approval_bottlenecks makes and for the same
        reason: the trail is what happened, and a duration column is only ever
        as right as the code that last wrote it.

        Rework is the half of throughput nobody counts. An invoice that goes
        out for approval, comes back rejected, is corrected and goes out again
        has consumed three touches and shows up in a state count as one
        approval — so the drivers are reported by reason, which is the only
        form of this figure anybody can act on.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        created = (
            self.db.query(
                AuditLog.object_id.label("object_id"),
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action.in_(["created", "uploaded"]),
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.object_id)
            .subquery()
        )
        settled = (
            self.db.query(
                AuditLog.object_id.label("object_id"),
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action == "marked_paid",
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.object_id)
            .subquery()
        )

        hours = func.extract("epoch", settled.c.at - created.c.at) / 3600.0
        row = (
            self.db.query(
                func.count().label("n"),
                func.avg(hours).label("mean"),
                func.percentile_cont(0.5)
                .within_group(hours)
                .label("median"),
            )
            .select_from(created)
            .join(settled, settled.c.object_id == created.c.object_id)
            .one()
        )

        # Rework: every time an invoice was sent back rather than forward.
        rework = (
            self.db.query(
                AuditLog.object_id, AuditLog.comment, AuditLog.after_value,
            )
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action.in_(["rejected", "approval_blocked"]),
                AuditLog.timestamp >= since,
            )
            .all()
        )
        drivers: Dict[str, int] = {}
        reworked_invoices = set()
        for object_id, comment, after in rework:
            reason = (after or {}).get("reason") or comment or "no reason given"
            drivers[reason[:120]] = drivers.get(reason[:120], 0) + 1
            reworked_invoices.add(object_id)

        captured = self.db.query(func.count()).select_from(created).scalar() or 0

        return {
            "window_days": days,
            "captured": captured,
            "settled": int(row.n or 0),
            "capture_to_paid_hours": {
                "mean": round(float(row.mean), 1) if row.mean is not None else None,
                # Median as well as mean: one invoice that sat for three weeks
                # drags an average somewhere no real invoice ever was.
                "median": round(float(row.median), 1) if row.median is not None else None,
            },
            # Two separate figures, because one invoice rejected three times is
            # three events and one affected invoice. The rate is over affected
            # invoices, so it stays a percentage somebody can read — counting
            # events against invoices produced 200%, which is arithmetically
            # true and not a rate of anything.
            "rework_events": len(rework),
            "invoices_reworked": len(reworked_invoices),
            "rework_rate_pct": (
                round(len(reworked_invoices) * 100.0 / captured, 1)
                if captured else 0.0
            ),
            "rework_drivers": sorted(
                ({"reason": k, "count": v} for k, v in drivers.items()),
                key=lambda d: -d["count"],
            )[:10],
            #: Not reported: match rate. Three-way match is computed on demand
            #: by three_way_match.py and never stored, so there is no record of
            #: what an invoice matched at the time it was approved. Reporting a
            #: rate recomputed today against goods receipts that have since
            #: changed would be a different number wearing the same name.
            "match_rate_pct": None,
        }

    def payment_run_status(self, current_user: dict, days: int = 90) -> Dict:
        """Where every payment run is, and what is stuck behind it.

        Build Book, AP/Treasury: "Payment run status: proposed, approved,
        executed, failed, reissued, and reasons."

        Two of those five have no equivalent here and are not invented.
        Sarmaya never moves money — it produces an instruction a treasury user
        uploads to their own bank — so it cannot know that a transfer failed
        or was reissued, and a column reporting zero failures would be read as
        "none failed" rather than "we cannot see". What it can see is the
        nearest honest thing: a run released with no bank file generated, and
        a run whose money never appeared on a statement.

        Gated on payments.view, not the dashboard permission: a manager can
        open every invoice this touches and still cannot see a payment run.
        """
        self._require_payments(current_user)
        since = _now() - timedelta(days=days)

        by_state = (
            self.db.query(
                Payment.current_state,
                func.count().label("n"),
                func.coalesce(func.sum(Payment.total_amount), 0).label("value"),
            )
            .filter(Payment.created_at >= since)
            .group_by(Payment.current_state)
            .all()
        )

        released = (
            self.db.query(Payment)
            .filter(
                Payment.current_state == PaymentState.RELEASED,
                Payment.created_at >= since,
            )
            .all()
        )

        matched_ids = {
            row[0] for row in self.db.query(BankStatementLine.matched_payment_id)
            .filter(BankStatementLine.matched_payment_id.isnot(None))
            .all()
        }

        awaiting_file, unreconciled = [], []
        for payment in released:
            if payment.bank_file_generated_at is None:
                awaiting_file.append(payment)
            elif payment.id not in matched_ids:
                unreconciled.append(payment)

        rejected = (
            self.db.query(Payment)
            .filter(
                Payment.current_state == PaymentState.REJECTED,
                Payment.created_at >= since,
            )
            .all()
        )

        return {
            "window_days": days,
            "by_state": [
                {
                    "state": str(getattr(state, "value", state)),
                    "count": n,
                    "value": money_to_float(value),
                }
                for state, n, value in by_state
            ],
            # Released, but no instruction has been produced for the bank —
            # the run is authorised and nothing has been handed over.
            "awaiting_bank_file": [
                {
                    "payment_number": p.payment_number,
                    "value": money_to_float(p.total_amount),
                    "released_at": p.released_at.isoformat() if p.released_at else None,
                }
                for p in awaiting_file
            ],
            # The file went to the bank and the money never appeared on a
            # statement. The closest this system can get to "failed", and
            # named for what it actually observed rather than what it infers.
            "unreconciled_after_release": [
                {
                    "payment_number": p.payment_number,
                    "value": money_to_float(p.total_amount),
                    "released_at": p.released_at.isoformat() if p.released_at else None,
                    "age_days": (
                        round((_now() - p.released_at).total_seconds() / 86400.0, 1)
                        if p.released_at else None
                    ),
                }
                for p in sorted(
                    unreconciled,
                    key=lambda p: p.released_at or _now(),
                )
            ],
            "rejected": [
                {
                    "payment_number": p.payment_number,
                    "value": money_to_float(p.total_amount),
                    "reason": p.rejection_reason,
                }
                for p in rejected
            ],
            "not_reported": {
                "failed": "Sarmaya does not move money, so a bank-side failure "
                          "is not observable here. See unreconciled_after_release.",
                "reissued": "No reissue concept exists; a replacement run is a "
                            "new run with no link to the original.",
            },
        }

    def duplicate_and_anomaly(self, current_user: dict, days: int = 90) -> Dict:
        """Duplicates caught, what happened to them, and the watchlist.

        Build Book, AP/Treasury: "Duplicate / anomaly dashboard: duplicates
        caught, prevented losses, and watchlist hits."

        "Prevented" is stated carefully, because it is the number most easily
        overclaimed. It counts invoices flagged as duplicates that were *not*
        subsequently paid — cancelled, rejected, or still held. It does not
        claim every one of those would have been paid twice: some were
        legitimate re-issues somebody chose not to pursue. What it does claim
        is the amount the flag actually held back, which is the honest version
        of the figure and the only one that survives being asked about.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        flagged = (
            self.db.query(Invoice)
            .filter(
                Invoice.potential_duplicate_id.isnot(None),
                Invoice.created_at >= since,
            )
            .all()
        )

        paid_anyway = [
            i for i in flagged
            if str(getattr(i.current_state, "value", i.current_state)) == InvoiceState.PAID.value
        ]
        stopped = [
            i for i in flagged
            if str(getattr(i.current_state, "value", i.current_state))
            in (InvoiceState.CANCELLED.value, InvoiceState.REJECTED.value)
        ]
        still_open = [
            i for i in flagged if i not in paid_anyway and i not in stopped
        ]

        alerts = (
            self.db.query(
                WatchlistAlert.category, WatchlistAlert.severity,
                func.count().label("n"),
                func.count(WatchlistAlert.acknowledged_at).label("acknowledged"),
            )
            .filter(WatchlistAlert.created_at >= since)
            .group_by(WatchlistAlert.category, WatchlistAlert.severity)
            .all()
        )

        return {
            "window_days": days,
            "flagged": len(flagged),
            "paid_anyway": len(paid_anyway),
            "still_held": len(still_open),
            "stopped": len(stopped),
            # Held back by the flag, not "losses prevented" — see the docstring.
            "value_held_back": round(
                sum(money_to_float(i.total_amount) for i in stopped + still_open), 2
            ),
            "value_paid_anyway": round(
                sum(money_to_float(i.total_amount) for i in paid_anyway), 2
            ),
            "watchlist": [
                {
                    "category": category,
                    "severity": severity,
                    "count": n,
                    "acknowledged": acknowledged,
                    "open": n - acknowledged,
                }
                for category, severity, n, acknowledged in alerts
            ],
        }

    def _require_payments(self, current_user: dict) -> None:
        """Payment reports read with payments.view.

        Same principle _require states — reading an aggregate is reading the
        records under it — applied to records a manager and an approver
        cannot open. Using the dashboard gate here would let both read run
        values and bank-file state they are refused on the payment itself.
        """
        from app.core.roles import PERM_VIEW_PAYMENT

        if not has_permission(current_user["role"], PERM_VIEW_PAYMENT):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view payment reports"
            )

    # --- Segregation of duties: what was refused -----------------------------

    def sod_violations(self, current_user: dict, days: int = 90) -> Dict:
        """Attempts the controls refused, and who made them.

        Build Book, Audit/Compliance: "SoD violations blocked and attempted
        actions (security posture)."

        Every other report in this file counts things that happened. This one
        counts things that were stopped, which is the only report here whose
        empty state is genuinely good news — and the reason it is worth having
        at all. A control that has never fired is indistinguishable, from the
        outside, from a control that is not wired up; this is the difference,
        and it is the single most direct answer to an auditor asking whether
        segregation of duties is enforced rather than merely documented.

        Read with audit.view rather than the dashboard gate. The other
        dashboards aggregate records anybody with invoices.view could open
        individually; this names a person and an action they were refused,
        which is a different kind of fact about a colleague.
        """
        self._require_audit(current_user)
        since = _now() - timedelta(days=days)

        blocks = (
            self.db.query(
                AuditLog.action, AuditLog.user_email, AuditLog.comment,
                AuditLog.object_type, AuditLog.object_id, AuditLog.after_value,
                AuditLog.timestamp,
            )
            .filter(
                AuditLog.timestamp >= since,
                # Every governance refusal in this codebase writes an action
                # ending "_blocked" — approval_blocked, release_blocked,
                # reconciliation_blocked, vendor_activation_blocked,
                # bank_change_approval_blocked. Matching the suffix rather than
                # listing them means a refusal added later appears here without
                # anybody remembering to register it, which is the failure this
                # kind of report otherwise has.
                AuditLog.action.like("%_blocked"),
            )
            .order_by(AuditLog.timestamp.desc())
            .all()
        )

        by_reason: Dict[str, Dict] = {}
        by_person: Dict[str, Dict] = {}
        by_object: Dict[str, int] = {}
        items = []

        for action, email, comment, object_type, object_id, after, at in blocks:
            reason = (after or {}).get("reason") or _reason_from_comment(comment)
            meta = BLOCK_REASONS.get(reason, {})
            is_sod = meta.get("is_sod", False)
            who = email or "unknown"

            bucket = by_reason.setdefault(reason, {
                "reason": reason,
                "label": meta.get("label", reason.replace("_", " ")),
                "is_sod": is_sod,
                "count": 0,
            })
            bucket["count"] += 1

            person = by_person.setdefault(who, {
                "who": who, "count": 0, "sod_count": 0,
            })
            person["count"] += 1
            if is_sod:
                person["sod_count"] += 1

            by_object[object_type] = by_object.get(object_type, 0) + 1

            items.append({
                "action": action,
                "reason": reason,
                "label": bucket["label"],
                "is_sod": is_sod,
                "who": email,
                "object_type": object_type,
                "object_id": str(object_id) if object_id else None,
                "at": at.isoformat() if at else None,
            })

        sod_total = sum(b["count"] for b in by_reason.values() if b["is_sod"])

        return {
            "window_days": days,
            "total_blocked": len(items),
            # Split, not merged. "Somebody tried to approve their own invoice"
            # and "somebody tried to approve an invoice with no vendor linked"
            # are both refusals and only one of them is a segregation failure;
            # reporting a single number would let a rise in clerical mistakes
            # read as a rise in attempted self-dealing.
            "sod_blocked": sod_total,
            "other_blocked": len(items) - sod_total,
            "by_reason": sorted(by_reason.values(), key=lambda r: -r["count"]),
            "by_person": sorted(by_person.values(), key=lambda p: -p["count"]),
            "by_object_type": [
                {"object_type": k, "count": v}
                for k, v in sorted(by_object.items(), key=lambda kv: -kv[1])
            ],
            "recent": items[:25],
        }

    def _require_audit(self, current_user: dict) -> None:
        from app.core.roles import PERM_VIEW_AUDIT

        if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view the segregation-"
                "of-duties report"
            )

    # --- 5. Evidence Completeness -------------------------------------------

    def evidence_completeness(self, current_user: dict) -> Dict:
        """What would embarrass you in an audit, counted now rather than then.

        Two things auditors ask for and nobody checks until they do: an invoice
        with no document behind it, and an approval that happened after its own
        deadline.
        """
        self._require(current_user)

        total = self.db.query(func.count(Invoice.id)).scalar() or 0
        without_document = (
            self.db.query(func.count(Invoice.id))
            .filter(
                Invoice.pdf_file_id.is_(None),
                Invoice.current_state.notin_([
                    InvoiceState.DRAFT.value, InvoiceState.CANCELLED.value,
                ]),
            )
            .scalar() or 0
        )
        escalated = (
            self.db.query(func.count(func.distinct(AuditLog.object_id)))
            .filter(AuditLog.action == "sla_escalated")
            .scalar() or 0
        )
        unreviewed_alerts = (
            self.db.query(func.count(WatchlistAlert.id))
            .filter(WatchlistAlert.acknowledged_at.is_(None))
            .scalar() or 0
        )

        scored = max(total, 1)
        return {
            "invoices": total,
            "missing_document": int(without_document),
            "missing_document_pct": round(without_document * 100.0 / scored, 1),
            "breached_sla": int(escalated),
            "unreviewed_watchlist_alerts": int(unreviewed_alerts),
            # A single number for the top of the page. Deliberately simple:
            # anything cleverer invites arguing with the weighting instead of
            # fixing the gaps.
            "completeness_pct": round(
                max(0.0, 100.0 - (without_document * 100.0 / scored)), 1
            ),
        }

    # --- 6. Reconciliation Health -------------------------------------------

    def reconciliation_health(self, current_user: dict) -> Dict:
        """Money that left the account with nothing accounting for it, by age.

        The oldest bucket is the one that matters. A debit nobody has explained
        in a month is not a backlog item; it is either a control failure or a
        payment somebody made outside the system, and both get worse quietly.
        """
        self._require(current_user)
        now = _now()

        rows = (
            self.db.query(
                BankStatementLine.value_date, BankStatementLine.amount,
                BankStatementLine.description,
            )
            .filter(
                BankStatementLine.matched_payment_id.is_(None),
                BankStatementLine.is_debit.is_(True),
            )
            .all()
        )

        buckets: Dict[str, Dict] = {
            label: {"bucket": label, "count": 0, "amount": 0.0}
            for _, _, label in AGE_BUCKETS
        }
        total = 0.0
        for value_date, amount, _description in rows:
            age = (now.date() - value_date).days if value_date else 0
            bucket = buckets[_bucket(age)]
            bucket["count"] += 1
            bucket["amount"] += money_to_float(amount or 0)
            total += money_to_float(amount or 0)
        for bucket in buckets.values():
            bucket["amount"] = round(bucket["amount"], 2)

        matched = (
            self.db.query(func.count(BankStatementLine.id))
            .filter(BankStatementLine.matched_payment_id.isnot(None))
            .scalar() or 0
        )
        return {
            "unexplained_count": len(rows),
            "unexplained_amount": round(total, 2),
            "matched_count": int(matched),
            "match_rate_pct": round(
                matched * 100.0 / max(matched + len(rows), 1), 1
            ),
            "aging": list(buckets.values()),
        }

    # --- 7. Autopilot Health -------------------------------------------------

    def autopilot_health(self, current_user: dict, days: int = 90) -> Dict:
        """What the machine decided, how sure it was, and what came back.

        Reversals are the number to watch. Autopilot approving a great deal is
        only good news while the reversal rate stays near zero; the two have to
        be read together, so they are reported together.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        rows = (
            self.db.query(
                AIActionLog.status, func.count(AIActionLog.id),
                func.avg(AIActionLog.confidence),
            )
            .filter(AIActionLog.created_at >= since)
            .group_by(AIActionLog.status)
            .all()
        )
        by_status = {
            status: {"count": int(count), "avg_confidence": round(float(avg or 0), 2)}
            for status, count, avg in rows
        }

        auto_approved = (
            self.db.query(func.count(func.distinct(AuditLog.object_id)))
            .filter(AuditLog.timestamp >= since, AuditLog.action == "autopilot_approved")
            .scalar() or 0
        )
        reverted = (
            self.db.query(func.count(func.distinct(AuditLog.object_id)))
            .filter(AuditLog.timestamp >= since, AuditLog.action == "autopilot_reverted")
            .scalar() or 0
        )

        return {
            "window_days": days,
            "auto_approved": int(auto_approved),
            "reverted": int(reverted),
            "reversal_rate_pct": round(
                reverted * 100.0 / max(auto_approved, 1), 1
            ) if auto_approved else 0.0,
            "ai_calls_by_status": by_status,
            # Falling back is not failure — it is the schema validator doing its
            # job — but a rising share of it means a prompt or a model has
            # started drifting.
            "schema_failures": by_status.get("failed_schema", {}).get("count", 0),
        }

    # --- everything, for the landing page ------------------------------------

    def overview(self, current_user: dict) -> Dict:
        """All seven, in one call.

        One request rather than seven, because the page shows them together and
        seven round trips would render it in pieces.
        """
        return {
            "control_room": self.control_room(current_user),
            "approval_bottlenecks": self.approval_bottlenecks(current_user),
            "exceptions": self.exceptions_heatmap(current_user),
            "policy_overrides": self.policy_overrides(current_user),
            "evidence": self.evidence_completeness(current_user),
            "reconciliation": self.reconciliation_health(current_user),
            "autopilot": self.autopilot_health(current_user),
        }

    # --- Variant D: supply chain --------------------------------------------
    #
    # Build Book D1 asks for three: stock accuracy and adjustment rate,
    # supplier delivery performance and lead time adherence, and GRN-to-invoice
    # latency with its impact on AP. All three are computed from what the
    # ledger and the receipts already record — this is reporting on existing
    # truth, not new plumbing.

    def stock_accuracy(self, current_user: dict, days: int = 90) -> Dict:
        """How often the count disagrees with the system, and by how much.

        The headline is the *write-off* rate rather than the net. Netting a
        write-on against a write-off is how a loss disappears into an
        arithmetic average: two adjustments that cancel out are not a warehouse
        in good order, they are two discrepancies.
        """
        self._require(current_user)
        from app.models.inventory import (
            Item, StockBalance, StockMovement, MOVE_ADJUSTMENT,
            REASON_THEFT_OR_LOSS, REASON_COUNT_CORRECTION,
        )
        from app.models.inventory_control import InventoryAdjustment, ADJ_POSTED

        since = _now() - timedelta(days=days)

        adjustments = (
            self.db.query(InventoryAdjustment)
            .filter(
                InventoryAdjustment.current_state == ADJ_POSTED,
                InventoryAdjustment.posted_at >= since,
            )
            .all()
        )

        by_reason: Dict[str, Dict] = {}
        written_off = written_on = 0.0
        for adjustment in adjustments:
            value = money_to_float(adjustment.total_value or 0)
            direction = sum(
                float(line.quantity_change) for line in adjustment.lines
            )
            bucket = by_reason.setdefault(
                adjustment.reason_code,
                {"reason": adjustment.reason_code, "count": 0, "value": 0.0},
            )
            bucket["count"] += 1
            bucket["value"] = round(bucket["value"] + value, 2)
            if direction < 0:
                written_off += value
            else:
                written_on += value

        # The denominator: what is on hand, valued. An adjustment rate needs
        # something to be a rate *of*, and "adjustments per month" says nothing
        # about a warehouse that doubled in size.
        holding = (
            self.db.query(
                func.coalesce(
                    func.sum(StockBalance.quantity * Item.standard_cost), 0
                )
            )
            .join(Item, Item.id == StockBalance.item_id)
            .scalar()
        ) or 0
        holding_value = money_to_float(holding)

        movement_count = (
            self.db.query(func.count(StockMovement.id))
            .filter(StockMovement.created_at >= since)
            .scalar()
        ) or 0
        adjustment_movements = (
            self.db.query(func.count(StockMovement.id))
            .filter(
                StockMovement.created_at >= since,
                StockMovement.movement_type == MOVE_ADJUSTMENT,
            )
            .scalar()
        ) or 0

        # Loss and theft called out separately: these are the reasons that mean
        # something left without anybody selling it, and burying them in a
        # total is how they stop being noticed.
        unexplained = round(sum(
            row["value"] for code, row in by_reason.items()
            if code == REASON_THEFT_OR_LOSS
        ), 2)

        return {
            "window_days": days,
            "adjustments_posted": len(adjustments),
            "value_written_off": round(written_off, 2),
            "value_written_on": round(written_on, 2),
            "unexplained_loss_value": unexplained,
            "holding_value": holding_value,
            "write_off_rate_percent": (
                round(written_off / holding_value * 100, 2)
                if holding_value else 0.0
            ),
            "adjustment_share_of_movements_percent": (
                round(adjustment_movements / movement_count * 100, 2)
                if movement_count else 0.0
            ),
            "count_corrections": sum(
                row["count"] for code, row in by_reason.items()
                if code == REASON_COUNT_CORRECTION
            ),
            "by_reason": sorted(by_reason.values(), key=lambda r: -r["value"]),
        }

    def supplier_delivery_performance(
        self, current_user: dict, days: int = 180
    ) -> Dict:
        """Who delivers on time, in full, and undamaged.

        Three separate questions, deliberately not averaged into one score. A
        supplier who is always late but never wrong needs a different
        conversation from one who is punctual and sends damaged goods, and a
        single number hides which of them you have.
        """
        self._require(current_user)
        from app.models.goods_receipt import GoodsReceipt
        from app.models.inventory_control import VendorReturn
        from app.models.purchase_order import PurchaseOrder
        from app.models.vendor import Vendor

        since = (_now() - timedelta(days=days)).date()

        rows = (
            self.db.query(
                PurchaseOrder.vendor_name,
                PurchaseOrder.expected_date,
                GoodsReceipt.received_date,
                GoodsReceipt.id,
            )
            .join(GoodsReceipt, GoodsReceipt.purchase_order_id == PurchaseOrder.id)
            .filter(GoodsReceipt.received_date >= since)
            .all()
        )

        by_vendor: Dict[str, Dict] = {}
        for vendor_name, expected, received, _receipt_id in rows:
            bucket = by_vendor.setdefault(vendor_name, {
                "vendor": vendor_name, "deliveries": 0, "on_time": 0,
                "late": 0, "unknown_due_date": 0, "total_days_late": 0,
                "returns_their_fault": 0,
            })
            bucket["deliveries"] += 1

            if expected is None:
                # No promised date means on-time is unanswerable. Counted
                # separately rather than assumed on time, which would flatter
                # every vendor whose orders never carried a date.
                bucket["unknown_due_date"] += 1
            elif received and received > expected:
                bucket["late"] += 1
                bucket["total_days_late"] += (received - expected).days
            else:
                bucket["on_time"] += 1

        attributable = dict(
            self.db.query(VendorReturn.vendor_id, func.count(VendorReturn.id))
            .filter(VendorReturn.vendor_attributable.is_(True))
            .group_by(VendorReturn.vendor_id)
            .all()
        )
        if attributable:
            for vendor in (
                self.db.query(Vendor)
                .filter(Vendor.id.in_(list(attributable)))
                .all()
            ):
                if vendor.legal_name in by_vendor:
                    by_vendor[vendor.legal_name]["returns_their_fault"] = (
                        attributable[vendor.id]
                    )

        vendors = []
        for row in by_vendor.values():
            measurable = row["on_time"] + row["late"]
            vendors.append({
                **row,
                "on_time_percent": (
                    round(row["on_time"] / measurable * 100, 1)
                    if measurable else None
                ),
                "average_days_late": (
                    round(row["total_days_late"] / row["late"], 1)
                    if row["late"] else 0.0
                ),
            })

        # Worst first, with the unmeasurable ones last: a vendor nobody can
        # score is a data problem, not a performance problem, and putting them
        # at the top would bury the suppliers actually failing.
        vendors.sort(
            key=lambda v: (v["on_time_percent"] is None, v["on_time_percent"] or 0)
        )

        return {
            "window_days": days,
            "vendors": vendors,
            "deliveries": sum(v["deliveries"] for v in vendors),
            "late_deliveries": sum(v["late"] for v in vendors),
            "deliveries_with_no_promised_date": sum(
                v["unknown_due_date"] for v in vendors
            ),
        }

    def receipt_to_invoice_latency(self, current_user: dict, days: int = 90) -> Dict:
        """How long goods sit received but uninvoiced, and what it is worth.

        The Build Book calls this "GRN to invoice latency and impact on AP",
        and the impact is the point: goods received without an invoice are a
        liability the ledger does not show yet. A month-end that misses them
        understates what is owed, which is the accrual an auditor asks about.
        """
        self._require(current_user)
        from app.models.goods_receipt import GoodsReceipt, GoodsReceiptLine
        from app.models.invoice import Invoice
        from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine

        since = (_now() - timedelta(days=days)).date()
        today = _now().date()

        invoiced_orders = {
            row[0] for row in
            self.db.query(Invoice.purchase_order_id)
            .filter(Invoice.purchase_order_id.isnot(None))
            .all()
        }

        rows = (
            self.db.query(
                GoodsReceipt.grn_number,
                GoodsReceipt.received_date,
                PurchaseOrder.id,
                PurchaseOrder.vendor_name,
                func.sum(
                    GoodsReceiptLine.quantity_received * PurchaseOrderLine.unit_price
                ),
            )
            .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
            .join(
                GoodsReceiptLine,
                GoodsReceiptLine.goods_receipt_id == GoodsReceipt.id,
            )
            .join(
                PurchaseOrderLine,
                PurchaseOrderLine.id == GoodsReceiptLine.purchase_order_line_id,
            )
            .filter(GoodsReceipt.received_date >= since)
            .group_by(
                GoodsReceipt.id, GoodsReceipt.grn_number,
                GoodsReceipt.received_date, PurchaseOrder.id,
                PurchaseOrder.vendor_name,
            )
            .all()
        )

        waiting, buckets, total_value = [], {}, 0.0
        for grn, received_date, order_id, vendor, value in rows:
            if order_id in invoiced_orders:
                continue
            age = (today - received_date).days if received_date else 0
            amount = money_to_float(value or 0)
            total_value += amount
            bucket = _bucket(age)
            buckets[bucket] = buckets.get(bucket, 0) + 1
            waiting.append({
                "grn_number": grn,
                "vendor": vendor,
                "received_date": received_date,
                "days_waiting": age,
                "value": amount,
                "link": f"/ai-tools/purchase-orders/{order_id}",
            })

        waiting.sort(key=lambda r: -r["days_waiting"])

        return {
            "window_days": days,
            "receipts_awaiting_invoice": len(waiting),
            "value_awaiting_invoice": round(total_value, 2),
            "by_age": [
                {"bucket": name, "count": count} for name, count in buckets.items()
            ],
            "oldest": waiting[:20],
        }

    # --- Variant C: HR ------------------------------------------------------
    #
    # Build Book C1/C2 reports: time to hire and onboarding SLA completion,
    # headcount plan vs actual, payroll variance and exception trends.
    # `headcount_plan` lives on HeadcountService, which owns that question;
    # these are the two that read across HR rather than within one record.

    def hiring_pipeline(self, current_user: dict, days: int = 365) -> Dict:
        """Time to hire, and where requests are stuck.

        Measured from **approval** to filled, not from when the request was
        raised. The gap before approval is a budget decision and belongs to
        whoever is sitting on it; time-to-hire is a recruiting number, and
        mixing the two produces a figure neither team can act on.

        Requests still open are reported separately from those filled. An
        average computed only over completed hires flatters every pipeline —
        the roles that never get filled are exactly the ones missing from it.
        """
        self._require_hr(current_user)
        from app.models.hr import (
            HeadcountRequest, HC_APPROVED, HC_FILLED, HC_PENDING_APPROVAL,
        )

        since = _now() - timedelta(days=days)
        today = _now().date()

        filled = (
            self.db.query(HeadcountRequest)
            .filter(
                HeadcountRequest.current_state == HC_FILLED,
                HeadcountRequest.filled_at >= since,
            )
            .all()
        )
        still_open = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_APPROVED)
            .all()
        )
        awaiting = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_PENDING_APPROVAL)
            .count()
        )

        days_to_fill = [
            (row.filled_at - row.approved_at).days
            for row in filled
            if row.filled_at and row.approved_at
        ]

        return {
            "window_days": days,
            "filled": len(filled),
            "average_days_to_fill": (
                round(sum(days_to_fill) / len(days_to_fill), 1)
                if days_to_fill else None
            ),
            "longest_days_to_fill": max(days_to_fill) if days_to_fill else None,
            "awaiting_approval": awaiting,
            "approved_still_open": len(still_open),
            # The number an average hides: roles approved long ago that nobody
            # has hired. They are committed cost and an unstaffed team.
            "open_positions_ageing": [
                {
                    "request_number": row.request_number,
                    "job_title": row.job_title,
                    "positions": row.positions,
                    "days_open": (
                        (today - row.approved_at.date()).days
                        if row.approved_at else None
                    ),
                    "annual_cost": float(row.annual_cost or 0),
                }
                for row in sorted(
                    still_open,
                    key=lambda r: r.approved_at or _now(),
                )
            ],
        }

    def payroll_variance(self, current_user: dict, days: int = 365) -> Dict:
        """What pay changed, why, and how unusually.

        Build Book: "payroll variance and exception trends". Reported as
        *movement* — the total of every applied change and the reasons behind
        them — rather than as a payroll total, because this report is readable
        by anyone with hr.view while salaries are not. A variance report that
        leaked the payroll would defeat the masking the rest of the module
        enforces.

        Corrections are called out separately. A rise is a decision; a
        correction is a mistake somebody made earlier, and a rising number of
        them says something about the process rather than about pay.
        """
        self._require_hr(current_user)
        from app.models.hr import (
            PayrollChangeRequest, PAY_APPLIED, PAY_REJECTED,
            PAY_REASON_CORRECTION,
        )

        since = _now() - timedelta(days=days)

        applied = (
            self.db.query(PayrollChangeRequest)
            .filter(
                PayrollChangeRequest.current_state == PAY_APPLIED,
                PayrollChangeRequest.applied_at >= since,
            )
            .all()
        )
        rejected = (
            self.db.query(PayrollChangeRequest)
            .filter(
                PayrollChangeRequest.current_state == PAY_REJECTED,
                PayrollChangeRequest.created_at >= since,
            )
            .count()
        )

        by_reason: Dict[str, Dict] = {}
        increases = decreases = 0
        total_movement = 0.0
        for change in applied:
            amount = money_to_float(change.total_amount or 0)
            total_movement += amount
            bucket = by_reason.setdefault(
                change.reason_code,
                {"reason": change.reason_code, "count": 0, "movement": 0.0},
            )
            bucket["count"] += 1
            bucket["movement"] = round(bucket["movement"] + amount, 2)

            if change.current_salary is not None and change.new_salary is not None:
                if change.new_salary > change.current_salary:
                    increases += 1
                elif change.new_salary < change.current_salary:
                    decreases += 1

        corrections = sum(
            row["count"] for code, row in by_reason.items()
            if code == PAY_REASON_CORRECTION
        )

        return {
            "window_days": days,
            "changes_applied": len(applied),
            "total_movement": round(total_movement, 2),
            "increases": increases,
            "decreases": decreases,
            "corrections": corrections,
            "rejected": rejected,
            "by_reason": sorted(by_reason.values(), key=lambda r: -r["movement"]),
        }

    def expense_exceptions(self, current_user: dict, days: int = 90) -> Dict:
        """Claims that needed a rule waived, and claims waiting to be paid.

        Two different failures in one report because they are read by the same
        person. An override is a control being set aside; an approved claim
        that has not been paid is an employee out of pocket, and nothing else
        in the system chases it.
        """
        self._require_hr(current_user)
        from app.models.employee import Employee
        from app.models.hr import (
            ExpenseReimbursement, EXP_APPROVED, EXP_PENDING_APPROVAL, EXP_PAID,
        )

        since = _now() - timedelta(days=days)
        today = _now().date()

        claims = (
            self.db.query(ExpenseReimbursement, Employee)
            .join(Employee, Employee.id == ExpenseReimbursement.employee_id)
            .filter(ExpenseReimbursement.created_at >= since)
            .all()
        )

        overrides, awaiting_payment = [], []
        paid = pending = 0
        by_category: Dict[str, Dict] = {}

        for claim, employee in claims:
            amount = money_to_float(claim.total_amount or 0)
            bucket = by_category.setdefault(
                claim.category,
                {"category": claim.category, "count": 0, "amount": 0.0},
            )
            bucket["count"] += 1
            bucket["amount"] = round(bucket["amount"] + amount, 2)

            if claim.policy_override_reason:
                overrides.append({
                    "claim_number": claim.claim_number,
                    "employee": employee.full_name,
                    "category": claim.category,
                    "amount": amount,
                    "reason": claim.policy_override_reason,
                    "approved_by": claim.approved_by,
                })
            if claim.current_state == EXP_PAID:
                paid += 1
            elif claim.current_state == EXP_PENDING_APPROVAL:
                pending += 1
            elif claim.current_state == EXP_APPROVED:
                awaiting_payment.append({
                    "claim_number": claim.claim_number,
                    "employee": employee.full_name,
                    "amount": amount,
                    "days_since_approval": (
                        (today - claim.approved_at.date()).days
                        if claim.approved_at else None
                    ),
                })

        awaiting_payment.sort(key=lambda r: -(r["days_since_approval"] or 0))

        return {
            "window_days": days,
            "claims": len(claims),
            "pending_approval": pending,
            "paid": paid,
            "policy_overrides": len(overrides),
            "approved_awaiting_payment": len(awaiting_payment),
            "value_awaiting_payment": round(
                sum(r["amount"] for r in awaiting_payment), 2
            ),
            "by_category": sorted(
                by_category.values(), key=lambda r: -r["amount"]
            ),
            "overrides": overrides,
            "awaiting_payment": awaiting_payment[:20],
        }

    def _require_hr(self, current_user: dict) -> None:
        """HR reports read with hr.view rather than the dashboard permission.

        Deliberately not the same gate: the other dashboards aggregate records
        anybody with invoices.view could open individually, while these
        aggregate people. Nothing here exposes a salary, but "who was hired,
        who left, whose pay moved" is still not company-readable.
        """
        from app.core.roles import PERM_VIEW_HR

        if not has_permission(current_user["role"], PERM_VIEW_HR):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view HR reports"
            )

    # --- CFO: what we owe, and what we spend ---------------------------------

    #: Payables aging, in the buckets a finance team actually uses. Deliberately
    #: NOT the module-level AGE_BUCKETS, which are 0-2/3-7/8-30/30+ and exist to
    #: answer "how long has this been stuck" for an operational reader. A CFO
    #: reading a payables balance expects 30/60/90, measured against the due
    #: date rather than against how long a record has sat in a state. Two
    #: different questions that would give two different numbers from the same
    #: invoices, so they get two different scales.
    PAYABLE_BUCKETS = [
        (None, 0, "not yet due"),
        (0, 30, "1-30 days overdue"),
        (30, 60, "31-60 days overdue"),
        (60, 90, "61-90 days overdue"),
        (90, None, "over 90 days overdue"),
    ]

    #: The pipeline a payable travels, in order. Each stage is somewhere money
    #: can sit, and the funnel exists so a CFO can see which stage is holding
    #: the balance — a large "approved, not in a run" is a treasury problem, a
    #: large "awaiting approval" is an operations one, and the total is the
    #: same either way.
    FUNNEL_STAGES = [
        ("awaiting_approval", "Awaiting approval",
         "Nobody has agreed to pay this yet."),
        ("approved_not_scheduled", "Approved, not in a payment run",
         "Agreed but not scheduled. This is the stage that quietly grows."),
        ("scheduled_not_released", "In a run, not released",
         "Prepared and waiting on a second signature."),
        ("released_not_cleared", "Released, not seen on a statement",
         "Instructed. Not yet confirmed to have left the account."),
    ]

    def ap_aging(self, current_user: dict) -> Dict:
        """What we owe, how late it is, and which stage is holding it.

        Build Book, CFO / Finance Director: "AP aging with exception funnel."

        Aged against the **due date**, not against how long the record has been
        sitting. Those are different figures and only the first is a payables
        aging — an invoice entered yesterday on 90-day terms is not overdue,
        and one entered ninety days ago on 7-day terms is badly overdue. Aging
        by record age would rank those two the wrong way round.

        Invoices with no due date are counted and reported separately rather
        than bucketed as "not yet due". The column is nullable, so treating
        missing as "not due" would quietly move a possibly-overdue balance into
        the comfortable column — and the missing dates are themselves the
        finding, because nothing can chase what has no date.

        A point-in-time balance, so it takes no `days` window: what is owed is
        owed regardless of the period somebody happens to be looking at.
        """
        self._require(current_user)
        today = _now().date()

        rows = (
            self.db.query(
                Invoice.id, Invoice.invoice_number, Invoice.due_date,
                Invoice.total_amount, Invoice.current_state,
                Vendor.legal_name,
            )
            .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
            .filter(
                # Everything owed and not yet settled. Rejected and cancelled
                # are not liabilities; paid ones have already left.
                Invoice.current_state.in_([
                    InvoiceState.VALIDATED.value,
                    InvoiceState.PENDING_APPROVAL.value,
                    InvoiceState.APPROVED.value,
                ]),
            )
            .all()
        )

        buckets = {
            label: {"bucket": label, "count": 0, "amount": 0.0}
            for _, _, label in self.PAYABLE_BUCKETS
        }
        no_due_date = {"count": 0, "amount": 0.0}
        overdue_amount = 0.0
        total = 0.0
        worst: List[Dict] = []

        for row in rows:
            amount = money_to_float(row.total_amount or 0)
            total += amount

            if row.due_date is None:
                no_due_date["count"] += 1
                no_due_date["amount"] += amount
                continue

            days_over = (today - row.due_date).days
            bucket = buckets[self._payable_bucket(days_over)]
            bucket["count"] += 1
            bucket["amount"] += amount

            if days_over > 0:
                overdue_amount += amount
                worst.append({
                    "invoice_id": str(row.id),
                    "invoice_number": row.invoice_number,
                    "vendor": row.legal_name,
                    "amount": round(amount, 2),
                    "days_overdue": days_over,
                    "state": row.current_state,
                })

        for bucket in buckets.values():
            bucket["amount"] = round(bucket["amount"], 2)
        no_due_date["amount"] = round(no_due_date["amount"], 2)
        worst.sort(key=lambda r: (-r["days_overdue"], -r["amount"]))

        return {
            "as_of": today.isoformat(),
            "total_payable": round(total, 2),
            "total_overdue": round(overdue_amount, 2),
            "overdue_pct": round(overdue_amount * 100.0 / total, 1) if total else 0.0,
            "aging": [buckets[label] for _, _, label in self.PAYABLE_BUCKETS],
            # Reported on its own rather than folded into a bucket. See the
            # docstring: a payable with no due date cannot be chased, and
            # rolling it into "not yet due" would hide exactly that.
            "no_due_date": no_due_date,
            "funnel": self._payable_funnel(),
            "most_overdue": worst[:20],
        }

    def _payable_bucket(self, days_overdue: int) -> str:
        for low, high, label in self.PAYABLE_BUCKETS:
            if low is None:
                if days_overdue <= 0:
                    return label
                continue
            if days_overdue > low and (high is None or days_overdue <= high):
                return label
        return self.PAYABLE_BUCKETS[-1][2]

    def _payable_funnel(self) -> List[Dict]:
        """Where the payable balance is sitting, stage by stage.

        Each stage is counted from the records themselves rather than from a
        status column somebody maintains, so a run abandoned halfway shows up
        where it actually is rather than where it was last marked.
        """
        awaiting = self._sum_invoices([
            InvoiceState.VALIDATED.value, InvoiceState.PENDING_APPROVAL.value,
        ])

        # Approved invoices that no payment line references at all.
        scheduled = (
            self.db.query(PaymentLine.invoice_id)
            .filter(PaymentLine.invoice_id.isnot(None))
            .subquery()
        )
        unscheduled = (
            self.db.query(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total_amount), 0),
            )
            .filter(
                Invoice.current_state == InvoiceState.APPROVED.value,
                ~Invoice.id.in_(select(scheduled.c.invoice_id)),
            )
            .one()
        )

        totals = {
            "awaiting_approval": awaiting,
            "approved_not_scheduled": (
                int(unscheduled[0] or 0), money_to_float(unscheduled[1] or 0),
            ),
            "scheduled_not_released": self._sum_payment_lines([
                PaymentState.DRAFT.value, PaymentState.PENDING_RELEASE.value,
            ]),
            "released_not_cleared": self._sum_payment_lines(
                [PaymentState.RELEASED.value], unmatched=True
            ),
        }
        return [
            {
                "stage": key,
                "label": label,
                "note": note,
                "count": totals[key][0],
                "amount": round(totals[key][1], 2),
            }
            for key, label, note in self.FUNNEL_STAGES
        ]

    def _sum_invoices(self, states: List[str]):
        row = (
            self.db.query(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total_amount), 0),
            )
            .filter(Invoice.current_state.in_(states))
            .one()
        )
        return int(row[0] or 0), money_to_float(row[1] or 0)

    def _sum_payment_lines(self, states: List[str], unmatched: bool = False):
        query = (
            self.db.query(
                func.count(PaymentLine.id),
                func.coalesce(func.sum(PaymentLine.amount), 0),
            )
            .join(Payment, Payment.id == PaymentLine.payment_id)
            .filter(Payment.current_state.in_(states))
        )
        if unmatched:
            # Released but never seen on a statement: money that has left on
            # paper and not in evidence.
            matched = (
                self.db.query(BankStatementLine.matched_payment_id)
                .filter(BankStatementLine.matched_payment_id.isnot(None))
                .subquery()
            )
            query = query.filter(
                ~Payment.id.in_(select(matched.c.matched_payment_id))
            )
        row = query.one()
        return int(row[0] or 0), money_to_float(row[1] or 0)

    # --- CFO: spend analytics ------------------------------------------------

    def spend_analytics(self, current_user: dict, days: int = 365) -> Dict:
        """Where the money went, by the dimensions that actually exist.

        Build Book, CFO / Finance Director: "spend analytics."

        Three dimensions, because three is what an invoice carries: vendor, GL
        account code, and cost centre. There is no category taxonomy in this
        system, so there is no category breakdown here — the same reasoning the
        approval matrix applies to its missing category axis. A fourth chart
        built on a field nobody fills would look like analysis and be noise.

        Both `gl_account_code` and `cost_center` are nullable, and the
        unclassified share is reported as its own figure rather than dropped
        from the denominator. A spend breakdown that silently omits the
        unclassified reads as complete while describing a fraction, and the
        fraction it describes is the well-behaved one — an unclassified
        balance is the part nobody has had to justify.

        Counted on **approved** spend and later: an invoice somebody entered
        and nobody agreed to is not spend, it is a claim.
        """
        self._require(current_user)
        since = (_now() - timedelta(days=days)).date()

        rows = (
            self.db.query(
                Invoice.total_amount, Invoice.invoice_date,
                Invoice.gl_account_code, Invoice.cost_center,
                Invoice.vendor_id, Vendor.legal_name,
            )
            .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
            .filter(
                Invoice.current_state.in_([
                    InvoiceState.APPROVED.value, InvoiceState.PAID.value,
                ]),
                Invoice.invoice_date >= since,
            )
            .all()
        )

        by_vendor: Dict[str, Dict] = {}
        by_account: Dict[str, Dict] = {}
        by_cost_centre: Dict[str, Dict] = {}
        by_month: Dict[str, Dict] = {}
        total = 0.0
        no_account = 0.0
        no_cost_centre = 0.0

        for row in rows:
            amount = money_to_float(row.total_amount or 0)
            total += amount

            vendor = row.legal_name or "(no vendor)"
            self._accumulate(by_vendor, vendor, amount)

            if row.gl_account_code:
                self._accumulate(by_account, row.gl_account_code, amount)
            else:
                no_account += amount

            if row.cost_center:
                self._accumulate(by_cost_centre, row.cost_center, amount)
            else:
                no_cost_centre += amount

            if row.invoice_date:
                self._accumulate(
                    by_month, row.invoice_date.strftime("%Y-%m"), amount
                )

        top_vendors = sorted(by_vendor.values(), key=lambda r: -r["amount"])
        # Concentration: how much of the spend sits with the largest handful.
        # A CFO reads this as negotiating position on one side and single-
        # supplier exposure on the other, which is why it is one number rather
        # than an invitation to add up the table.
        #
        # Not reported below six vendors, where it is arithmetically always
        # 100% and therefore says nothing about concentration — it says there
        # are five or fewer suppliers, which the vendor count already says. A
        # headline "100%" would read as an alarm about exposure rather than a
        # fact about the size of the supplier list, so it is withheld the same
        # way match rate and payment failures are on the AP/Treasury report.
        top_five = sum(r["amount"] for r in top_vendors[:5])
        concentration = (
            round(top_five * 100.0 / total, 1)
            if total and len(by_vendor) > 5 else None
        )

        return {
            "window_days": days,
            "since": since.isoformat(),
            "total_spend": round(total, 2),
            "invoice_count": len(rows),
            "by_vendor": top_vendors[:25],
            "vendor_count": len(by_vendor),
            "top_5_vendor_share_pct": concentration,
            "by_gl_account": sorted(
                by_account.values(), key=lambda r: -r["amount"]
            )[:25],
            "by_cost_centre": sorted(
                by_cost_centre.values(), key=lambda r: -r["amount"]
            )[:25],
            "by_month": sorted(by_month.values(), key=lambda r: r["key"]),
            # Kept in the denominator and named. See the docstring.
            "unclassified": {
                "no_gl_account": round(no_account, 2),
                "no_gl_account_pct": (
                    round(no_account * 100.0 / total, 1) if total else 0.0
                ),
                "no_cost_centre": round(no_cost_centre, 2),
                "no_cost_centre_pct": (
                    round(no_cost_centre * 100.0 / total, 1) if total else 0.0
                ),
            },
            #: Absent for the same reason the approval matrix reports no
            #: category axis: invoices carry no category, so a breakdown by one
            #: would be invented here rather than measured.
            "by_category": None,
        }

    @staticmethod
    def _accumulate(target: Dict[str, Dict], key: str, amount: float) -> None:
        entry = target.setdefault(key, {"key": key, "count": 0, "amount": 0.0})
        entry["count"] += 1
        entry["amount"] = round(entry["amount"] + amount, 2)

    # --- Procurement: how long sourcing takes, and what it saved -------------

    def _require_sourcing(self, current_user: dict) -> None:
        """Sourcing reports read with requisitions.view.

        Same principle _require states — reading an aggregate is reading the
        records under it. Everybody who can open a requisition can open the
        RFQ raised from it, so the gate is the one that governs both rather
        than sourcing.manage, which is the permission to *run* an RFQ and
        would wrongly exclude the procurement lead who only reads them.
        """
        from app.core.roles import PERM_VIEW_REQUISITION

        if not has_permission(current_user["role"], PERM_VIEW_REQUISITION):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view sourcing reports"
            )

    #: The sourcing lifecycle, as the audit trail records it. Each pair is a
    #: stage somebody is accountable for, and they are separated because the
    #: remedies differ: a long draft-to-issued is our own delay, a long
    #: issued-to-closed is the window we chose to give vendors, and a long
    #: closed-to-awarded is a decision nobody is making. One "RFQ cycle time"
    #: number averages all three into something nobody can act on.
    #: (key, label, start action, end action, note). The label is carried
    #: rather than derived from the action names, because those are internal
    #: vocabulary — "created_from_award" is an audit action, not something a
    #: procurement lead should be asked to read.
    RFQ_STAGES = [
        ("draft_to_issued", "Drafting the RFQ", "created", "issued",
         "Ours. The RFQ was written and then sat."),
        ("issued_to_closed", "Vendors' window to respond", "issued", "closed",
         "The window vendors were given. A choice, not a delay."),
        ("closed_to_awarded", "Choosing a winner", "closed", "awarded",
         "Ours. Quotes were in and nobody picked."),
        ("awarded_to_po", "Raising the purchase order",
         "awarded", "created_from_award",
         "Ours. The decision was made and the order not raised."),
    ]

    def rfq_cycle_time(self, current_user: dict, days: int = 180) -> Dict:
        """How long sourcing takes, stage by stage, and what it saved.

        Build Book, Procurement Leadership: "RFQ cycle time, savings vs
        baseline."

        Measured from the audit trail rather than from the RFQ's own
        timestamp columns, the same choice invoice_throughput and
        approval_bottlenecks make: the trail is what happened, and a duration
        column is only ever as right as the code that last wrote it.

        Reported per stage rather than as one number, because the three stages
        we control and the one we do not have different remedies — see
        RFQ_STAGES. A single average would hide a fortnight of nobody deciding
        behind a generous vendor window.

        Savings are measured against **two** baselines and named as two
        figures, because neither alone is honest:

          * against the requisition's estimated_amount — what somebody
            committed to in writing before any vendor quoted, so it cannot be
            adjusted after the fact to make the saving look better;
          * against the highest compliant quote on the same RFQ — the worst
            alternative that was actually on the table.

        The second is only meaningful where more than one compliant quote
        arrived, so RFQs awarded on a single quote are excluded from it and
        counted separately. An award with nothing to compare against did not
        save anything; it just happened.

        180 days by default: a sourcing cycle is measured in weeks, so 90 days
        would often hold only one or two completed cycles.
        """
        self._require_sourcing(current_user)
        since = _now() - timedelta(days=days)

        events = (
            self.db.query(
                AuditLog.object_id, AuditLog.action,
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.object_type == "rfq",
                AuditLog.action.in_(
                    ["created", "issued", "closed", "awarded", "cancelled"]
                ),
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.object_id, AuditLog.action)
            .all()
        )
        # The PO event hangs off the purchase order, not the RFQ, so it is
        # fetched by correlation rather than by object id.
        timeline: Dict[str, Dict[str, object]] = {}
        for object_id, action, at in events:
            timeline.setdefault(str(object_id), {})[action] = at

        po_raised = (
            self.db.query(
                AuditLog.correlation_id,
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.object_type == "purchase_order",
                AuditLog.action == "created_from_award",
                AuditLog.timestamp >= since,
            )
            .group_by(AuditLog.correlation_id)
            .all()
        )
        by_correlation = {str(cid): at for cid, at in po_raised if cid}

        rfqs = (
            self.db.query(
                RFQ.id, RFQ.rfq_number, RFQ.current_state, RFQ.closes_at,
                RFQ.correlation_id, RFQ.awarded_quote_id, RFQ.requisition_id,
            )
            .filter(RFQ.created_at >= since)
            .all()
        )

        durations: Dict[str, List[float]] = {
            key: [] for key, _, _, _, _ in self.RFQ_STAGES
        }
        for rfq in rfqs:
            marks = dict(timeline.get(str(rfq.id), {}))
            if rfq.correlation_id and str(rfq.correlation_id) in by_correlation:
                marks["created_from_award"] = by_correlation[str(rfq.correlation_id)]
            for key, _label, start, end, _note in self.RFQ_STAGES:
                if marks.get(start) and marks.get(end):
                    hours = (marks[end] - marks[start]).total_seconds() / 3600.0
                    if hours >= 0:
                        durations[key].append(hours)

        stages = [
            {
                "stage": key,
                "label": label,
                # The raw audit actions, kept so somebody reconciling this
                # against the trail can see exactly what was measured.
                "from": start,
                "to": end,
                "note": note,
                "count": len(durations[key]),
                "median_days": _median_days(durations[key]),
                "worst_days": (
                    round(max(durations[key]) / 24.0, 1) if durations[key] else None
                ),
            }
            for key, label, start, end, note in self.RFQ_STAGES
        ]

        return {
            "window_days": days,
            "rfq_count": len(rfqs),
            "stages": stages,
            "competition": self._rfq_competition(rfqs),
            "savings": self._rfq_savings(rfqs),
            "overdue_open": self._rfq_past_close(rfqs),
        }

    def _rfq_competition(self, rfqs: List) -> Dict:
        """Whether anybody actually competed.

        A cycle time says how fast sourcing ran. It says nothing about whether
        sourcing did its job, and an RFQ issued to one vendor who quoted once
        is a purchase order with extra steps. Both halves are reported: how
        many were invited, and how many answered.
        """
        rfq_ids = [r.id for r in rfqs]
        if not rfq_ids:
            return {
                "invited": 0, "quoted": 0, "response_rate_pct": 0.0,
                "single_quote_awards": 0, "awarded_with_competition": 0,
            }

        invited = (
            self.db.query(func.count(RFQVendor.id))
            .filter(RFQVendor.rfq_id.in_(rfq_ids)).scalar() or 0
        )
        quotes_per_rfq = dict(
            self.db.query(Quote.rfq_id, func.count(Quote.id))
            .filter(Quote.rfq_id.in_(rfq_ids))
            .group_by(Quote.rfq_id)
            .all()
        )
        quoted = sum(quotes_per_rfq.values())

        awarded = [r for r in rfqs if r.awarded_quote_id is not None]
        single = sum(1 for r in awarded if quotes_per_rfq.get(r.id, 0) <= 1)

        return {
            "invited": int(invited),
            "quoted": int(quoted),
            "response_rate_pct": (
                round(quoted * 100.0 / invited, 1) if invited else 0.0
            ),
            # Awarded on one quote or none. Not necessarily wrong — sole
            # supply is a real situation — but it is the case somebody should
            # be able to point at, which a cycle-time average never surfaces.
            "single_quote_awards": single,
            "awarded_with_competition": len(awarded) - single,
        }

    def _rfq_savings(self, rfqs: List) -> Dict:
        """Two baselines, named separately. See rfq_cycle_time's docstring."""
        awarded = [r for r in rfqs if r.awarded_quote_id is not None]
        if not awarded:
            return {
                "awarded_value": 0.0,
                "vs_estimate": None, "vs_estimate_pct": None,
                "vs_highest_quote": None, "vs_highest_quote_pct": None,
                "awards_with_no_comparison": 0,
            }

        quote_ids = [r.awarded_quote_id for r in awarded]
        awarded_amounts = dict(
            self.db.query(Quote.id, Quote.total_amount)
            .filter(Quote.id.in_(quote_ids)).all()
        )
        # Highest *compliant* quote per RFQ. A non-compliant quote is not an
        # alternative that was available, so measuring a saving against one
        # would be measuring against something nobody could have bought.
        highest = dict(
            self.db.query(Quote.rfq_id, func.max(Quote.total_amount))
            .filter(
                Quote.rfq_id.in_([r.id for r in awarded]),
                Quote.is_compliant.is_(True),
            )
            .group_by(Quote.rfq_id)
            .all()
        )
        compliant_counts = dict(
            self.db.query(Quote.rfq_id, func.count(Quote.id))
            .filter(
                Quote.rfq_id.in_([r.id for r in awarded]),
                Quote.is_compliant.is_(True),
            )
            .group_by(Quote.rfq_id)
            .all()
        )
        estimates = dict(
            self.db.query(
                PurchaseRequisition.id, PurchaseRequisition.estimated_amount
            )
            .filter(PurchaseRequisition.id.in_(
                [r.requisition_id for r in awarded if r.requisition_id]
            )).all()
        )

        total_awarded = 0.0
        estimate_base = 0.0
        estimate_awarded = 0.0
        highest_base = 0.0
        comparable_awarded = 0.0
        no_comparison = 0

        for rfq in awarded:
            amount = money_to_float(awarded_amounts.get(rfq.awarded_quote_id) or 0)
            total_awarded += amount

            estimate = estimates.get(rfq.requisition_id)
            if estimate is not None:
                estimate_base += money_to_float(estimate)
                estimate_awarded += amount

            # Only where a genuine alternative existed.
            if compliant_counts.get(rfq.id, 0) > 1 and rfq.id in highest:
                highest_base += money_to_float(highest[rfq.id])
                comparable_awarded += amount
            else:
                no_comparison += 1

        def _delta(base: float, actual: float):
            if not base:
                return None, None
            saved = base - actual
            return round(saved, 2), round(saved * 100.0 / base, 1)

        vs_estimate, vs_estimate_pct = _delta(estimate_base, estimate_awarded)
        vs_highest, vs_highest_pct = _delta(highest_base, comparable_awarded)

        return {
            "awarded_value": round(total_awarded, 2),
            # What somebody put in writing before any vendor quoted, so it
            # cannot be adjusted afterwards to flatter the saving.
            "vs_estimate": vs_estimate,
            "vs_estimate_pct": vs_estimate_pct,
            # The worst alternative that was actually on the table.
            "vs_highest_quote": vs_highest,
            "vs_highest_quote_pct": vs_highest_pct,
            # Awarded on a single compliant quote, so there was nothing to
            # save against. Counted rather than folded in at zero, which would
            # dilute the rate with awards that never had a comparison.
            "awards_with_no_comparison": no_comparison,
        }

    def _rfq_past_close(self, rfqs: List) -> List[Dict]:
        """Issued, past the date it said it would close, still open.

        Nothing errors when this happens — the RFQ simply stays open and the
        requisition behind it stays unmet, which is why it needs a report to
        appear at all.
        """
        now = _now()
        late = [
            {
                "rfq_id": str(r.id),
                "rfq_number": r.rfq_number,
                "closed_days_ago": round((now - r.closes_at).total_seconds() / 86400.0, 1),
            }
            for r in rfqs
            if r.current_state == RFQState.ISSUED.value
            and r.closes_at is not None and r.closes_at < now
        ]
        late.sort(key=lambda r: -r["closed_days_ago"])
        return late[:20]

    # --- COO / Supply Chain: turns, and the whole purchase-to-pay run --------

    def _require_inventory(self, current_user: dict) -> None:
        """Stock reports read with inventory.view."""
        from app.core.roles import PERM_VIEW_INVENTORY

        if not has_permission(current_user["role"], PERM_VIEW_INVENTORY):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view inventory reports"
            )

    def inventory_turns(self, current_user: dict, days: int = 365) -> Dict:
        """How many times the stock turned over, and what could not be valued.

        Build Book, COO / Supply Chain: "inventory turns."

        Turns are cost of goods sold divided by average inventory value. Both
        halves are computed from the movement ledger rather than from a stored
        valuation, which is what makes the average honest:

          * COGS is the value of everything issued in the window. `issue` is
            the movement type for stock consumed — receipts, transfers and
            adjustments are not sales and are excluded, because counting a
            transfer between two of our own locations as a turn would let a
            warehouse improve this figure by shuffling boxes.
          * Average inventory is the mean of the opening and closing value.
            The opening balance is *reconstructed exactly* — current balance
            minus the net of every movement in the window — which is possible
            only because the ledger is append-only and never edited. A report
            that used closing value alone would flatter a business that had
            just run its stock down, and understate one that had just built up
            for a season.

        **Items with no standard_cost cannot be valued**, so the whole report
        describes the costed subset of the warehouse, and `uncosted` says how
        much is outside it.

        Worth being precise about why, because the obvious argument is wrong:
        costing an unknown item at zero would *not* distort the ratio. Zero
        contributes nothing to COGS and nothing to inventory value, so the
        quotient is identical either way — excluding and zeroing produce the
        same number. The problem is not manipulation, it is representativeness.
        A ratio computed over the costed items is only a fact about the
        business to the extent that most of the business is costed, and a
        reader cannot judge that without being told. Hence `uncosted`, and
        hence excluding rather than zeroing: the two give the same answer, and
        only one of them leaves a trace that the answer was partial.
        """
        self._require_inventory(current_user)
        since = _now() - timedelta(days=days)

        costs = dict(
            self.db.query(Item.id, Item.standard_cost)
            .filter(Item.standard_cost.isnot(None)).all()
        )
        uncosted_ids = [
            row[0] for row in
            self.db.query(Item.id).filter(Item.standard_cost.is_(None)).all()
        ]

        # Closing position, as the balances stand now.
        balances = self.db.query(
            StockBalance.item_id, func.sum(StockBalance.quantity)
        ).group_by(StockBalance.item_id).all()

        # Everything that moved inside the window, per item. Signed, so the
        # sum is the net change and subtracting it from the closing balance
        # gives the opening one exactly.
        net_movement = dict(
            self.db.query(StockMovement.item_id, func.sum(StockMovement.quantity))
            .filter(StockMovement.created_at >= since)
            .group_by(StockMovement.item_id).all()
        )
        issued = dict(
            self.db.query(StockMovement.item_id, func.sum(StockMovement.quantity))
            .filter(
                StockMovement.created_at >= since,
                StockMovement.movement_type == MOVE_ISSUE,
            )
            .group_by(StockMovement.item_id).all()
        )

        closing_value = 0.0
        opening_value = 0.0
        cogs = 0.0
        uncosted_units = 0.0

        for item_id, quantity in balances:
            units = float(quantity or 0)
            cost = costs.get(item_id)
            if cost is None:
                uncosted_units += units
                continue
            cost = money_to_float(cost)
            closing_value += units * cost
            opening_value += (units - float(net_movement.get(item_id, 0) or 0)) * cost

        for item_id, quantity in issued.items():
            cost = costs.get(item_id)
            if cost is not None:
                # Issues are negative; the value consumed is their magnitude.
                cogs += abs(float(quantity or 0)) * money_to_float(cost)

        average_value = (opening_value + closing_value) / 2.0
        # Annualised, so a 90-day window and a year are comparable. Turns is
        # conventionally a yearly figure and reporting a 90-day raw ratio
        # under the same name would read as a business turning over four times
        # more slowly than it is.
        turns = (
            round((cogs / average_value) * (365.0 / days), 2)
            if average_value > 0 else None
        )

        return {
            "window_days": days,
            "since": since.date().isoformat(),
            "cogs": round(cogs, 2),
            "opening_value": round(opening_value, 2),
            "closing_value": round(closing_value, 2),
            "average_value": round(average_value, 2),
            # None rather than 0.0 when there is no stock to turn: zero turns
            # reads as stock sitting dead, which is a different problem from
            # having none.
            "turns_per_year": turns,
            "days_of_stock": (
                round(365.0 / turns, 1) if turns and turns > 0 else None
            ),
            # How much of the warehouse the figures above do not cover.
            # See the docstring: the ratio is the same whether these are
            # excluded or zeroed, so this block is the disclosure, not a
            # defence against a distorted number.
            "uncosted": {
                "item_count": len(uncosted_ids),
                "units_on_hand": round(uncosted_units, 3),
            },
        }

    #: Purchase-to-pay, end to end. Each hop is a handover between two teams,
    #: which is where elapsed time actually accumulates — the work inside a
    #: step is rarely the problem.
    P2P_STEPS = [
        ("requisition_to_po", "Requisition approved to PO raised",
         "requisition", "approved", "purchase_order", "created"),
        ("po_to_receipt", "PO raised to goods received",
         "purchase_order", "created", "goods_receipt", "goods_received"),
        ("receipt_to_invoice", "Goods received to invoice entered",
         "goods_receipt", "goods_received", "invoice", "created"),
        ("invoice_to_approval", "Invoice entered to approved",
         "invoice", "created", "invoice", "approved"),
        ("approval_to_payment", "Invoice approved to payment released",
         "invoice", "approved", "payment", "released"),
    ]

    def p2p_cycle_time(self, current_user: dict, days: int = 180) -> Dict:
        """The whole run from "we need this" to "they were paid".

        Build Book, COO / Supply Chain: "P2P cycle."

        Every other cycle-time report in this system measures inside one
        module. This one is the only report that follows a single purchase
        across all five, and it can exist because every record in the chain
        carries the same `correlation_id` — the thing that makes an evidence
        pack possible makes this possible too.

        Reported per hop rather than end to end. An end-to-end median is a
        number nobody owns: it is the sum of five handovers between different
        teams, and the only actionable question is which handover is the slow
        one. The total is reported alongside, but as the sum of the parts
        rather than instead of them.

        A chain is only counted for a hop where both of its events exist, so a
        purchase still in flight contributes to the hops it has completed and
        to none after. Treating a missing end as "now" would make every
        in-flight purchase look like a delay and make the figure grow whenever
        business picked up.
        """
        self._require(current_user)
        since = _now() - timedelta(days=days)

        wanted = {(t, a) for _, _, t, a, _, _ in self.P2P_STEPS}
        wanted |= {(t, a) for _, _, _, _, t, a in self.P2P_STEPS}

        rows = (
            self.db.query(
                AuditLog.correlation_id, AuditLog.object_type, AuditLog.action,
                func.min(AuditLog.timestamp).label("at"),
            )
            .filter(
                AuditLog.correlation_id.isnot(None),
                AuditLog.timestamp >= since,
                tuple_(AuditLog.object_type, AuditLog.action).in_(wanted),
            )
            .group_by(AuditLog.correlation_id, AuditLog.object_type, AuditLog.action)
            .all()
        )

        chains: Dict[str, Dict[tuple, object]] = {}
        for correlation_id, object_type, action, at in rows:
            chains.setdefault(str(correlation_id), {})[(object_type, action)] = at

        durations: Dict[str, List[float]] = {k: [] for k, _, _, _, _, _ in self.P2P_STEPS}
        for marks in chains.values():
            for key, _label, from_type, from_action, to_type, to_action in self.P2P_STEPS:
                start = marks.get((from_type, from_action))
                end = marks.get((to_type, to_action))
                if start and end:
                    hours = (end - start).total_seconds() / 3600.0
                    if hours >= 0:
                        durations[key].append(hours)

        steps = [
            {
                "step": key,
                "label": label,
                "from": f"{from_type}.{from_action}",
                "to": f"{to_type}.{to_action}",
                "count": len(durations[key]),
                "median_days": _median_days(durations[key]),
                "worst_days": (
                    round(max(durations[key]) / 24.0, 1) if durations[key] else None
                ),
            }
            for key, label, from_type, from_action, to_type, to_action
            in self.P2P_STEPS
        ]

        measured = [s["median_days"] for s in steps if s["median_days"] is not None]
        return {
            "window_days": days,
            "chains_seen": len(chains),
            "steps": steps,
            # The sum of the medians, not the median of the totals. Stated
            # plainly because they are different numbers and the difference
            # matters: no single purchase necessarily took this long, and the
            # figure is here to be read as "the typical run, hop by hop".
            "typical_total_days": round(sum(measured), 1) if measured else None,
            "slowest_step": (
                max(
                    (s for s in steps if s["median_days"] is not None),
                    key=lambda s: s["median_days"], default=None,
                ) or {}
            ).get("step"),
        }
