from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID


class WatchlistAlertResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    category: str            # vendor_bank_change | master_data_edit | policy_override
    severity: str            # high | medium
    object_type: str
    object_id: UUID
    summary: str
    #: Before/after where there is one. Account numbers are masked here as
    #: everywhere else — the auditor is on this list.
    detail: Optional[Dict[str, Any]] = None
    actor_id: Optional[UUID] = None
    acknowledged_by: Optional[UUID] = None
    acknowledged_at: Optional[datetime] = None
    acknowledgement_note: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WatchlistFeed(BaseModel):
    open_count: int
    items: List[WatchlistAlertResponse]


class AcknowledgeRequest(BaseModel):
    """What the reviewer concluded. Optional, but the field exists because an
    acknowledgement with no note records that somebody clicked, which is not
    the same as somebody checking."""
    note: Optional[str] = None
