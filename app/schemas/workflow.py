from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, List, Dict
from uuid import UUID

from app.core.roles import is_valid_role


class WorkflowStateResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    workflow_type: str
    state_name: str
    display_name: Optional[str] = None
    state_order: int
    is_initial: bool
    is_final: bool
    allowed_transitions: List[str] = []
    guards: Dict[str, List[str]] = {}
    sla: Dict = {}
    color: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowSlaUpdate(BaseModel):
    """Set (or clear) the SLA for sitting in a state. Both fields empty clears
    the SLA; hours without escalate_to means overdue-tracking only."""
    hours: Optional[int] = None
    escalate_to: Optional[str] = None

    @field_validator("hours")
    @classmethod
    def hours_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("hours must be positive")
        return v

    @field_validator("escalate_to")
    @classmethod
    def role_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not is_valid_role(v):
            raise ValueError(f"'{v}' is not a valid role")
        return v


class WorkflowTransitionsUpdate(BaseModel):
    """Replace the set of states a given state may transition to. Targets are
    normalised to lowercase to match how transition_state looks them up."""
    allowed_transitions: List[str]

    @field_validator("allowed_transitions")
    @classmethod
    def normalise(cls, v: List[str]) -> List[str]:
        cleaned = []
        for t in v:
            name = (t or "").strip().lower()
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned
