"""Procurement Leadership.

RFQ cycle time, reported per stage because three of the four are delays
we own and one is a window we chose to give vendors; and savings against
two baselines, because neither alone is honest.
"""
from datetime import timedelta
from typing import Dict, List

from sqlalchemy import func

from app.core.enums import (
    RFQState,
)
from app.core.roles import has_permission
from app.models.audit_log import AuditLog
from app.models.rfq import RFQ, RFQVendor, Quote
from app.models.requisition import PurchaseRequisition
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    DashboardBase, _median_days,
    _now,
)


class ProcurementReports(DashboardBase):
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

