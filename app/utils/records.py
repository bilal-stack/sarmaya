"""How a record names itself, for anything that has to talk about one.

The models already declare `OBJECT_TYPE` and `REFERENCE_FIELD`, so a module
joins every human-readable surface — audit stories, notifications, the inbox —
by declaring those two attributes rather than by being added to a list in each
place that formats text. The correlation chain had this rule privately; the
notification service instead assumed every record was an invoice and read
`invoice_number` off it, which raised for everything else.
"""


def record_reference(row) -> str:
    """The number a person would quote, falling back to the id."""
    field = getattr(type(row), "REFERENCE_FIELD", None)
    if field:
        value = getattr(row, field, None)
        if value:
            return str(value)
    return str(row.id)


def describe_record(row) -> str:
    """A short human label, e.g. "invoice INV-0042" or "requisition REQ-7"."""
    object_type = getattr(type(row), "OBJECT_TYPE", None) or type(row).__name__.lower()
    return f"{str(object_type).replace('_', ' ')} {record_reference(row)}"
