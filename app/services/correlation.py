"""Universal correlation IDs.

Build Book: "Every transaction chain carries a correlation_id that links every
record across modules. Search must support correlation_id to reconstruct the
entire story instantly."

Today an AP chain starts at the invoice. When procurement lands, a PR will mint
the id and the PO, GRN and invoice will inherit it — `resolve_correlation_id`
is the single place that mapping lives, so the rest of the code never needs to
know which object type starts a chain.
"""
import logging
import uuid
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.audit_log import AuditLog
from app.models.policy_eval import PolicyEval
from app.models.ai_action_log import AIActionLog
from app.core.roles import has_permission, PERM_VIEW_AUDIT, PERM_VIEW_INVOICE

logger = logging.getLogger(__name__)

# object_type -> the model that owns the chain id for that object.
_CHAIN_OWNERS = {"invoice": Invoice}


def new_correlation_id() -> uuid.UUID:
    """Mint a new chain id. Called when a transaction story begins."""
    return uuid.uuid4()


def resolve_correlation_id(db: Session, object_type: Optional[str], object_id) -> Optional[uuid.UUID]:
    """The chain id for an object, or None if it has none / isn't a chain root.
    Best-effort: never raise into the caller's write path."""
    model = _CHAIN_OWNERS.get((object_type or "").lower())
    if model is None or object_id is None:
        return None
    try:
        row = db.query(model).filter(model.id == object_id).first()
        return getattr(row, "correlation_id", None) if row else None
    except Exception:
        logger.exception("Could not resolve correlation_id for %s %s", object_type, object_id)
        return None


class CorrelationService:
    """Reconstructs a full transaction story from its correlation_id."""

    def __init__(self, db: Session):
        self.db = db

    def get_chain(self, correlation_id, current_user: dict) -> Dict:
        role = current_user["role"]
        if not (has_permission(role, PERM_VIEW_AUDIT) or has_permission(role, PERM_VIEW_INVOICE)):
            raise PermissionError("You do not have permission to view transaction chains")

        invoices = (
            self.db.query(Invoice)
            .filter(Invoice.correlation_id == correlation_id)
            .order_by(Invoice.created_at.asc())
            .all()
        )
        audit = (
            self.db.query(AuditLog)
            .filter(AuditLog.correlation_id == correlation_id)
            .order_by(AuditLog.timestamp.asc())
            .all()
        )
        evals = (
            self.db.query(PolicyEval)
            .filter(PolicyEval.correlation_id == correlation_id)
            .order_by(PolicyEval.created_at.asc())
            .all()
        )
        ai = (
            self.db.query(AIActionLog)
            .filter(AIActionLog.correlation_id == correlation_id)
            .order_by(AIActionLog.created_at.asc())
            .all()
        )

        events: List[Dict] = [
            {
                "at": a.timestamp,
                "kind": "audit",
                "object_type": a.object_type,
                "object_id": a.object_id,
                "summary": a.action,
                "actor": a.user_email,
                "detail": a.comment,
            }
            for a in audit
        ] + [
            {
                "at": e.created_at,
                "kind": "policy_eval",
                "object_type": e.object_type,
                "object_id": e.object_id,
                "summary": f"{e.policy_key} → {(e.output or {}).get('required_role')}",
                "actor": None,
                "detail": (e.reasons or [None])[0],
            }
            for e in evals
        ] + [
            {
                "at": l.created_at,
                "kind": "ai_action",
                "object_type": l.object_type,
                "object_id": l.object_id,
                "summary": f"{l.action} ({l.status})",
                "actor": l.ai_model,
                "detail": l.output_summary,
            }
            for l in ai
        ]
        events.sort(key=lambda e: (e["at"] is None, e["at"]))

        return {
            "correlation_id": correlation_id,
            "objects": [
                {
                    "object_type": "invoice",
                    "object_id": inv.id,
                    "reference": inv.invoice_number,
                    "state": getattr(inv.current_state, "value", inv.current_state),
                }
                for inv in invoices
            ],
            "counts": {
                "audit_events": len(audit),
                "policy_evaluations": len(evals),
                "ai_actions": len(ai),
            },
            "total_events": len(events),
            "events": events,
        }
