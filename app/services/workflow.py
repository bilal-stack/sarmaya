from sqlalchemy.orm import Session
from typing import Optional
from uuid import UUID
import logging

from app.models.workflow_state import WorkflowState
from app.core.enums import InvoiceState

logger = logging.getLogger(__name__)


def transition_state(db: Session, obj, target_state: str, user_id: str | UUID) -> bool:
    """
    Validate and apply state transition using workflow_states table.

    Returns True on success. Raises ValueError if the transition is not allowed
    (consistent with the change_state fallback).
    """
    current = (obj.current_state or InvoiceState.DRAFT.value).lower()
    target = target_state.lower()

    # Query workflow_states for allowed transitions
    current_workflow = db.query(WorkflowState).filter(
        WorkflowState.tenant_id == obj.tenant_id,
        WorkflowState.workflow_type == "invoice",
        WorkflowState.state_name == current
    ).first()

    if not current_workflow:
        # Fallback to hardcoded rules if workflow_states not configured
        return change_state(obj, target_state, db)

    allowed_transitions = current_workflow.allowed_transitions or []

    if target not in allowed_transitions:
        logger.warning(
            "Transition %s -> %s not allowed. Allowed: %s",
            current, target, allowed_transitions,
        )
        raise ValueError(f"Invalid state transition: {current} -> {target}")

    obj.current_state = target
    db.add(obj)
    return True


def change_state(invoice, target_state: str, db: Session):
    """
    Simple workflow state machine (fallback/legacy)
    """
    ALLOWED_TRANSITIONS = {
        InvoiceState.DRAFT.value: {
            InvoiceState.VALIDATED.value,
            InvoiceState.CANCELLED.value
        },
        InvoiceState.VALIDATED.value: {
            InvoiceState.PENDING_APPROVAL.value,
            InvoiceState.CANCELLED.value
        },
        InvoiceState.PENDING_APPROVAL.value: {
            InvoiceState.APPROVED.value,
            InvoiceState.REJECTED.value
        },
        InvoiceState.APPROVED.value: {
            InvoiceState.PAID.value,
            InvoiceState.CANCELLED.value
        },
        InvoiceState.REJECTED.value: {
            InvoiceState.DRAFT.value
        },
        InvoiceState.PAID.value: set(),
        InvoiceState.CANCELLED.value: set(),
    }
    
    current = (invoice.current_state or InvoiceState.DRAFT.value).lower()
    target = target_state.lower()
    
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    

    if target not in allowed:
        raise ValueError(f"Invalid state transition: {current} -> {target}")
    
    invoice.current_state = target
    db.add(invoice)
    return True


def get_allowed_transitions(db: Session, tenant_id: UUID, current_state: str) -> list[str]:
    """Helper: get allowed transitions for current state"""
    workflow = db.query(WorkflowState).filter(
        WorkflowState.tenant_id == tenant_id,
        WorkflowState.workflow_type == "invoice",
        WorkflowState.state_name == current_state.lower()
    ).first()
    
    if workflow:
        return workflow.allowed_transitions or []
    
    # Fallback with ALL states
    FALLBACK_TRANSITIONS = {
        InvoiceState.DRAFT.value: [
            InvoiceState.VALIDATED.value,
            InvoiceState.CANCELLED.value
        ],
        InvoiceState.VALIDATED.value: [
            InvoiceState.PENDING_APPROVAL.value,
            InvoiceState.CANCELLED.value
        ],
        InvoiceState.PENDING_APPROVAL.value: [
            InvoiceState.APPROVED.value,
            InvoiceState.REJECTED.value
        ],
        InvoiceState.APPROVED.value: [
            InvoiceState.PAID.value,
            InvoiceState.CANCELLED.value
        ],
        InvoiceState.REJECTED.value: [
            InvoiceState.DRAFT.value
        ],
        InvoiceState.PAID.value: [],
        InvoiceState.CANCELLED.value: [],
    }
    
    return FALLBACK_TRANSITIONS.get(current_state.lower(), [])


def start_workflow(name: str, payload: dict):
    return {"workflow": name, "status": "started"}
