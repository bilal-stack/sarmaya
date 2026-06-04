from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID

from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.services.policy import explain_approval_routing
from app.core.roles import (
    has_permission,
    PERM_VIEW_INVOICE,
    PERM_VIEW_VENDORS,
    PERM_VIEW_AUDIT,
)

# object_type -> permission required to view its timeline. Live Audit Mode is
# transparent to whoever can already see the object; anything else needs audit.view.
_VIEW_PERMISSION = {
    "invoice": PERM_VIEW_INVOICE,
    "vendor": PERM_VIEW_VENDORS,
}


class AuditService:
    """Builds an object's Live Audit timeline: a chronological history where each
    event carries a derived, human-readable reason (including the policy reason
    behind approval routing)."""

    def __init__(self, db: Session):
        self.db = db

    def get_timeline(self, object_type: str, object_id: UUID, current_user: dict) -> dict:
        self._require_view(object_type, current_user)

        logs = (
            self.db.query(AuditLog)
            .filter(AuditLog.object_type == object_type, AuditLog.object_id == object_id)
            .order_by(AuditLog.timestamp.asc())
            .all()
        )

        events = [self._to_event(log) for log in logs]

        current_state: Optional[str] = None
        policy_reason: Optional[str] = None
        if object_type == "invoice":
            invoice = self.db.query(Invoice).filter(Invoice.id == object_id).first()
            if invoice:
                current_state = invoice.current_state
                # Prefer the routing snapshot recorded at the last decision, so
                # the timeline reflects the policy actually applied — not what it
                # would be recomputed to under a since-edited policy. Only project
                # from the current policy when no decision has been made yet.
                snapshot = self._latest_routing_reason(logs)
                if snapshot:
                    policy_reason = snapshot
                else:
                    policy_reason = explain_approval_routing(
                        self.db, current_user["tenant_id"], float(invoice.total_amount or 0)
                    )["reason"]

        return {
            "object_type": object_type,
            "object_id": object_id,
            "current_state": current_state,
            "policy_reason": policy_reason,
            "total_events": len(events),
            "events": events,
        }

    # --- event rendering ----------------------------------------------------

    def _to_event(self, log: AuditLog) -> dict:
        return {
            "timestamp": log.timestamp,
            "action": log.action,
            "summary": self._summarize(log),
            "actor": log.user_email,
            "actor_role": str(log.user_role) if log.user_role is not None else None,
            "workflow_step": log.workflow_step,
            "before": log.before_value,
            "after": log.after_value,
            "comment": log.comment,
            "document_hash": log.document_hash,
            "ai_assisted": bool(log.ai_assisted),
            "ai_confidence": log.ai_confidence,
        }

    @staticmethod
    def _latest_routing_reason(logs) -> Optional[str]:
        """The routing reason snapshotted at the most recent approval decision."""
        for log in reversed(logs):
            after = log.after_value or {}
            if after.get("routing_reason"):
                return after["routing_reason"]
        return None

    @staticmethod
    def _summarize(log: AuditLog) -> str:
        action = (log.action or "").lower()
        after = log.after_value or {}
        before = log.before_value or {}

        if action == "created":
            if log.ai_assisted:
                conf = f" ({log.ai_confidence}% OCR confidence)" if log.ai_confidence is not None else " via OCR"
                return f"Created from an uploaded document{conf}."
            return "Created."
        if action == "validated":
            return "Validated — all required fields present."
        if action == "submitted_for_approval":
            reason = after.get("routing_reason")
            if reason:
                return f"Submitted for approval. {reason}"
            role = after.get("required_role")
            return (
                f"Submitted for approval; routed to {role.upper()}."
                if role else "Submitted for approval."
            )
        if action == "approved":
            return "Approved."
        if action == "auto_approved":
            return log.comment or "Auto-approved by Restricted Autopilot."
        if action == "auto_approval_reverted":
            return "Autopilot approval reverted to pending for manual review."
        if action == "rejected":
            return f"Rejected: {log.comment}" if log.comment else "Rejected."
        if action == "duplicate_overridden":
            return (
                f"Flagged duplicate overridden with reason: {log.comment}"
                if log.comment else "Flagged duplicate overridden."
            )
        if action in ("approval_blocked", "validation_blocked"):
            return f"Blocked: {log.comment}" if log.comment else "Action blocked by a governance gate."
        if action in ("marked_paid", "paid"):
            return "Marked as paid."
        if action == "status_changed":
            return f"Status changed from {before.get('status')} to {after.get('status')}."
        if action == "transitions_updated":
            return "Workflow transitions reconfigured."
        return (log.action or "event").replace("_", " ").capitalize() + "."

    @staticmethod
    def _require_view(object_type: str, current_user: dict) -> None:
        required = _VIEW_PERMISSION.get(object_type, PERM_VIEW_AUDIT)
        if not has_permission(current_user["role"], required):
            raise PermissionError("You do not have permission to view this audit timeline")
