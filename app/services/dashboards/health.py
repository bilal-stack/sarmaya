"""Whether the system's own controls are working.

Evidence completeness, reconciliation health, autopilot reversals, and
the combined overview the landing page reads.
"""
from datetime import timedelta
from typing import Dict

from sqlalchemy import func

from app.core.enums import (
    InvoiceState,
)
from app.models.ai_action_log import AIActionLog
from app.models.audit_log import AuditLog
from app.models.bank_statement import BankStatementLine
from app.models.invoice import Invoice
from app.models.watchlist_alert import WatchlistAlert
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    AGE_BUCKETS, DashboardBase, _bucket, _now,
)


class HealthReports(DashboardBase):
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

