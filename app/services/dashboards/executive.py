"""The Executive Control Room and the three reports behind it.

Build Book lines 265-272. What is stuck, why, and what it is worth —
then the cycle times, exception causes and overrides that explain it.
"""
from datetime import timedelta
from typing import Dict, List

from sqlalchemy import func

from app.core.enums import (
    InvoiceState, PaymentState, VendorStatus,
)
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    AGE_BUCKETS, DashboardBase, _bucket, _now,
)


class ExecutiveReports(DashboardBase):
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

