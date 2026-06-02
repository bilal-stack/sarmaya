"""Per-field confidence + "Why?" explanations for extracted invoice fields.

The MVP spec requires that, alongside each extracted field, the UI shows a
confidence score and a short "Why?" snippet — the line of source text the value
was taken from. This module derives that explanation from the raw OCR text and
the final (post-AI) field values, so it works regardless of which provider did
the extraction.

It is a pure function with no I/O, which keeps it cheap to unit-test.
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# The six fields the MVP spec extracts, in display order.
EXPLAINED_FIELDS = (
    "vendor_name",
    "invoice_number",
    "invoice_date",
    "total_amount",
    "tax_amount",
    "currency",
)

# Confidence levels (0-100) for the three evidence outcomes.
_CONF_EVIDENCED = 90  # value present AND a matching source line was located
_CONF_UNSUPPORTED = 40  # value present but no source line backs it up
_CONF_MISSING = 0  # no value extracted

_CURRENCY_TOKENS = {
    "PKR": ["pkr", "rs.", "rs ", "₨"],
    "USD": ["usd", "us$", "$"],
    "EUR": ["eur", "€"],
    "GBP": ["gbp", "£"],
}


def build_field_explanations(
    raw_text: Optional[str], fields: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Return ``{field: {value, confidence, source, reason}}`` for each of the
    six spec fields, locating the source line in ``raw_text`` when possible."""
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    return {
        field: _explain(field, fields.get(field), lines)
        for field in EXPLAINED_FIELDS
    }


def _explain(field: str, value: Any, lines: List[str]) -> Dict[str, Any]:
    if _is_blank(value):
        return _result(value, _CONF_MISSING, None, "Not found in the document")

    source = _find_source(field, value, lines)
    if source:
        return _result(value, _CONF_EVIDENCED, source, f"Matched text: \"{source}\"")
    return _result(
        value,
        _CONF_UNSUPPORTED,
        None,
        "Extracted, but no matching line was found in the document text",
    )


def _result(value: Any, confidence: int, source: Optional[str], reason: str) -> Dict[str, Any]:
    return {
        "value": _serialize(value),
        "confidence": confidence,
        "source": source,
        "reason": reason,
    }


def _find_source(field: str, value: Any, lines: List[str]) -> Optional[str]:
    """Find the first source line that evidences ``value``."""
    if field in ("total_amount", "tax_amount"):
        return _find_amount_source(value, lines)
    if field == "currency":
        return _find_currency_source(value, lines)
    if field == "invoice_date":
        return _find_date_source(value, lines)
    # vendor_name / invoice_number: plain case-insensitive substring match.
    return _find_text_source(str(value), lines)


def _find_text_source(needle: str, lines: List[str]) -> Optional[str]:
    needle = needle.strip().lower()
    if not needle:
        return None
    for line in lines:
        if needle in line.lower():
            return line
    return None


def _find_amount_source(value: Any, lines: List[str]) -> Optional[str]:
    """Match an amount against common textual renderings (grouped, decimals)."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None

    variants = {f"{amount:.2f}", f"{amount:,.2f}"}
    if amount == int(amount):
        whole = int(amount)
        variants.update({str(whole), f"{whole:,}"})

    for line in lines:
        if any(v in line for v in variants):
            return line
    return None


def _find_currency_source(value: Any, lines: List[str]) -> Optional[str]:
    code = str(value).strip().upper()
    tokens = _CURRENCY_TOKENS.get(code, [code.lower()])
    for line in lines:
        low = line.lower()
        if any(tok in low for tok in tokens):
            return line
    return None


def _find_date_source(value: Any, lines: List[str]) -> Optional[str]:
    """Match a date against several common textual formats."""
    candidates: List[str] = []
    if isinstance(value, (date, datetime)):
        d = value.date() if isinstance(value, datetime) else value
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%m-%d-%Y", "%m/%d/%Y",
                    "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
            candidates.append(d.strftime(fmt))
    else:
        candidates.append(str(value))

    candidates = [c for c in candidates if c]
    for line in lines:
        low = line.lower()
        if any(c.lower() in low for c in candidates):
            return line
    return None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return True
    return False


def _serialize(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
