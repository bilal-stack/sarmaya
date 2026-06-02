from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional, Literal, Dict, Any
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
