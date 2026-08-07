from sqlalchemy.orm import Session
from typing import Dict, Optional
from uuid import UUID
import logging

from app.models.workflow_state import WorkflowState
from app.services.workflow_guards import evaluate_guards
from app.core.enums import InvoiceState
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)


def workflow_type_of(obj) -> str:
    """The workflow governing a record, declared by its model as WORKFLOW_TYPE.

    Read from the object rather than passed in by the caller: the engine used
    to query workflow_type == "invoice" unconditionally, so a second module
    would have silently been governed by the invoice state machine. Declaring
    it on the model means a new module cannot forget to say which it is —
    omitting it raises here instead of quietly inheriting invoice rules.
    """
    workflow_type = getattr(obj, "WORKFLOW_TYPE", None)
    if not workflow_type:
        raise ValueError(
            f"{type(obj).__name__} does not declare WORKFLOW_TYPE, so the "
            "workflow engine cannot tell which state machine governs it."
        )
    return workflow_type


def workflow_models() -> Dict[str, type]:
    """{workflow_type: model} for every mapped model declaring WORKFLOW_TYPE.

    Discovered from the registry so a new module joins SLA scanning and any
    other cross-workflow sweep by declaring the attribute, with no list to
    update.
    """
    from app.models.base import mapper_registry

    return {
        mapper.class_.WORKFLOW_TYPE: mapper.class_
        for mapper in mapper_registry.mappers
        if getattr(mapper.class_, "WORKFLOW_TYPE", None)
    }


def _enter_state(obj, target: str) -> None:
    """Apply the state change and restart the SLA timer (Build Book: SLA
    timers start when a task enters a state)."""
    obj.current_state = target
    if hasattr(obj, "state_entered_at"):
        obj.state_entered_at = utc_now()


def transition_state(db: Session, obj, target_state: str, user_id: str | UUID) -> bool:
    """
    Validate and apply state transition using workflow_states table.

    Returns True on success. Raises ValueError if the transition is not allowed
    (consistent with the change_state fallback).
    """
    workflow_type = workflow_type_of(obj)
    current = (obj.current_state or InvoiceState.DRAFT.value).lower()
    target = target_state.lower()

    # Query workflow_states for allowed transitions
    current_workflow = db.query(WorkflowState).filter(
        WorkflowState.tenant_id == obj.tenant_id,
        WorkflowState.workflow_type == workflow_type,
        WorkflowState.state_name == current
    ).first()

    if not current_workflow:
        # Fallback to hardcoded rules if workflow_states not configured. The
        # fallback encodes the invoice state machine, so it is only safe for
        # invoices; any other workflow must be configured before it can move,
        # rather than inheriting transitions that were never meant for it.
        if workflow_type != "invoice":
            raise ValueError(
                f"No workflow configured for '{workflow_type}' state '{current}'. "
                "Seed its workflow_states before transitioning it."
            )
        return change_state(obj, target_state, db)

    allowed_transitions = current_workflow.allowed_transitions or []

    if target not in allowed_transitions:
        logger.warning(
            "Transition %s -> %s not allowed. Allowed: %s",
            current, target, allowed_transitions,
        )
        raise ValueError(f"Invalid state transition: {current} -> {target}")

    # Configurable guards: the transition fires only if all guards for this
    # target pass. Guard names are stored per target on the source state.
    guard_names = (current_workflow.guards or {}).get(target, [])
    ok, reason = evaluate_guards(db, obj, guard_names)
    if not ok:
        logger.warning("Transition %s -> %s blocked by guard: %s", current, target, reason)
        raise ValueError(reason)

    _enter_state(obj, target)
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

    _enter_state(invoice, target)
    db.add(invoice)
    return True


def get_allowed_transitions(
    db: Session,
    tenant_id: UUID,
    current_state: str,
    workflow_type: str = "invoice",
) -> list[str]:
    """Allowed transitions out of a state, for the named workflow.

    The fallback below is the invoice state machine, so it is only returned
    for invoices; another workflow with no configuration has no transitions
    rather than borrowing the invoice ones.
    """
    workflow = db.query(WorkflowState).filter(
        WorkflowState.tenant_id == tenant_id,
        WorkflowState.workflow_type == workflow_type,
        WorkflowState.state_name == current_state.lower()
    ).first()
    
    if workflow:
        return workflow.allowed_transitions or []

    if workflow_type != "invoice":
        return []

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
