from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any, List
from datetime import datetime
from uuid import UUID

from app.core.roles import is_valid_role

Operator = Literal["greater_than", "greater_equal", "less_than", "less_equal", "equal"]


class ApprovalRule(BaseModel):
    """The rule_config payload of an approval_limit policy: route an invoice to
    `required_role` when its amount satisfies `operator` against `amount_threshold`."""
    amount_threshold: float
    operator: Operator
    required_role: str

    @field_validator("amount_threshold")
    @classmethod
    def threshold_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("amount_threshold cannot be negative")
        return v

    @field_validator("required_role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not is_valid_role(v):
            raise ValueError(f"'{v}' is not a valid role")
        return v


class ApprovalPolicyCreate(BaseModel):
    policy_name: str
    description: Optional[str] = None
    rule: ApprovalRule
    priority: int = 0
    is_active: bool = True

    @field_validator("policy_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("policy_name is required")
        return v.strip()


class ApprovalPolicyUpdate(BaseModel):
    policy_name: Optional[str] = None
    description: Optional[str] = None
    rule: Optional[ApprovalRule] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None

    @field_validator("policy_name")
    @classmethod
    def name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            raise ValueError("policy_name cannot be blank")
        return v.strip() if v else v


class ApprovalPolicyResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    policy_type: str
    policy_name: str
    description: Optional[str] = None
    rule_config: Dict[str, Any]
    applies_to: Optional[str] = None
    is_active: bool
    priority: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PolicyEvalResponse(BaseModel):
    """One recorded policy evaluation: which rule version decided what, on what
    inputs, and why."""
    id: UUID
    policy_key: str
    policy_id: Optional[UUID] = None
    policy_name: Optional[str] = None
    policy_version: Optional[int] = None
    inputs: Dict[str, Any]
    output: Dict[str, Any]
    reasons: List[str] = []
    object_type: Optional[str] = None
    object_id: Optional[UUID] = None
    evaluated_by: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SimulationRule(BaseModel):
    """One rule in a proposed approval matrix."""
    policy_name: str = "Proposed rule"
    priority: int = 0
    rule: ApprovalRule


class PolicySimulationRequest(BaseModel):
    """Replay a proposed approval matrix against historical invoices."""
    proposed_rules: List[SimulationRule]
    window_days: int = 90
    # Optional separate what-if: how many invoices a given autopilot limit
    # would have made eligible for auto-approval.
    autopilot_limit: Optional[float] = None


class SimulationChange(BaseModel):
    invoice_id: UUID
    invoice_number: Optional[str] = None
    amount: float
    from_role: str
    to_role: str
    new_reason: str


class PolicySimulationResult(BaseModel):
    window_days: int
    invoices_evaluated: int
    routing_before: Dict[str, Any]
    routing_after: Dict[str, Any]
    changed_count: int
    changed_value: float
    net_by_role: Dict[str, int]
    changes: List[SimulationChange]
    autopilot_eligible: Optional[Dict[str, Any]] = None
