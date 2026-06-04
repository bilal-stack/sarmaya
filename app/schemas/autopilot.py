from pydantic import BaseModel, field_validator
from typing import Optional, List
from uuid import UUID


class AutopilotConfig(BaseModel):
    """Opt-in Restricted Autopilot settings. Disabled by default; only ever acts
    within these bounds."""
    enabled: bool = False
    max_auto_approve_amount: float = 0
    require_active_vendor: bool = True
    require_no_duplicate: bool = True

    @field_validator("max_auto_approve_amount")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("max_auto_approve_amount cannot be negative")
        return v


class AutopilotCandidate(BaseModel):
    invoice_id: UUID
    invoice_number: Optional[str] = None
    vendor_name: Optional[str] = None
    amount: float
    eligible: bool
    reason: str


class AutopilotPreview(BaseModel):
    enabled: bool
    config: AutopilotConfig
    considered: int
    eligible_count: int
    candidates: List[AutopilotCandidate]


class AutopilotRunResult(BaseModel):
    enabled: bool
    considered: int
    approved_count: int
    approved: List[AutopilotCandidate]
