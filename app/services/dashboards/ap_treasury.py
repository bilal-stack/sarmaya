"""AP and Treasury: what is on the desk today.

Invoice throughput, payment run status, the duplicate check's catches,
and the segregation failures the controls refused. Two figures are
deliberately NOT reported and say so in the payload rather than showing
a zero — see invoice_throughput and payment_run_status.
"""
from datetime import timedelta
from typing import Dict

from sqlalchemy import func

from app.core.enums import (
    InvoiceState, PaymentState,
)
from app.core.roles import has_permission
from app.models.audit_log import AuditLog
from app.models.bank_statement import BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.watchlist_alert import WatchlistAlert
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    BLOCK_REASONS, DashboardBase, _now, _reason_from_comment,
)


class ApTreasuryReports(DashboardBase):
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

