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
from typing import Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.enums import InvoiceState, PaymentState, VendorStatus
from app.core.roles import has_permission, PERM_VIEW_INVOICE
from app.models.ai_action_log import AIActionLog
from app.models.audit_log import AuditLog
from app.models.bank_statement import BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.vendor import Vendor
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
            self.db.query(AuditLog.action, AuditLog.comment, AuditLog.after_value)
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.action.in_(["rejected", "approval_blocked"]),
                AuditLog.timestamp >= since,
            )
            .all()
        )
        drivers: Dict[str, int] = {}
        for _action, comment, after in rework:
            reason = (after or {}).get("reason") or comment or "no reason given"
            drivers[reason[:120]] = drivers.get(reason[:120], 0) + 1

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
            "rework_events": len(rework),
            "rework_rate_pct": (
                round(len(rework) * 100.0 / captured, 1) if captured else 0.0
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
