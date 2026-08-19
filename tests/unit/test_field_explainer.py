"""Unit tests for OCR per-field "Why?" explanations
(app/services/ocr/field_explainer.py).

The explainer derives, for each of the six spec fields, a confidence score and
the source line of OCR text the value came from. Pure logic, no I/O.
"""
from datetime import date


from app.services.ocr.field_explainer import (
    build_field_explanations,
    EXPLAINED_FIELDS,
    _CONF_EVIDENCED,
    _CONF_UNSUPPORTED,
    _CONF_MISSING,
)

RAW_TEXT = (
    "ACME LIMITED\n"
    "Invoice No: INV-1001\n"
    "Date: 14-03-2026\n"
    "Subtotal: PKR 1,000.00\n"
    "GST: PKR 200.00\n"
    "Total: PKR 1,200.00\n"
)

FIELDS = {
    "vendor_name": "ACME LIMITED",
    "invoice_number": "INV-1001",
    "invoice_date": date(2026, 3, 14),
    "total_amount": 1200.0,
    "tax_amount": 200.0,
    "currency": "PKR",
}


def test_all_six_fields_explained():
    exp = build_field_explanations(RAW_TEXT, FIELDS)
    assert set(exp.keys()) == set(EXPLAINED_FIELDS)


def test_present_fields_are_evidenced_with_source_line():
    exp = build_field_explanations(RAW_TEXT, FIELDS)

    assert exp["vendor_name"]["confidence"] == _CONF_EVIDENCED
    assert exp["vendor_name"]["source"] == "ACME LIMITED"

    assert exp["invoice_number"]["source"] == "Invoice No: INV-1001"
    assert exp["total_amount"]["source"] == "Total: PKR 1,200.00"
    assert exp["tax_amount"]["source"] == "GST: PKR 200.00"
    assert exp["invoice_date"]["source"] == "Date: 14-03-2026"
    assert exp["currency"]["confidence"] == _CONF_EVIDENCED


def test_date_value_is_serialized_to_iso():
    exp = build_field_explanations(RAW_TEXT, FIELDS)
    assert exp["invoice_date"]["value"] == "2026-03-14"


def test_missing_field_has_zero_confidence_and_reason():
    fields = {**FIELDS, "invoice_number": ""}
    exp = build_field_explanations(RAW_TEXT, fields)

    assert exp["invoice_number"]["confidence"] == _CONF_MISSING
    assert exp["invoice_number"]["source"] is None
    assert "not found" in exp["invoice_number"]["reason"].lower()


def test_zero_amount_treated_as_missing():
    fields = {**FIELDS, "tax_amount": 0.0}
    exp = build_field_explanations(RAW_TEXT, fields)
    assert exp["tax_amount"]["confidence"] == _CONF_MISSING


def test_value_without_supporting_text_is_low_confidence():
    # Vendor name is correct but never appears in the OCR text.
    fields = {**FIELDS, "vendor_name": "Phantom Corp"}
    exp = build_field_explanations(RAW_TEXT, fields)

    assert exp["vendor_name"]["confidence"] == _CONF_UNSUPPORTED
    assert exp["vendor_name"]["source"] is None


def test_amount_matches_without_grouping_separators():
    raw = "Grand Total 1200.00 PKR"
    fields = {**FIELDS, "total_amount": 1200.0}
    exp = build_field_explanations(raw, fields)
    assert exp["total_amount"]["source"] == "Grand Total 1200.00 PKR"


def test_empty_raw_text_marks_present_values_unsupported():
    exp = build_field_explanations("", FIELDS)
    assert exp["vendor_name"]["confidence"] == _CONF_UNSUPPORTED
    assert exp["total_amount"]["confidence"] == _CONF_UNSUPPORTED
