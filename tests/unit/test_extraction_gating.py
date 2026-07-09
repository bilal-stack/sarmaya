"""Unit tests for AI extraction gating (InvoiceExtractionResult) and the OCR
pipeline's use of it (providers stubbed — no network)."""
import pytest

from app.schemas.ai import InvoiceExtractionResult
from app.services import ocr as ocr_module
from app.core.config import settings


class TestInvoiceExtractionResultSchema:
    def test_valid_output_passes(self):
        r = InvoiceExtractionResult.try_validate({
            "vendor_name": " Acme Ltd ", "invoice_number": "INV-1",
            "invoice_date": "2026-01-05", "total_amount": 1500.5,
            "tax_amount": 250, "currency": "PKR", "confidence": 92,
            "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 625, "amount": 1250}],
        })
        assert r is not None
        assert r.vendor_name == "Acme Ltd"
        assert r.total_amount == 1500.5
        assert r.line_items[0]["quantity"] == 2.0

    def test_money_strings_coerced(self):
        r = InvoiceExtractionResult.try_validate({"total_amount": "Rs 1,250,000.50", "tax_amount": "abc"})
        assert r.total_amount == 1250000.50
        assert r.tax_amount == 0.0  # unparseable -> 0, caught later by field guard

    def test_confidence_clamped(self):
        assert InvoiceExtractionResult.try_validate({"confidence": 250}).confidence == 100
        assert InvoiceExtractionResult.try_validate({"confidence": "high"}).confidence == 0

    def test_structural_violation_rejects_whole_result(self):
        # line_items as a string is a structure violation -> reject entirely.
        assert InvoiceExtractionResult.try_validate({"line_items": "two widgets"}) is None
        assert InvoiceExtractionResult.try_validate({"line_items": ["not-a-dict"]}) is None
        assert InvoiceExtractionResult.try_validate("not a dict") is None

    def test_unknown_keys_ignored(self):
        r = InvoiceExtractionResult.try_validate({"vendor_name": "A", "evil": "x"})
        assert r is not None and not hasattr(r, "evil")


class _StubOCR:
    def extract_invoice_data(self, file_path):
        return {
            "vendor_name": "OCR Vendor", "invoice_number": "OCR-1",
            "invoice_date": "2026-01-01", "total_amount": 100.0, "tax_amount": 0.0,
            "confidence": 60, "line_items": [], "raw_data": {"text": "raw ocr text"},
        }


class _StubAI:
    model = "stub-model"

    def __init__(self, result):
        self._result = result

    def extract_invoice_fields(self, ocr_text, raw_ocr_data=None, line_items=None):
        return self._result


@pytest.fixture(autouse=True)
def _ai_enhanced_on(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENHANCED_OCR", True)


class TestPipelineGate:
    def _run(self, monkeypatch, ai_result):
        monkeypatch.setattr(ocr_module, "get_ocr_provider", lambda: _StubOCR())
        monkeypatch.setattr(ocr_module, "get_ai_provider", lambda: _StubAI(ai_result))
        return ocr_module.extract_invoice_data_ocr("dummy.pdf")

    def test_valid_higher_confidence_ai_is_merged(self, monkeypatch):
        out = self._run(monkeypatch, {
            "vendor_name": "AI Vendor", "invoice_number": "AI-1",
            "total_amount": 999.0, "confidence": 90,
            "line_items": [{"description": "Item", "quantity": 1, "unit_price": 999, "amount": 999}],
        })
        assert out["ai_enhanced"] is True
        assert out["vendor_name"] == "AI Vendor"
        assert out["confidence"] == 90

    def test_malformed_ai_output_is_rejected_ocr_stands(self, monkeypatch):
        out = self._run(monkeypatch, {"vendor_name": "AI Vendor", "confidence": 95, "line_items": "3 widgets"})
        assert out["ai_enhanced"] is False
        assert out["vendor_name"] == "OCR Vendor"  # raw OCR result untouched
        assert out["confidence"] == 60

    def test_provider_error_result_is_skipped(self, monkeypatch):
        out = self._run(monkeypatch, {"error": "AI extraction failed", "confidence": 0})
        assert out["ai_enhanced"] is False
        assert out["vendor_name"] == "OCR Vendor"
