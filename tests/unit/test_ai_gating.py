"""Unit tests for AI-output gating (app/schemas/ai.py).

AI output must be schema-validated structured JSON with provenance; malformed
output must fall back to a conservative, non-finalizing result.
"""
from app.schemas.ai import DuplicateDetectionResult


class TestDuplicateDetectionResult:
    def test_valid_passthrough_with_provenance(self):
        r = DuplicateDetectionResult.validated(
            {"is_duplicate": True, "confidence": 0.9, "strategy": "fuzzy_ai",
             "matched_invoice_id": "abc", "reasoning": "same vendor+amount"},
            provenance={"ai_provider": "claude", "ai_model": "claude-opus-4-8"},
        )
        assert r.is_duplicate is True
        assert r.confidence == 0.9
        assert r.ai_provider == "claude"
        assert r.ai_model == "claude-opus-4-8"

    def test_confidence_is_clamped(self):
        assert DuplicateDetectionResult.validated({"confidence": 5}).confidence == 1.0
        assert DuplicateDetectionResult.validated({"confidence": -2}).confidence == 0.0
        assert DuplicateDetectionResult.validated({"confidence": "x"}).confidence == 0.0

    def test_null_matched_id_normalized(self):
        assert DuplicateDetectionResult.validated({"matched_invoice_id": "null"}).matched_invoice_id is None
        assert DuplicateDetectionResult.validated({"matched_invoice_id": ""}).matched_invoice_id is None

    def test_malformed_output_falls_back_safely(self):
        # A non-coercible bool for is_duplicate violates the schema.
        r = DuplicateDetectionResult.validated(
            {"is_duplicate": "maybe", "confidence": 0.8},
            provenance={"ai_provider": "claude", "ai_model": "claude-opus-4-8"},
        )
        # Never trust malformed AI output: conservative non-duplicate, flagged for review.
        assert r.is_duplicate is False
        assert r.strategy == "schema_invalid"
        assert "review" in r.reasoning.lower()
        # Provenance is preserved even on the fallback path.
        assert r.ai_provider == "claude"
        assert r.ai_model == "claude-opus-4-8"

    def test_unknown_keys_ignored(self):
        r = DuplicateDetectionResult.validated(
            {"is_duplicate": True, "confidence": 0.5, "evil": "drop me"}
        )
        assert not hasattr(r, "evil")
        assert r.is_duplicate is True
