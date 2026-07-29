"""Policy Simulator — what-if analysis before a rule change goes live.

Build Book: "UI in Admin Console to simulate a policy change against historical
sample data. Outputs include predicted changes in approval volume, exception
volume, SLA risk, and autopilot eligibility."

Replays a proposed approval matrix over the tenant's historical invoices and
reports what would have changed: how routing shifts between roles, which
invoices move and by how much value, and how autopilot eligibility is affected.

Strictly read-only. Nothing is written, no policy is touched, and the routing is
computed by the same `evaluate_rules` the live path uses — a simulation that
disagreed with production would be worse than none.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.core.enums import VendorStatus
from app.core.roles import has_permission, PERM_MANAGE_POLICIES
from app.services.policy import evaluate_rules, active_approval_rules
from app.utils.money import money_to_float
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)


class PolicySimulator:
    """Replays a proposed approval matrix against historical invoices."""

    def __init__(self, db: Session):
        self.db = db

    def simulate(
        self,
        current_user: dict,
        proposed_rules: List[Dict],
        window_days: int = 90,
        autopilot_limit: Optional[float] = None,
    ) -> Dict:
        if not has_permission(current_user["role"], PERM_MANAGE_POLICIES):
            raise PermissionError("You do not have permission to simulate policy changes")
        if window_days <= 0:
            raise ValueError("window_days must be positive")

        # Proposed rules are evaluated highest-priority-first, exactly as live
        # rules are, so ordering can't be a source of difference on its own.
        proposed = sorted(
            proposed_rules or [], key=lambda r: r.get("priority", 0), reverse=True
        )
        current = active_approval_rules(self.db, current_user["tenant_id"])

        since = utc_now() - timedelta(days=window_days)
        rows = (
            self.db.query(Invoice, Vendor)
            .outerjoin(Vendor, Invoice.vendor_id == Vendor.id)
            .filter(Invoice.created_at >= since.replace(tzinfo=None))
            .all()
        )

        before_counts: Dict[str, int] = {}
        after_counts: Dict[str, int] = {}
        before_value: Dict[str, float] = {}
        after_value: Dict[str, float] = {}
        changed: List[Dict] = []
        autopilot_eligible = 0

        for invoice, vendor in rows:
            amount = money_to_float(invoice.total_amount)
            now = evaluate_rules(current, amount)
            then = evaluate_rules(proposed, amount)

            a, b = now["required_role"], then["required_role"]
            before_counts[a] = before_counts.get(a, 0) + 1
            after_counts[b] = after_counts.get(b, 0) + 1
            before_value[a] = round(before_value.get(a, 0.0) + amount, 2)
            after_value[b] = round(after_value.get(b, 0.0) + amount, 2)

            if a != b:
                changed.append({
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                    "amount": amount,
                    "from_role": a,
                    "to_role": b,
                    "new_reason": then["reason"],
                })

            # Autopilot has its own limit, independent of the approval matrix,
            # so this is a separate what-if: under the proposed limit, how many
            # of these invoices would have been safe to auto-approve? Only
            # clean ones ever qualify (active vendor, no open duplicate).
            if autopilot_limit is not None:
                clean = (
                    vendor is not None
                    and vendor.status == VendorStatus.ACTIVE
                    and not (invoice.potential_duplicate_id and not invoice.duplicate_acknowledged)
                )
                if clean and amount <= autopilot_limit:
                    autopilot_eligible += 1

        result = {
            "window_days": window_days,
            "invoices_evaluated": len(rows),
            "routing_before": {"counts": before_counts, "value": before_value},
            "routing_after": {"counts": after_counts, "value": after_value},
            "changed_count": len(changed),
            "changed_value": round(sum(c["amount"] for c in changed), 2),
            "changes": sorted(changed, key=lambda c: -c["amount"])[:100],
            "net_by_role": {
                role: after_counts.get(role, 0) - before_counts.get(role, 0)
                for role in set(before_counts) | set(after_counts)
            },
        }
        if autopilot_limit is not None:
            result["autopilot_eligible"] = {
                "limit": autopilot_limit,
                "count": autopilot_eligible,
                "share": round(autopilot_eligible / len(rows), 3) if rows else 0.0,
            }
        return result
