"""The AI router: which model runs a task, and what happens when it fails.

Build Book, AI Router:

  * *"Rules-based routing first: cheap models for extraction and
    classification, stronger models for reasoning, optional frontier model for
    complex unstructured text."*
  * *"Fallback logic when AI fails schema validation or confidence is low."*

Before this there was one `AI_PROVIDER` setting for everything, so classifying
a duplicate and reasoning about a workflow went to the same model at the same
cost — and when the answer came back malformed, each caller decided for itself
what to do, if anything.

Two things this owns, and nothing else:

  * **Choosing.** A task maps to an ordered list of candidates, cheapest first.
  * **Refusing.** Output is parsed and validated against the prompt's schema,
    and a response that fails validation or falls below the confidence floor is
    not returned to the caller — the next candidate is tried, and if none
    succeed the caller is told plainly that AI produced nothing usable.

What it deliberately does not own: authority. A validated response is a
well-formed suggestion, not a permitted one. Whether the suggested action is
allowed is a policy question and stays with the caller — the invoice agent
still checks the AI's action against what the gate permits, and still ignores
it when they differ.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.services.ai.prompts import (
    Prompt, get_prompt, TASK_CLASSIFICATION, TASK_EXTRACTION, TASK_REASONING,
)

logger = logging.getLogger(__name__)


@dataclass
class Attempt:
    """One try at one model. Kept whether it worked or not — a fallback that
    nobody can see is indistinguishable from a first-choice success, and the
    difference is what tells you a prompt or a model has started drifting."""
    provider: str
    model: Optional[str]
    ok: bool
    error: Optional[str] = None
    latency_ms: int = 0


@dataclass
class RoutedResult:
    """A validated output plus everything needed to reproduce it."""
    output: BaseModel
    prompt_name: str
    prompt_version: str
    prompt_hash: str
    provider: str
    model: Optional[str]
    attempts: List[Attempt] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def used_fallback(self) -> bool:
        return len(self.attempts) > 1


class AIUnavailable(RuntimeError):
    """No candidate produced a valid response.

    Raised rather than returning None so a caller cannot mistake "AI failed"
    for "AI said nothing was wrong". Callers fall back to their deterministic
    path; none of them are allowed to proceed without one.
    """

    def __init__(self, prompt_name: str, attempts: List[Attempt]):
        self.prompt_name = prompt_name
        self.attempts = attempts
        detail = "; ".join(
            f"{a.provider}/{a.model or 'default'}: {a.error}" for a in attempts
        ) or "no candidates configured"
        super().__init__(f"No usable AI response for {prompt_name!r} ({detail})")


def _candidates_for(task: str) -> List[Tuple[str, Optional[str]]]:
    """(provider, model) in the order they should be tried.

    Cheap first. The second entry is the fallback the Build Book asks for: it
    only runs when the first fails schema validation or comes back under-
    confident, so the stronger model costs nothing on the common path.

    Configured rather than hardcoded, because which model is "cheap" changes
    faster than this code will.
    """
    table = {
        TASK_EXTRACTION: settings.AI_ROUTE_EXTRACTION,
        TASK_CLASSIFICATION: settings.AI_ROUTE_CLASSIFICATION,
        TASK_REASONING: settings.AI_ROUTE_REASONING,
    }
    raw = table.get(task) or ""
    candidates: List[Tuple[str, Optional[str]]] = []
    for entry in [e.strip() for e in raw.split(",") if e.strip()]:
        provider, _, model = entry.partition(":")
        candidates.append((provider.strip(), model.strip() or None))
    if not candidates:
        # Nothing configured for this task: fall back to the single global
        # provider, which is what the whole system used before routing existed.
        candidates.append((settings.AI_PROVIDER, None))
    return candidates


def parse_json(text: str) -> dict:
    """Pull a JSON object out of model output, tolerating ``` fences.

    Models wrap JSON in markdown far more often than they return it bare, and
    treating that as a failure would send every response to the fallback for a
    formatting habit rather than a content problem.
    """
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


class AIRouter:
    """Runs a registered prompt against the right model, and validates the answer."""

    def __init__(self, provider_factory=None):
        # Injectable so tests can drive the routing and fallback logic without
        # a network call or an API key.
        self._provider_factory = provider_factory or self._default_factory

    @staticmethod
    def _default_factory(provider_name: str, model: Optional[str]):
        from app.services.ai import get_ai_provider_named

        return get_ai_provider_named(provider_name, model)

    def run(
        self,
        prompt_name: str,
        variables: Dict[str, Any],
        *,
        min_confidence: Optional[float] = None,
    ) -> RoutedResult:
        """Render, send, validate, and fall back. Raises AIUnavailable if none work."""
        prompt = get_prompt(prompt_name)
        rendered = prompt.render(**variables)
        floor = (
            min_confidence if min_confidence is not None
            else settings.AI_MIN_CONFIDENCE
        )

        attempts: List[Attempt] = []
        started = time.time()

        for provider_name, model in _candidates_for(prompt.task):
            attempt_started = time.time()
            try:
                provider = self._provider_factory(provider_name, model)
                raw = provider.chat(
                    messages=[{"role": "user", "content": rendered}], context=None
                )
                output = self._validate(prompt, raw)

                if output.confidence < floor:
                    # Build Book: fall back when confidence is low. A confident
                    # wrong answer and an unconfident right one look identical
                    # from here, so the only safe reading of "unsure" is to ask
                    # somebody better.
                    raise ValueError(
                        f"confidence {output.confidence:.2f} below floor {floor:.2f}"
                    )

                latency = int((time.time() - attempt_started) * 1000)
                attempts.append(Attempt(provider_name, model, True, None, latency))
                return RoutedResult(
                    output=output,
                    prompt_name=prompt.name,
                    prompt_version=prompt.version,
                    prompt_hash=prompt.content_hash,
                    provider=provider_name,
                    model=model,
                    attempts=attempts,
                    latency_ms=int((time.time() - started) * 1000),
                )
            except Exception as exc:
                latency = int((time.time() - attempt_started) * 1000)
                attempts.append(
                    Attempt(provider_name, model, False, str(exc)[:200], latency)
                )
                logger.warning(
                    "AI candidate %s/%s failed for %s: %s",
                    provider_name, model or "default", prompt.name, exc,
                )

        raise AIUnavailable(prompt.name, attempts)

    @staticmethod
    def _validate(prompt: Prompt, raw: str) -> BaseModel:
        """Parse and validate, or raise.

        Both failures are treated the same on purpose: unparseable output and
        output that parses into the wrong shape are equally unusable, and
        distinguishing them here would only tempt somebody into salvaging the
        second.
        """
        try:
            data = parse_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"not JSON: {exc}") from exc
        try:
            return prompt.output_schema.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"failed schema: {exc.error_count()} error(s)") from exc
