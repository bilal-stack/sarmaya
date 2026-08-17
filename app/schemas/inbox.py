from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime
from uuid import UUID


class DecisionInboxItem(BaseModel):
    """A single actionable item from any module, surfaced by its most blocking
    next step, with why it is here and where to act.

    The identity fields are deliberately neutral. They named an invoice while
    the inbox only read invoices; every other module then had to either
    misreport itself as one or stay out of the inbox entirely.
    """
    category: str          # duplicate_review | payment_release | ... (9 in all)
    work_item_type: str    # approval | exception | review | reconciliation | admin
    priority: int          # lower sorts first
    action: str            # human label for the next step
    reason: str            # why this needs attention now
    object_type: str       # invoice | requisition | payment | ...
    object_id: UUID
    reference: Optional[str] = None   # the number a person would quote
    subtitle: Optional[str] = None    # vendor, title, counterparty
    amount: float
    current_state: Optional[str] = None
    required_role: Optional[str] = None   # for approval items
    detail_url: str                       # where to go to act on it
    timeline_url: str                     # Live Audit Mode link
    # SLA (Build Book: timers + escalation visible on every work item)
    sla_due_at: Optional[datetime] = None
    overdue: bool = False
    escalated: bool = False               # visible to you via SLA escalation


class DecisionInbox(BaseModel):
    total: int
    counts: Dict[str, int]                    # per-category counts
    by_work_item_type: Dict[str, int] = {}    # the Build Book's own grouping
    overdue_count: int = 0                    # SLA-breached items in this page
    items: List[DecisionInboxItem]
