"""The shapes AI output must take.

Build Book, AI Principles: *"No free-form outputs in production paths.
Everything is strict JSON validated against schemas."*

The point is not tidiness. A model that returns prose where a decision was
expected, or a confidence of "high" where a number was expected, must fail
*visibly* and fall back — rather than being coerced into something that looks
like an answer and flows into a workflow. Every field below is therefore
strict: extra keys are rejected, and a confidence outside 0..1 is an invalid
response rather than a clamped one, because a model that returns 87 when asked
for 0.87 has misunderstood the question and its other fields deserve no trust
either.
"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class AIOutput(BaseModel):
    """Common base: strict, and every output states its own confidence.

    `extra="forbid"` on purpose. A model inventing an extra field is a model
    that has drifted from the instruction, and silently dropping the field
    hides that.
    """
    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


class InvoiceNextAction(AIOutput):
    """A suggestion for what to do with an invoice.

    The action is checked against what policy permits by the caller — this
    schema only guarantees the shape, never the authority. The gate is
    elsewhere and stays there.
    """
    action: str = Field(min_length=1)


class DuplicateAssessment(AIOutput):
    is_duplicate: bool
    matched_invoice_number: Optional[str] = None


class NaturalLanguageQuery(AIOutput):
    filters: Dict[str, Any] = Field(default_factory=dict)
