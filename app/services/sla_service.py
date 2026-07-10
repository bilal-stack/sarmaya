"""SLA escalation runner.

Scans invoices sitting past their state's configured SLA and escalates each
breach once: an `sla_escalated` audit event (preserving the original approver
chain — the routing snapshot stays in the trail) plus a notification to the
escalation role. Idempotent per state entry: an invoice already escalated since
it entered its current state is skipped, so the runner can be invoked freely
(admin button, cron, or a scheduler later — DR-009).
"""
import logging
from typing import Dict, List

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.workflow_state import WorkflowState
from app.models.audit_log import AuditLog
from app.services.audit import log_audit
from app.services.decision_inbox_service import sla_status
from app.services.notification_service import NotificationService
from app.core.roles import has_permission, PERM_MANAGE_WORKFLOW
from app.utils.datetime_helpers import to_utc, make_naive

logger = logging.getLogger(__name__)


class SlaService:
    def __init__(self, db: Session):
        self.db = db
        self.notifications = NotificationService(db)

    def run_escalations(self, current_user: dict) -> Dict:
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to run SLA escalations")

        sla_map = {
            s.state_name: dict(s.sla or {})
            for s in self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == "invoice")
            .all()
            if (s.sla or {}).get("hours") and (s.sla or {}).get("escalate_to")
        }
        if not sla_map:
            return {"escalated_count": 0, "items": []}

        invoices = (
            self.db.query(Invoice)
            .filter(Invoice.current_state.in_(list(sla_map)))
            .all()
        )

        escalated: List[Dict] = []
        for invoice in invoices:
            due, overdue, cfg = sla_status(invoice, sla_map)
            if not overdue:
                continue
            if self._already_escalated(invoice):
                continue

            role = cfg["escalate_to"]
            hours = cfg["hours"]
            state = str(getattr(invoice.current_state, "value", invoice.current_state))
            log_audit(
                db=self.db,
                tenant_id=invoice.tenant_id,
                user_id=current_user["id"],
                object_type="invoice",
                object_id=invoice.id,
                action="sla_escalated",
                workflow_step=state,
                workflow_type="invoice",
                comment=f"SLA of {hours}h in {state} breached; escalated to {role.upper()}.",
                after_value={
                    "escalate_to": role,
                    "sla_hours": hours,
                    "due_at": due.isoformat() if due else None,
                },
            )
            self.notifications.notify_sla_escalation(invoice, role, hours)
            escalated.append({
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "state": state,
                "escalated_to": role,
                "sla_due_at": due.isoformat() if due else None,
            })

        if escalated:
            self.db.commit()
        return {"escalated_count": len(escalated), "items": escalated}

    def _already_escalated(self, invoice: Invoice) -> bool:
        """One escalation per state entry: has an sla_escalated event been
        recorded since the invoice entered its current state?"""
        entered = invoice.state_entered_at or invoice.updated_at or invoice.created_at
        if entered is None:
            return False
        entered_naive = make_naive(to_utc(entered))
        return (
            self.db.query(AuditLog)
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.object_id == invoice.id,
                AuditLog.action == "sla_escalated",
                AuditLog.timestamp >= entered_naive,
            )
            .first()
            is not None
        )
