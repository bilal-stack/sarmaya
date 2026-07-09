"""Schemas that gate AI output.

Per the Build Book, AI output must be structured JSON, schema-validated, and
carry model/provider provenance — and malformed AI output must never be trusted
(AI assists; it never finalizes a decision). These models validate raw provider
output and fall back to a conservative default when it doesn't conform.
"""
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

logger = logging.getLogger(__name__)


class AIActionLogResponse(BaseModel):
    """One AI-action audit record (provenance + status + explainability trace)."""
    id: UUID
    action: str
    status: str
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    prompt_version: Optional[str] = None
    confidence: Optional[float] = None
    latency_ms: Optional[int] = None
    input_summary: Optional[str] = None
    output_summary: Optional[str] = None
    object_type: Optional[str] = None
    object_id: Optional[UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _coerce_money(v):
    """Coerce a money-ish value (int/float/str with commas or currency text)
    to float; unparseable values become 0.0 rather than failing the whole
    result — a zero amount is caught later by the required-fields guard."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        cleaned = "".join(ch for ch in v if ch.isdigit() or ch in ".-")
        try:
            return float(cleaned) if cleaned else 0.0
        except ValueError:
            return 0.0
    return 0.0


class InvoiceExtractionResult(BaseModel):
    """Validated result of AI-enhanced invoice field extraction.

    Gates the OCR-enhancement output (Build Book: structured JSON only, schema
    validation always). Scalars are coerced leniently (a comma in an amount
    should not void a good extraction); structure is strict (line_items must be
    a list of objects). Structurally malformed output is rejected entirely and
    the raw OCR result is used instead.
    """
    vendor_name: str = ""
    invoice_number: str = ""
    invoice_date: Optional[str] = None
    due_date: Optional[str] = None
    total_amount: float = 0.0
    tax_amount: float = 0.0
    subtotal_amount: Optional[float] = None
    currency: str = "PKR"
    line_items: list[dict] = []
    confidence: int = 0
    ai_corrections: dict = {}

    @field_validator("vendor_name", "invoice_number", "currency", mode="before")
    @classmethod
    def _str_or_empty(cls, v):
        return str(v).strip() if v is not None else ""

    @field_validator("invoice_date", "due_date", mode="before")
    @classmethod
    def _date_str(cls, v):
        if v in (None, "", "null"):
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    @field_validator("total_amount", "tax_amount", "subtotal_amount", mode="before")
    @classmethod
    def _money(cls, v):
        return _coerce_money(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_0_100(cls, v):
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, v))

    @field_validator("line_items", mode="before")
    @classmethod
    def _line_items_structure(cls, v):
        if v is None:
            return []
        if not isinstance(v, list) or any(not isinstance(i, dict) for i in v):
            # Structural violation — fail validation so the whole result is rejected.
            raise ValueError("line_items must be a list of objects")
        return [
            {
                "description": str(i.get("description", "")).strip(),
                "quantity": _coerce_money(i.get("quantity")) or 0.0,
                "unit_price": _coerce_money(i.get("unit_price")) or 0.0,
                "amount": _coerce_money(i.get("amount")) or 0.0,
                "product_code": str(i.get("product_code", "") or ""),
            }
            for i in v
        ]

    @field_validator("ai_corrections", mode="before")
    @classmethod
    def _corrections_dict(cls, v):
        return v if isinstance(v, dict) else {}

    @classmethod
    def try_validate(cls, raw) -> Optional["InvoiceExtractionResult"]:
        """Validate raw AI output; None when structurally malformed (the caller
        must then fall back to the un-enhanced OCR result)."""
        if not isinstance(raw, dict):
            logger.warning("AI extraction output is not an object: %r", type(raw))
            return None
        data = {k: v for k, v in raw.items() if k in cls.model_fields}
        try:
            return cls(**data)
        except ValidationError as e:
            logger.warning("AI extraction output failed schema validation: %s", e)
            return None


class InvoiceNextActionSuggestion(BaseModel):
    """Validated output of the invoice next-action agent.

    The agent SUGGESTS the next step for an invoice (extract-review / validate /
    submit / resolve-duplicate / verify-vendor / approve / ...); it never
    executes one. `signals` is the explainability trace — the deterministic
    facts the suggestion was based on (Build Book: AI must attach what signals
    were used and why).
    """
    action: str
    confidence: float = 0.0
    reasoning: str = ""
    signals: list[str] = []
    required_role: Optional[str] = None
    # "rules" when produced deterministically; "ai" when the AI wrote the
    # suggestion (within the policy-permitted action set).
    source: str = "rules"
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    prompt_version: Optional[str] = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, v))


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
