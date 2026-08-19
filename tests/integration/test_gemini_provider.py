"""The Gemini provider satisfies the shared AIProvider contract.

`get_ai_provider()` raised NotImplementedError for AI_PROVIDER=gemini while the
Build Book and the client-facing material promise a choice of mainstream
providers. These tests cover the adaptations that are easy to get wrong and
invisible until a real call is made: Gemini names the assistant role "model",
takes the system prompt as `system_instruction` rather than a message, and
describes tools as FunctionDeclarations instead of OpenAI's nested shape.

The SDK is stubbed throughout. A real call needs a paid key, and what matters
here is the translation between the app's provider-agnostic shapes and
Gemini's, which is exactly what a stub can assert.
"""
from types import SimpleNamespace

import pytest

from app.services.ai.gemini_provider import (
    GeminiProvider, _split_system, _to_gemini_tools, _text_of, _parse_json,
)

pytestmark = pytest.mark.integration


def _response(text=None, function_calls=None):
    """Minimal stand-in for a google-genai GenerateContentResponse."""
    parts = []
    if text is not None:
        parts.append(SimpleNamespace(text=text, function_call=None))
    content = SimpleNamespace(role="model", parts=parts)
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)],
        function_calls=function_calls or [],
    )


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch):
    """The provider refuses to construct without a key; tests are not run
    against a real one."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "GOOGLE_AI_API_KEY", "test-key")


@pytest.fixture
def provider(monkeypatch):
    """A provider whose client records calls instead of making them."""
    calls = []

    class _Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return _response(text='{"ok": true}')

    monkeypatch.setattr(
        "app.services.ai.gemini_provider.genai.Client",
        lambda **kw: SimpleNamespace(models=_Models()),
    )
    p = GeminiProvider()
    p.recorded = calls
    return p


class TestMessageTranslation:

    def test_assistant_becomes_model(self):
        _, contents = _split_system([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        assert [c.role for c in contents] == ["user", "model"]

    def test_system_messages_are_lifted_out(self):
        system, contents = _split_system([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ])
        assert system == "be brief"
        # The system prompt must not remain in the conversation.
        assert [c.role for c in contents] == ["user"]

    def test_multiple_system_messages_are_joined(self):
        system, _ = _split_system([
            {"role": "system", "content": "one"},
            {"role": "system", "content": "two"},
            {"role": "user", "content": "hello"},
        ])
        assert system == "one\n\ntwo"

    def test_an_empty_conversation_still_produces_a_user_turn(self):
        """Gemini rejects an empty contents list."""
        _, contents = _split_system([{"role": "system", "content": "only system"}])
        assert len(contents) == 1
        assert contents[0].role == "user"


class TestToolTranslation:

    def test_openai_tool_shape_is_converted(self):
        tools = _to_gemini_tools([{
            "type": "function",
            "function": {
                "name": "query_invoices",
                "description": "Find invoices",
                "parameters": {"type": "object", "properties": {"status": {"type": "string"}}},
            },
        }])
        assert len(tools) == 1
        decl = tools[0].function_declarations[0]
        assert decl.name == "query_invoices"
        assert decl.description == "Find invoices"

    def test_flat_tool_shape_is_tolerated(self):
        tools = _to_gemini_tools([{"name": "flat_tool", "parameters": {"type": "object"}}])
        assert tools[0].function_declarations[0].name == "flat_tool"

    def test_no_tools_yields_no_tool_config(self):
        assert _to_gemini_tools(None) == []
        assert _to_gemini_tools([]) == []


class TestResponseHandling:

    def test_text_is_read_from_parts(self):
        assert _text_of(_response(text="hello")) == "hello"

    def test_a_function_call_only_reply_does_not_raise(self):
        """response.text raises when the model replied with a call and no text."""
        assert _text_of(_response()) == ""

    def test_malformed_response_yields_empty_text(self):
        assert _text_of(SimpleNamespace(candidates=[])) == ""

    def test_fenced_json_is_parsed(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


class TestProviderContract:

    def test_chat_sends_the_system_instruction_on_the_config(self, provider):
        provider.chat([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ])
        config = provider.recorded[-1]["config"]
        assert config.system_instruction == "be brief"

    def test_chat_folds_context_into_the_system_instruction(self, provider):
        provider.chat([{"role": "user", "content": "hi"}], context={"tenant_id": "t1"})
        assert "t1" in provider.recorded[-1]["config"].system_instruction

    def test_extraction_requests_json_natively(self, provider):
        provider.extract_invoice_fields("some ocr text")
        assert provider.recorded[-1]["config"].response_mime_type == "application/json"

    def test_extraction_fills_missing_keys(self, provider):
        result = provider.extract_invoice_fields("some ocr text")
        for key in ("vendor_name", "invoice_number", "total_amount",
                    "confidence", "line_items", "ai_corrections"):
            assert key in result

    def test_a_provider_failure_returns_a_safe_result(self, monkeypatch):
        """A failed extraction must not raise into the upload flow; a zeroed
        result is caught later by the required-fields guard."""
        class _Boom:
            def generate_content(self, **kwargs):
                raise RuntimeError("provider down")

        monkeypatch.setattr(
            "app.services.ai.gemini_provider.genai.Client",
            lambda **kw: SimpleNamespace(models=_Boom()),
        )
        result = GeminiProvider().extract_invoice_fields("text")
        assert result["confidence"] == 0
        assert result["error"] == "AI extraction failed"

    def test_chat_failure_returns_a_message_not_an_exception(self, monkeypatch):
        class _Boom:
            def generate_content(self, **kwargs):
                raise RuntimeError("provider down")

        monkeypatch.setattr(
            "app.services.ai.gemini_provider.genai.Client",
            lambda **kw: SimpleNamespace(models=_Boom()),
        )
        assert "temporarily unavailable" in GeminiProvider().chat(
            [{"role": "user", "content": "hi"}]
        )

    def test_no_candidates_means_no_duplicate_call_is_made(self, provider):
        result = provider.detect_duplicate_invoices({"vendor_name": "Acme"}, [])
        assert result["is_duplicate"] is False
        assert provider.recorded == [], "the provider was called with nothing to compare"


class TestToolCallingIsTenantScoped:

    def test_the_tool_result_is_fed_back_and_scoped_to_the_tenant(
        self, db, tenant, make_user, monkeypatch
    ):
        """The SDK's automatic function calling is disabled so the query runs
        through _execute_invoice_query, which always filters by tenant."""
        import uuid
        from app.core.enums import InvoiceState, UserRole
        from app.models.invoice import Invoice

        user = make_user(UserRole.ADMIN)
        db.add(Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_name="Acme",
            invoice_number="GEM-1", invoice_date="2026-07-01", total_amount=100,
            current_state=InvoiceState.DRAFT, created_by=user["id"],
        ))
        db.flush()

        seen = []

        class _Models:
            def __init__(self):
                self.n = 0

            def generate_content(self, **kwargs):
                seen.append(kwargs)
                self.n += 1
                if self.n == 1:
                    call = SimpleNamespace(name="query_invoices", args={"vendor_name": "Acme"})
                    return _response(function_calls=[call])
                return _response(text="You have 1 invoice from Acme.")

        monkeypatch.setattr(
            "app.services.ai.gemini_provider.genai.Client",
            lambda **kw: SimpleNamespace(models=_Models()),
        )

        out = GeminiProvider().chat_with_tools(
            [{"role": "user", "content": "invoices from Acme?"}],
            context={"tenant_id": str(tenant.id)},
            tools=[{"function": {"name": "query_invoices", "parameters": {"type": "object"}}}],
            db=db,
        )

        assert out["function_called"] == "query_invoices"
        assert out["function_result"]["count"] == 1
        assert "Acme" in out["content"]
        # Automatic calling must stay off, or the SDK would run the tool itself
        # and bypass the tenant filter.
        assert seen[0]["config"].automatic_function_calling.disable is True

    def test_a_tool_call_without_a_db_falls_back_to_text(self, monkeypatch):
        class _Models:
            def generate_content(self, **kwargs):
                call = SimpleNamespace(name="query_invoices", args={})
                return _response(text="cannot query", function_calls=[call])

        monkeypatch.setattr(
            "app.services.ai.gemini_provider.genai.Client",
            lambda **kw: SimpleNamespace(models=_Models()),
        )
        out = GeminiProvider().chat_with_tools(
            [{"role": "user", "content": "hi"}], context={}, tools=None, db=None
        )
        assert out["function_called"] is None


class TestConfiguration:

    def test_a_missing_key_names_the_setting(self, monkeypatch):
        """Selecting gemini without a key should say so, not surface the SDK's
        bare ValueError as an opaque 500."""
        from app.core.config import settings
        monkeypatch.setattr(settings, "GOOGLE_AI_API_KEY", "")

        with pytest.raises(ValueError, match="GOOGLE_AI_API_KEY"):
            GeminiProvider()

    def test_the_factory_returns_the_gemini_provider(self, monkeypatch):
        from app.core.config import settings
        from app.services.ai import get_ai_provider

        monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")
        monkeypatch.setattr(
            "app.services.ai.gemini_provider.genai.Client",
            lambda **kw: SimpleNamespace(models=None),
        )
        assert type(get_ai_provider()).__name__ == "GeminiProvider"
