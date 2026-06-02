from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, List
from uuid import UUID


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
    color: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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
