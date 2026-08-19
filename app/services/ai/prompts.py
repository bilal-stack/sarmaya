"""The prompt registry: every prompt, its version, and its schema in one place.

Build Book, AI Principles: *"Prompt and model versions must be stored with every
AI output for reproducibility."*

That was recorded but not true. Each agent carried a `PROMPT_VERSION = "...-v1"`
constant near the top of the file and the prompt text itself further down, inline
in the method. Nothing connected them, so editing the wording without touching
the constant left every logged `prompt_version` pointing at a prompt that no
longer existed — and reproducibility means being able to say what was actually
sent, not what a string says was sent.

So a prompt here carries its own text, and its identity includes a hash of that
text. `test_ai_orchestration.py` pins the hashes: changing the wording without
bumping the version fails, loudly, in the one place somebody will see it.

Adding a prompt means adding an entry here rather than a string literal inside a
method — which also puts every prompt in the system on one screen, where they
can be read against each other.
"""
import hashlib
from dataclasses import dataclass
from typing import Dict, Type

from pydantic import BaseModel

from app.services.ai.schemas import (
    DuplicateAssessment, InvoiceNextAction, NaturalLanguageQuery,
)

#: What kind of work a prompt is, which is what the router uses to pick a model.
#: Build Book: "cheap models for extraction and classification, stronger models
#: for reasoning, optional frontier model for complex unstructured text".
TASK_EXTRACTION = "extraction"
TASK_CLASSIFICATION = "classification"
TASK_REASONING = "reasoning"


@dataclass(frozen=True)
class Prompt:
    """One prompt, versioned, with the shape its output must take."""

    name: str
    version: str
    task: str
    template: str
    #: Every production path validates against this. Build Book: "No free-form
    #: outputs in production paths."
    output_schema: Type[BaseModel]

    @property
    def content_hash(self) -> str:
        """First 12 hex of sha256 over the template.

        Logged beside the version so an output can be traced to the exact text
        that produced it, whatever anybody believed the version meant.
        """
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()[:12]

    def render(self, **variables) -> str:
        """Fill the template. Missing variables raise rather than silently
        producing a prompt with a literal `{placeholder}` in it."""
        return self.template.format(**variables)


INVOICE_NEXT_ACTION = Prompt(
    name="invoice_next_action",
    version="v1",
    task=TASK_REASONING,
    output_schema=InvoiceNextAction,
    template=(
        "You are the invoice workflow assistant. Policy has already determined "
        "the next action for this invoice; your job is to explain it clearly to "
        "the user and estimate your confidence.\n\n"
        "Invoice: {invoice_number} | vendor: {vendor_name} | amount: {amount} | "
        "state: {state}\n"
        "Signals: {signals}\n"
        "Permitted next action (you MUST use exactly this): {permitted_action}\n\n"
        "Return ONLY a JSON object:\n"
        '{{"action": "{permitted_action}", "confidence": 0.0, '
        '"reasoning": "one or two sentences for the user"}}\n'
    ),
)

DUPLICATE_ASSESSMENT = Prompt(
    name="duplicate_assessment",
    version="v1",
    task=TASK_CLASSIFICATION,
    output_schema=DuplicateAssessment,
    template=(
        "You are checking whether an invoice duplicates one already in the "
        "system. Judge only on the evidence given; do not assume.\n\n"
        "Candidate invoice: {candidate}\n"
        "Existing invoices: {existing}\n\n"
        "Return ONLY a JSON object:\n"
        '{{"is_duplicate": false, "confidence": 0.0, '
        '"matched_invoice_number": null, '
        '"reasoning": "what matched or did not, in one or two sentences"}}\n'
    ),
)

NATURAL_LANGUAGE_QUERY = Prompt(
    name="natural_language_query",
    version="v1",
    task=TASK_CLASSIFICATION,
    output_schema=NaturalLanguageQuery,
    template=(
        "Turn the user's question into structured filters for an invoice "
        "search. Use only the fields listed; leave anything unstated as null.\n\n"
        "Question: {question}\n"
        "Available fields: {fields}\n\n"
        "Return ONLY a JSON object:\n"
        '{{"filters": {{}}, "confidence": 0.0, '
        '"reasoning": "how the question maps to the filters"}}\n'
    ),
)

#: Every prompt in the system, by name.
PROMPTS: Dict[str, Prompt] = {
    p.name: p for p in (
        INVOICE_NEXT_ACTION,
        DUPLICATE_ASSESSMENT,
        NATURAL_LANGUAGE_QUERY,
    )
}


def get_prompt(name: str) -> Prompt:
    if name not in PROMPTS:
        raise KeyError(
            f"No prompt named {name!r}. Prompts are registered in "
            "app/services/ai/prompts.py so that every one of them is versioned "
            "and hashed."
        )
    return PROMPTS[name]
