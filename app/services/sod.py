"""Segregation of Duties (SoD) checks.

Build Book non-negotiable (maker-checker, lines 190-193): the person who makes a
request must not be the one who approves it. Two rules are enforced today:

  * you cannot approve an invoice you created;
  * you cannot activate a vendor you created.

Admins are treated as the Build Book's "unless explicitly allowed" carve-out and
are exempt. Finer-grained configuration (per-rule toggles, amount thresholds, and
the vendor-bank-change rule) is a documented follow-up.
"""
from app.core.roles import ADMIN


def _is_admin(current_user: dict) -> bool:
    return (current_user.get("role") or "").strip().lower() == ADMIN


def _same_person(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def violates_self_approval(record, current_user: dict) -> bool:
    """Maker-checker: True if a non-admin is approving something they created.

    Applies to any record carrying created_by — invoices and purchase orders
    both do. The rule was never invoice-specific; only its name was.
    """
    if _is_admin(current_user):
        return False
    return _same_person(getattr(record, "created_by", None), current_user.get("id"))


def violates_self_invoice_approval(invoice, current_user: dict) -> bool:
    """Maker-checker for invoices. Retained as the name the invoice service
    already calls; delegates to the general rule."""
    return violates_self_approval(invoice, current_user)


def violates_self_vendor_activation(vendor, current_user: dict) -> bool:
    """True if a non-admin is activating a vendor they created."""
    if _is_admin(current_user):
        return False
    return _same_person(getattr(vendor, "created_by", None), current_user.get("id"))
