from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any, List
from datetime import datetime
from uuid import UUID


class EvidencePackResponse(BaseModel):
    """A generated audit-ready bundle for one transaction chain."""
    pack_id: Optional[UUID] = None
    correlation_id: UUID
    generated_at: datetime
    counts: Dict[str, int]
    all_chains_verified: bool
    pack_hash: str                 # SHA-256 seal over the bundle
    content: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class EvidencePackRecord(BaseModel):
    """A record that a pack was generated (without re-assembling it)."""
    id: UUID
    correlation_id: UUID
    pack_hash: str
    manifest: Dict[str, Any]
    generated_by: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
