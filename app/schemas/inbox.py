from pydantic import BaseModel
from typing import Optional, List, Dict
from uuid import UUID


class DecisionInboxItem(BaseModel):
    """A single actionable item: one invoice surfaced by its most blocking next
    step, with why it's here and where to act."""
    category: str          # duplicate_review | vendor_verification | approval
    priority: int          # 1 = most urgent
    action: str            # human label for the next step
    reason: str            # why this needs attention now
    invoice_id: UUID
    invoice_number: Optional[str] = None
    vendor_name: Optional[str] = None
    amount: float
    current_state: Optional[str] = None
    required_role: Optional[str] = None   # for approval items
    timeline_url: str                     # Live Audit Mode link


class DecisionInbox(BaseModel):
    total: int
    counts: Dict[str, int]                # per-category counts
    items: List[DecisionInboxItem]
