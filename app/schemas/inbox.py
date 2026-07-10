from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime
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
    # SLA (Build Book: timers + escalation visible on every work item)
    sla_due_at: Optional[datetime] = None
    overdue: bool = False
    escalated: bool = False               # visible to you via SLA escalation


class DecisionInbox(BaseModel):
    total: int
    counts: Dict[str, int]                # per-category counts
    overdue_count: int = 0                # SLA-breached items in this page
    items: List[DecisionInboxItem]
