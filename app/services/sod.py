"""Segregation of Duties (SoD) checks.

Build Book non-negotiable (maker-checker, lines 190-193): the person who makes a
request must not be the one who approves it. Two rules are enforced today:

  * you cannot approve an invoice you created;
  * you cannot activate a vendor you created.

Admins are treated as the Build Book's "unless explicitly allowed" carve-out and
are exempt. Finer-grained configuration (per-rule toggles, amount thresholds, and
is a documented follow-up. The vendor-bank-change rule is implemented
below.
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


def violates_self_release(prepared_by, current_user: dict) -> bool:
    """Maker-checker on a payment run.

    Deliberately has no admin exemption, unlike the approval rules above.
    Those carve-outs exist so a one-person demo tenant still functions; this
    one guards the moment money leaves, and an admin releasing their own run is
    precisely the action the control exists to prevent.
    """
    return _same_person(prepared_by, current_user.get("id"))


def violates_self_reconciliation(released_by, current_user: dict) -> bool:
    """True if the person who released a payment is confirming it cleared.

    Reconciliation is the check on the release, not a continuation of it. One
    person holding both controls the instruction and the evidence that the
    instruction was correct — which is how a misdirected payment stays hidden.

    No admin exemption, for the same reason as the release rule: this guards
    the verification of money that has already left.
    """
    return _same_person(released_by, current_user.get("id"))


def violates_self_bank_change_approval(requested_by, current_user: dict) -> bool:
    """True if the person who requested a vendor bank change is approving it.

    No admin exemption, unlike the invoice and vendor rules. Those carve-outs
    exist so a one-person demo tenant still functions, and their cost is
    bounded because a wrongly approved invoice still meets every downstream
    control. This one has no downstream control: once the account is changed,
    the next genuine invoice, genuinely approved and genuinely released, pays
    the wrong person. A second pair of eyes is the entire mechanism.
    """
    return _same_person(requested_by, current_user.get("id"))
