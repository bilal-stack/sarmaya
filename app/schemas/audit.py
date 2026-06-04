from pydantic import BaseModel
from typing import Optional, List, Any, Dict
from datetime import datetime
from uuid import UUID


class AuditTimelineEvent(BaseModel):
    """One event on an object's Live Audit timeline, with a derived plain-English
    summary of what happened and why."""
    timestamp: datetime
    action: str
    summary: str
    actor: Optional[str] = None
    actor_role: Optional[str] = None
    workflow_step: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    comment: Optional[str] = None
    document_hash: Optional[str] = None
    ai_assisted: bool = False
    ai_confidence: Optional[int] = None


class AuditTimeline(BaseModel):
    object_type: str
    object_id: UUID
    current_state: Optional[str] = None
    policy_reason: Optional[str] = None
    total_events: int
    events: List[AuditTimelineEvent]
