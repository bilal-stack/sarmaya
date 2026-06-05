from pydantic import BaseModel, ConfigDict
from typing import Optional, Any, Dict
from datetime import datetime
from uuid import UUID


class ConfigVersionResponse(BaseModel):
    """One entry in a config object's append-only version history."""
    version: int
    config_type: str
    config_key: str
    change_action: str
    change_reason: Optional[str] = None
    changed_by: Optional[UUID] = None
    created_at: datetime
    snapshot: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)
