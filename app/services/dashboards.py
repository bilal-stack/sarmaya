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

One deliberate omission: none of these are cached. They are aggregate queries
over tens of thousands of rows at the scale this product targets, and a cache
is the thing that eventually shows somebody a number that is no longer true —
which for "what is stuck and what is it costing" is worse than a slow page.
When it stops being fast enough, the answer is a read replica or a materialised
view with an explicit refresh, not a TTL nobody remembers.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from sqlalchemy import Float, case, func
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

        hours = (
            func.extract("epoch", decided.c.at - submitted.c.at) / 3600.0
        ).label("hours")
        rows = (
            self.db.query(decided.c.role, hours)
            .join(submitted, submitted.c.object_id == decided.c.object_id)
            .filter(decided.c.at >= submitted.c.at)
            .all()
        )

        by_role: Dict[str, List[float]] = {}
        for role, taken in rows:
            by_role.setdefault(role or "unknown", []).append(float(taken))

        steps = []
        for role, values in sorted(by_role.items(), key=lambda kv: -len(kv[1])):
            values.sort()
            steps.append({
                "step": "approval",
                "role": role,
                "decisions": len(values),
                "median_hours": round(values[len(values) // 2], 1),
                "average_hours": round(sum(values) / len(values), 1),
                "slowest_hours": round(values[-1], 1),
            })

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
