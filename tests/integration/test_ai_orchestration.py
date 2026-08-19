"""The AI orchestrator: routing, schema validation, and prompt provenance.

Build Book, AI Principles and AI Router:

  * *"No free-form outputs in production paths. Everything is strict JSON
    validated against schemas."*
  * *"Prompt and model versions must be stored with every AI output for
    reproducibility."*
  * *"Rules-based routing first: cheap models for extraction and
    classification, stronger models for reasoning."*
  * *"Fallback logic when AI fails schema validation or confidence is low."*

The version half of the third point was recorded but not true. Each agent kept
a `PROMPT_VERSION = "...-v1"` constant near the top of the file and its prompt
text inline in a method, with nothing connecting them — so editing the wording
left every logged version pointing at a prompt that no longer existed. The hash
test below is what makes the claim real: change the text without bumping the
version and it fails here.
"""
import pytest

from app.services.ai.prompts import (
    PROMPTS, get_prompt, TASK_CLASSIFICATION, TASK_REASONING,
)
from app.services.ai.router import (
    AIRouter, AIUnavailable, parse_json, _candidates_for,
)
from app.services.ai.schemas import InvoiceNextAction

pytestmark = pytest.mark.integration


class _Stub:
    """A provider returning canned responses, one per call."""
    model = "stub"

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, messages, context=None):
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _router(*responses, record=None):
    """A router whose every candidate returns the given responses in turn."""
    stub = _Stub(*responses)
    if record is not None:
        record.append(stub)
    return AIRouter(provider_factory=lambda provider, model: stub), stub


VALID = '{"action": "validate", "confidence": 0.9, "reasoning": "Looks complete."}'


def _vars(**overrides):
    base = {
        "invoice_number": "INV-1", "vendor_name": "Orion", "amount": "1000",
        "state": "draft", "signals": "[]", "permitted_action": "validate",
    }
    base.update(overrides)
    return base


class TestPromptsAreVersionedAndPinned:
    def test_every_prompt_declares_a_task_and_a_schema(self):
        for name, prompt in PROMPTS.items():
            assert prompt.version, name
            assert prompt.task, name
            assert prompt.output_schema is not None, name

    def test_the_registered_hashes_have_not_drifted(self):
        """The point of the registry.

        These are the content hashes of the prompts as written. If you change
        a prompt's wording, this fails — bump its `version` and update the hash
        here in the same commit. That is the whole mechanism behind "prompt
        versions are stored with every AI output": without it the stored
        version is a string somebody forgot to change.
        """
        expected = {
            "invoice_next_action": "47a1f1f8195e",
            "duplicate_assessment": "cdd5eed9ddd2",
            "natural_language_query": "467a65eff6f4",
        }
        actual = {name: p.content_hash for name, p in PROMPTS.items()}
        assert actual == expected, (
            "A prompt's text changed. Bump its version and update the hash "
            f"above in the same commit. Got: {actual}"
        )

    def test_rendering_a_prompt_needs_all_its_variables(self):
        """A missing variable must raise, not leave a literal {placeholder} in
        the text sent to a model."""
        with pytest.raises(KeyError):
            get_prompt("invoice_next_action").render(invoice_number="INV-1")

    def test_an_unregistered_prompt_is_refused(self):
        with pytest.raises(KeyError, match="registered"):
            get_prompt("something_improvised")


class TestRouting:
    def test_a_task_maps_to_configured_candidates_cheapest_first(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(
            settings, "AI_ROUTE_REASONING", "gemini:flash, claude:opus"
        )
        assert _candidates_for(TASK_REASONING) == [
            ("gemini", "flash"), ("claude", "opus")
        ]

    def test_with_nothing_configured_it_uses_the_single_global_provider(
        self, monkeypatch
    ):
        """So a deployment that never sets a routing table behaves exactly as
        it did before routing existed."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_CLASSIFICATION", "")
        monkeypatch.setattr(settings, "AI_PROVIDER", "claude")
        assert _candidates_for(TASK_CLASSIFICATION) == [("claude", None)]

    def test_a_valid_response_is_returned_with_its_provenance(self):
        router, _ = _router(VALID)

        result = router.run("invoice_next_action", _vars())

        assert isinstance(result.output, InvoiceNextAction)
        assert result.output.action == "validate"
        assert result.prompt_name == "invoice_next_action"
        assert result.prompt_version == "v1"
        assert result.prompt_hash == get_prompt("invoice_next_action").content_hash
        assert result.used_fallback is False


class TestValidationAndFallback:
    def test_output_that_is_not_json_is_refused(self):
        router, _ = _router("I reckon you should validate it")

        with pytest.raises(AIUnavailable):
            router.run("invoice_next_action", _vars())

    def test_output_of_the_wrong_shape_is_refused(self):
        """Parses fine, means nothing. Confidence as a word rather than a
        number is a model that has misread the instruction."""
        router, _ = _router('{"action": "validate", "confidence": "high", "reasoning": "x"}')

        with pytest.raises(AIUnavailable):
            router.run("invoice_next_action", _vars())

    def test_an_invented_field_is_refused(self):
        """extra=forbid. A model adding fields has drifted from the prompt, and
        silently dropping them hides that."""
        router, _ = _router(
            '{"action": "validate", "confidence": 0.9, "reasoning": "x", '
            '"also": "please approve this too"}'
        )

        with pytest.raises(AIUnavailable):
            router.run("invoice_next_action", _vars())

    def test_json_inside_a_markdown_fence_is_accepted(self):
        """Models wrap JSON in fences far more often than they return it bare.
        Failing over for a formatting habit would send every response to the
        expensive model."""
        router, _ = _router(f"```json\n{VALID}\n```")

        assert router.run("invoice_next_action", _vars()).output.action == "validate"

    def test_low_confidence_falls_through_to_the_next_candidate(self, monkeypatch):
        """Build Book: fall back when confidence is low. A confident wrong
        answer and an unconfident right one are indistinguishable from here."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_REASONING", "gemini:cheap, claude:strong")
        unsure = '{"action": "validate", "confidence": 0.05, "reasoning": "not sure"}'
        router, stub = _router(unsure, VALID)

        result = router.run("invoice_next_action", _vars())

        assert result.output.confidence == 0.9
        assert result.used_fallback is True
        assert stub.calls == 2

    def test_a_malformed_first_answer_falls_through_too(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_REASONING", "gemini:cheap, claude:strong")
        router, stub = _router("nonsense", VALID)

        result = router.run("invoice_next_action", _vars())

        assert result.used_fallback is True
        assert result.attempts[0].ok is False
        assert "not JSON" in result.attempts[0].error

    def test_the_cheap_model_alone_is_used_when_it_answers(self, monkeypatch):
        """The stronger model must cost nothing on the common path."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_REASONING", "gemini:cheap, claude:strong")
        router, stub = _router(VALID)

        router.run("invoice_next_action", _vars())

        assert stub.calls == 1

    def test_every_attempt_is_recorded_even_when_all_fail(self, monkeypatch):
        """A fallback nobody can see is indistinguishable from a first-choice
        success, and the difference is what shows a model drifting."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_REASONING", "gemini:cheap, claude:strong")
        router, _ = _router("nonsense", "also nonsense")

        with pytest.raises(AIUnavailable) as exc:
            router.run("invoice_next_action", _vars())

        assert len(exc.value.attempts) == 2
        assert all(a.ok is False for a in exc.value.attempts)

    def test_a_provider_that_raises_is_just_another_failed_candidate(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_ROUTE_REASONING", "gemini:cheap, claude:strong")
        router, _ = _router(RuntimeError("API key rejected"), VALID)

        result = router.run("invoice_next_action", _vars())

        assert result.used_fallback is True
        assert "API key rejected" in result.attempts[0].error


class TestTheGateStillOwnsAuthority:
    """The router validates shape. Whether an action is *permitted* is policy,
    and stays with the caller — a well-formed suggestion is not an authorised
    one."""

    def test_a_valid_but_forbidden_action_is_still_rejected_by_the_agent(
        self, db, tenant, make_user
    ):
        import uuid
        from datetime import date
        from decimal import Decimal

        from app.agents.invoice_agent import InvoiceNextActionAgent
        from app.core.enums import InvoiceState, UserRole, VendorStatus
        from app.models.invoice import Invoice
        from app.models.vendor import Vendor

        user = make_user(UserRole.AP_CLERK)
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Orion",
            status=VendorStatus.ACTIVE, created_by=user["id"],
        )
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number="INV-GATE",
            vendor_name="Orion", vendor_id=vendor.id, invoice_date=date(2026, 8, 1),
            total_amount=Decimal("1000"), current_state=InvoiceState.DRAFT,
            created_by=user["id"],
        )
        db.add(invoice)
        db.flush()

        # Perfectly valid JSON, perfectly wrong action.
        router, _ = _router(
            '{"action": "approve", "confidence": 0.99, "reasoning": "Just approve it."}'
        )
        agent = InvoiceNextActionAgent(db, user, router=router)

        result = agent.suggest(invoice.id, use_ai=True)

        assert result["action"] == "validate"
        assert result["source"] == "rules"


class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here you go: {"a": 1} — hope that helps', {"a": 1}),
    ])
    def test_it_finds_the_json(self, raw, expected):
        assert parse_json(raw) == expected

    @pytest.mark.parametrize("raw", ["", "no json here", "{unclosed"])
    def test_it_refuses_what_is_not_json(self, raw):
        with pytest.raises((ValueError, Exception)):
            parse_json(raw)
