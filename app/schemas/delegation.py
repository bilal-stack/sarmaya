from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from uuid import UUID


class DelegationCreate(BaseModel):
    """Lend approval authority for a bounded window. Omit `from_user_id` to
    delegate your own; naming someone else requires users.manage."""
    to_user_id: UUID
    starts_at: datetime
    ends_at: datetime
    from_user_id: Optional[UUID] = None
    reason: Optional[str] = None


class DelegationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    starts_at: datetime
    ends_at: datetime
    reason: Optional[str] = None
    revoked_at: Optional[datetime] = None
    created_by: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
