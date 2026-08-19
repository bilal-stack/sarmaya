"""SLA escalation runner.

Scans invoices sitting past their state's configured SLA and escalates each
breach once: an `sla_escalated` audit event (preserving the original approver
chain — the routing snapshot stays in the trail) plus a notification to the
escalation role. Idempotent per state entry: an invoice already escalated since
it entered its current state is skipped, so the runner can be invoked freely
(admin button, cron, or a scheduler later — DR-009).
"""
import logging
from datetime import timedelta
from typing import Dict, List

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.workflow_state import WorkflowState
from app.models.audit_log import AuditLog
from app.services.audit import log_audit
from app.services.decision_inbox_service import sla_status
from app.services.workflow import workflow_models
from app.services.notification_service import NotificationService
from app.core.config import settings
from app.core.roles import has_permission, PERM_MANAGE_WORKFLOW
from app.utils.datetime_helpers import utc_now, to_utc, make_naive
from app.utils.records import record_reference

logger = logging.getLogger(__name__)


#: Who can act on each workflow's waiting state. Escalation knows the role to
#: escalate *to* from the SLA config; a reminder has to reach whoever can act
#: now, which is a capability rather than a role — so it is resolved by
#: permission, the same way notify_awaiting_action does it.
ACTOR_PERMISSION = {
    "invoice": "invoices.approve",
    "requisition": "requisitions.approve",
    "rfq": "sourcing.award",
    "purchase_order": "purchase_orders.approve",
    "payment": "payments.release",
}


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
                # Read from the model's own REFERENCE_FIELD. The hardcoded
                # invoice_number-or-po_number fallback here was the same shape
                # of bug DR-033 fixed elsewhere: every workflow outside that
                # pair reported a raw UUID, so an escalation about RFQ-SLA
                # arrived naming a number nobody can look up.
                "reference": record_reference(invoice),
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

    # --- reminders -----------------------------------------------------------

    def run_reminders(self, current_user: dict) -> Dict:
        """Nudge whoever can act on something still waiting.

        Distinct from escalation, and deliberately so. Escalation fires once,
        at breach, to a *higher* role — it is an alarm. A reminder fires before
        breach, to the same people who could have acted all along, and its
        purpose is that the alarm never has to go off.

        Sending one only after a quiet interval, and recording it, keeps this
        from becoming the thing people filter to a folder. Anything already
        overdue is left alone: it is escalation's problem by then, and two
        messages about the same lateness is how both get ignored.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_WORKFLOW):
            raise PermissionError("You do not have permission to send reminders")

        reminded: List[Dict] = []
        for workflow_type, model in workflow_models().items():
            reminded.extend(
                self._remind_workflow(workflow_type, model, current_user)
            )
        if reminded:
            self.db.commit()
        return {"reminded_count": len(reminded), "items": reminded}

    def _remind_workflow(self, workflow_type: str, model, current_user: dict) -> List[Dict]:
        permission = ACTOR_PERMISSION.get(workflow_type)
        if not permission:
            return []

        sla_map = {
            s.state_name: dict(s.sla or {})
            for s in self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == workflow_type)
            .all()
            if (s.sla or {}).get("hours")
        }
        if not sla_map:
            return []

        records = (
            self.db.query(model)
            .filter(model.current_state.in_(list(sla_map)))
            .all()
        )

        interval = timedelta(hours=settings.REMINDER_INTERVAL_HOURS)
        reminded: List[Dict] = []
        for record in records:
            due, overdue, cfg = sla_status(record, sla_map)
            if overdue:
                continue  # escalation's problem now
            if self._reminded_recently(record, workflow_type, interval):
                continue

            entered = record.state_entered_at or record.updated_at or record.created_at
            if entered is None:
                continue
            waiting = utc_now() - to_utc(entered)

            # Nudge once a fraction of the deadline has gone, not after a fixed
            # number of hours. A fixed interval cannot work: with the interval
            # at 24h and a 24h SLA, the only moment a reminder could fire is the
            # moment the item goes overdue and escalation takes it — so the
            # reminder would be dead code for every workflow whose SLA is at or
            # under the interval, which is most of them.
            threshold = timedelta(
                hours=cfg["hours"] * settings.REMINDER_AT_SLA_FRACTION
            )
            if waiting < threshold:
                continue  # still comfortably inside the deadline

            state = str(getattr(record.current_state, "value", record.current_state))
            log_audit(
                db=self.db,
                tenant_id=record.tenant_id,
                user_id=current_user["id"],
                object_type=workflow_type,
                object_id=record.id,
                action="reminder_sent",
                workflow_step=state,
                workflow_type=workflow_type,
                comment=(
                    f"Still waiting in {state} after "
                    f"{int(waiting.total_seconds() // 3600)}h; reminded."
                ),
            )
            self.notifications.notify_awaiting_action(
                record, permission, "approve or reject",
                exclude_user_id=getattr(record, "created_by", None),
            )
            reminded.append({
                "object_type": workflow_type,
                "reference": record_reference(record),
                "state": state,
                "waiting_hours": int(waiting.total_seconds() // 3600),
                "sla_due_at": due.isoformat() if due else None,
            })
        return reminded

    def _reminded_recently(self, record, workflow_type: str, interval) -> bool:
        """One reminder per interval, per state entry.

        Same shape as the escalation idempotency check, and matched on the
        workflow type for the same reason: hardcoding "invoice" there meant
        every other workflow re-notified on every run.
        """
        since = make_naive(to_utc(utc_now() - interval))
        entered = record.state_entered_at or record.updated_at or record.created_at
        if entered is not None:
            entered_naive = make_naive(to_utc(entered))
            since = max(since, entered_naive)
        return (
            self.db.query(AuditLog)
            .filter(
                AuditLog.object_type == workflow_type,
                AuditLog.object_id == record.id,
                AuditLog.action == "reminder_sent",
                AuditLog.timestamp >= since,
            )
            .first()
            is not None
        )
