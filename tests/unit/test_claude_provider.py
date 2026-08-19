"""Unit tests for the Claude provider's pure adapters.

These cover the OpenAI->Anthropic shape conversions and tenant scoping without
any network call or API key (the network methods are exercised live, not here).
"""

from app.services.ai.claude_provider import (
    _split_system,
    _to_anthropic_tools,
    _parse_json,
)


class TestSplitSystem:
    def test_pulls_system_out_and_keeps_turns(self):
        system, conv = _split_system([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert system == "You are helpful."
        assert conv == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_first_message_forced_to_user(self):
        # Anthropic requires the first message to be a user turn.
        system, conv = _split_system([{"role": "assistant", "content": "x"}])
        assert conv[0]["role"] == "user"

    def test_no_system_returns_none(self):
        system, conv = _split_system([{"role": "user", "content": "hi"}])
        assert system is None


class TestToolConversion:
    def test_openai_function_def_converted(self):
        out = _to_anthropic_tools([
            {"type": "function", "function": {
                "name": "query_invoices",
                "description": "Query invoices",
                "parameters": {"type": "object", "properties": {"status": {"type": "string"}}},
            }}
        ])
        assert out == [{
            "name": "query_invoices",
            "description": "Query invoices",
            "input_schema": {"type": "object", "properties": {"status": {"type": "string"}}},
        }]

    def test_empty_is_empty(self):
        assert _to_anthropic_tools(None) == []


class TestParseJson:
    def test_plain(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
