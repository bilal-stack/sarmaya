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
from app.services.workflow import workflow_models
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

        escalated: List[Dict] = []

        # Every workflow that declares itself is scanned, not just invoices, so
        # a new module gets SLA escalation by declaring WORKFLOW_TYPE and
        # configuring SLAs on its states.
        for workflow_type, model in workflow_models().items():
            escalated.extend(
                self._escalate_workflow(workflow_type, model, current_user)
            )

        if escalated:
            self.db.commit()
        return {"escalated_count": len(escalated), "items": escalated}

    def _escalate_workflow(self, workflow_type: str, model, current_user: dict) -> List[Dict]:
        sla_map = {
            s.state_name: dict(s.sla or {})
            for s in self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == workflow_type)
            .all()
            if (s.sla or {}).get("hours") and (s.sla or {}).get("escalate_to")
        }
        if not sla_map:
            return []

        records = (
            self.db.query(model)
            .filter(model.current_state.in_(list(sla_map)))
            .all()
        )

        escalated: List[Dict] = []
        for invoice in records:
            due, overdue, cfg = sla_status(invoice, sla_map)
            if not overdue:
                continue
            if self._already_escalated(invoice, workflow_type):
                continue

            role = cfg["escalate_to"]
            hours = cfg["hours"]
            state = str(getattr(invoice.current_state, "value", invoice.current_state))
            log_audit(
                db=self.db,
                tenant_id=invoice.tenant_id,
                user_id=current_user["id"],
                object_type=workflow_type,
                object_id=invoice.id,
                action="sla_escalated",
                workflow_step=state,
                workflow_type=workflow_type,
                comment=f"SLA of {hours}h in {state} breached; escalated to {role.upper()}.",
                after_value={
                    "escalate_to": role,
                    "sla_hours": hours,
                    "due_at": due.isoformat() if due else None,
                },
            )
            self.notifications.notify_sla_escalation(invoice, role, hours)
            escalated.append({
                "object_type": workflow_type,
                # invoice_id/invoice_number are kept for the existing inbox
                # client; reference is the workflow-agnostic label.
                "invoice_id": str(invoice.id),
                "invoice_number": getattr(invoice, "invoice_number", None),
                "reference": getattr(invoice, "invoice_number", None)
                or getattr(invoice, "po_number", None)
                or str(invoice.id),
                "state": state,
                "escalated_to": role,
                "sla_due_at": due.isoformat() if due else None,
            })

        return escalated

    def _already_escalated(self, record, workflow_type: str) -> bool:
        """One escalation per state entry: has an sla_escalated event been
        recorded since this record entered its current state?

        The object_type must match what the escalation actually writes, which is
        the workflow type. Hardcoding "invoice" made this true only for
        invoices: every other workflow found no matching audit row, so each run
        escalated the same overdue record again and re-notified the approver.
        """
        entered = record.state_entered_at or record.updated_at or record.created_at
        if entered is None:
            return False
        entered_naive = make_naive(to_utc(entered))
        return (
            self.db.query(AuditLog)
            .filter(
                AuditLog.object_type == workflow_type,
                AuditLog.object_id == record.id,
                AuditLog.action == "sla_escalated",
                AuditLog.timestamp >= entered_naive,
            )
            .first()
            is not None
        )
