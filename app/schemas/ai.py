"""Schemas that gate AI output.

Per the Build Book, AI output must be structured JSON, schema-validated, and
carry model/provider provenance — and malformed AI output must never be trusted
(AI assists; it never finalizes a decision). These models validate raw provider
output and fall back to a conservative default when it doesn't conform.
"""
import logging
from typing import Optional

from pydantic import BaseModel, ValidationError, field_validator

logger = logging.getLogger(__name__)


class DuplicateDetectionResult(BaseModel):
    """Validated result of the duplicate-detection agent."""
    is_duplicate: bool = False
    confidence: float = 0.0
    strategy: str = "none"
    matched_invoice_id: Optional[str] = None
    reasoning: str = ""
    # Provenance — which model/provider produced this (gating requirement).
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, v))

    @field_validator("matched_invoice_id", mode="before")
    @classmethod
    def _normalize_id(cls, v):
        if v in (None, "", "null", "None"):
            return None
        return str(v)

    @classmethod
    def validated(cls, raw: dict, provenance: Optional[dict] = None) -> "DuplicateDetectionResult":
        """Coerce raw provider output into a valid result, attaching provenance.
        On a schema violation, return a conservative non-duplicate result that
        routes the invoice to manual review rather than trusting bad output."""
        data = {k: v for k, v in (raw or {}).items() if k in cls.model_fields}
        if provenance:
            data.update({k: v for k, v in provenance.items() if k in cls.model_fields})
        try:
            return cls(**data)
        except ValidationError as e:
            logger.warning("AI duplicate-detection output failed schema validation: %s", e)
            return cls(
                is_duplicate=False,
                confidence=0.0,
                strategy="schema_invalid",
                reasoning="AI returned an unrecognized result; manual review recommended.",
                ai_provider=(provenance or {}).get("ai_provider"),
                ai_model=(provenance or {}).get("ai_model"),
            )
