"""Unit tests for the workflow transition-guard registry."""
from types import SimpleNamespace
from datetime import date

from app.services.workflow_guards import evaluate_guards


def _inv(**overrides):
    base = dict(
        vendor_id="v", invoice_number="INV-1", invoice_date=date(2026, 1, 1),
        total_amount=100, potential_duplicate_id=None, duplicate_acknowledged=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestRequiredFields:
    def test_passes_when_complete(self):
        assert evaluate_guards(None, _inv(), ["required_fields_present"])[0] is True

    def test_blocks_and_lists_missing(self):
        ok, reason = evaluate_guards(
            None, _inv(invoice_number="", total_amount=0), ["required_fields_present"]
        )
        assert ok is False
        assert "invoice_number" in reason and "total_amount" in reason


class TestDuplicateResolved:
    def test_unresolved_blocks(self):
        ok, _ = evaluate_guards(
            None, _inv(potential_duplicate_id="x", duplicate_acknowledged=False),
            ["duplicate_resolved"],
        )
        assert ok is False

    def test_acknowledged_passes(self):
        assert evaluate_guards(
            None, _inv(potential_duplicate_id="x", duplicate_acknowledged=True),
            ["duplicate_resolved"],
        )[0] is True


class TestRegistry:
    def test_unknown_guard_fails_closed(self):
        ok, reason = evaluate_guards(None, _inv(), ["does_not_exist"])
        assert ok is False
        assert "Unknown" in reason

    def test_no_guards_passes(self):
        assert evaluate_guards(None, _inv(), [])[0] is True
