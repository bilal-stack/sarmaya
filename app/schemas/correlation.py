from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict
from datetime import datetime
from uuid import UUID


class ChainObject(BaseModel):
    """A business object belonging to a transaction chain."""
    object_type: str
    object_id: UUID
    reference: Optional[str] = None
    state: Optional[str] = None


class ChainEvent(BaseModel):
    """One thing that happened in the chain, from any record type."""
    at: Optional[datetime] = None
    kind: str                       # audit | policy_eval | ai_action
    object_type: Optional[str] = None
    object_id: Optional[UUID] = None
    summary: str
    actor: Optional[str] = None
    detail: Optional[str] = None


class TransactionChain(BaseModel):
    """The full story behind one correlation_id."""
    correlation_id: UUID
    objects: List[ChainObject]
    counts: Dict[str, int]
    total_events: int
    events: List[ChainEvent]

    model_config = ConfigDict(from_attributes=True)
