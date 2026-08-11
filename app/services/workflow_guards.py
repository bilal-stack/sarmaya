"""Named, configurable transition guards for the workflow engine.

The Build Book's state-machine template (lines 139-147) declares guards per
transition. A transition only fires if all its configured guards pass. Guards
are referenced by name in the workflow config (versioned), and resolved here —
so the *what-must-be-true* of a process lives in config, not scattered in code.

Each guard is `f(db, obj) -> (ok: bool, reason: str)`. Unknown guard names fail
closed (a transition referencing a missing guard is blocked, not silently
allowed).
"""
from app.models.vendor import Vendor
from app.core.enums import VendorStatus, PaymentState


def _required_fields_present(db, obj):
    missing = []
    if not getattr(obj, "vendor_id", None):
        missing.append("vendor")
    if not (getattr(obj, "invoice_number", None) or "").strip():
        missing.append("invoice_number")
    if not getattr(obj, "invoice_date", None):
        missing.append("invoice_date")
    amount = getattr(obj, "total_amount", None)
    if amount is None or amount <= 0:
        missing.append("total_amount")
    if missing:
        return False, "Missing required fields: " + ", ".join(missing)
    return True, ""


def _vendor_active(db, obj):
    """Shared by invoices and purchase orders, so the wording names neither.

    On a PO this gates `issued`: once an order reaches the vendor, goods can
    arrive and a liability exists. Catching an unverified vendor at invoice
    approval is already too late to have prevented that.
    """
    vendor_id = getattr(obj, "vendor_id", None)
    if not vendor_id:
        return False, "Not linked to a vendor master record"
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        return False, "Linked vendor no longer exists"
    status = vendor.status.value if hasattr(vendor.status, "value") else vendor.status
    if status != VendorStatus.ACTIVE.value:
        return False, f"Vendor is {status}, not active"
    return True, ""


def _duplicate_resolved(db, obj):
    if getattr(obj, "potential_duplicate_id", None) and not getattr(obj, "duplicate_acknowledged", False):
        return False, "Invoice is flagged as a potential duplicate; resolve it first"
    return True, ""


def _po_has_lines(db, obj):
    """A purchase order with no lines commits to nothing and cannot be matched
    against a receipt or an invoice later, so it must not reach approval."""
    lines = getattr(obj, "lines", None) or []
    if not lines:
        return False, "Purchase order has no lines"
    total = getattr(obj, "total_amount", None)
    if total is None or total <= 0:
        return False, "Purchase order total must be greater than zero"
    return True, ""


def _payment_has_lines(db, obj):
    """A run with no lines pays nobody and would export an empty bank file."""
    lines = getattr(obj, "lines", None) or []
    if not lines:
        return False, "Payment has no invoices to settle"
    total = getattr(obj, "total_amount", None)
    if total is None or total <= 0:
        return False, "Payment total must be greater than zero"
    return True, ""


def _payment_lines_still_payable(db, obj):
    """Re-check at release that every line is still safe to pay.

    A run can sit awaiting release for days. In that time an invoice can be
    paid by another run, a vendor can be blocked, or an approval can be undone.
    Checking only at preparation would authorise a picture of the world that
    has since changed — and release is the irreversible step.
    """
    from app.core.enums import InvoiceState, VendorStatus
    from app.models.invoice import Invoice
    from app.models.payment import Payment, PaymentLine

    problems = []
    for line in getattr(obj, "lines", None) or []:
        invoice = db.query(Invoice).filter(Invoice.id == line.invoice_id).first()
        if not invoice:
            problems.append(f"{line.vendor_name}: the invoice no longer exists")
            continue

        state = str(getattr(invoice.current_state, "value", invoice.current_state)).lower()
        if state == InvoiceState.PAID.value:
            problems.append(f"{invoice.invoice_number} has already been paid")
            continue
        if state != InvoiceState.APPROVED.value:
            problems.append(f"{invoice.invoice_number} is {state}, not approved")
            continue

        if invoice.vendor_id:
            vendor = db.query(Vendor).filter(Vendor.id == invoice.vendor_id).first()
            status = getattr(vendor.status, "value", vendor.status) if vendor else None
            if status != VendorStatus.ACTIVE.value:
                problems.append(
                    f"{invoice.invoice_number}: vendor is {status or 'missing'}, not active"
                )
                continue

        # An instruction with no destination account is not payable. The bank
        # rejects it at best and silently drops the line at worst, so the run
        # must not reach `released` looking authorised.
        if not (line.iban or line.bank_account_number):
            problems.append(
                f"{invoice.invoice_number}: {line.vendor_name} has no bank account "
                "on file, so there is nowhere to send the money"
            )
            continue

        # Claimed by another run that is already released or awaiting release.
        clash = (
            db.query(PaymentLine)
            .join(Payment, Payment.id == PaymentLine.payment_id)
            .filter(
                PaymentLine.invoice_id == line.invoice_id,
                PaymentLine.payment_id != obj.id,
                Payment.current_state.in_([
                    PaymentState.RELEASED.value, PaymentState.PENDING_RELEASE.value,
                ]),
            )
            .first()
        )
        if clash:
            problems.append(
                f"{invoice.invoice_number} is already on another payment run"
            )

    if problems:
        return False, "Cannot release: " + "; ".join(problems)
    return True, ""


GUARD_REGISTRY = {
    "required_fields_present": _required_fields_present,
    "po_has_lines": _po_has_lines,
    "payment_has_lines": _payment_has_lines,
    "payment_lines_still_payable": _payment_lines_still_payable,
    "vendor_active": _vendor_active,
    "duplicate_resolved": _duplicate_resolved,
}


def evaluate_guards(db, obj, guard_names):
    """Run the named guards in order; return (ok, reason). First failure wins;
    an unknown guard name fails closed."""
    for name in guard_names or []:
        guard = GUARD_REGISTRY.get(name)
        if guard is None:
            return False, f"Unknown transition guard '{name}'"
        ok, reason = guard(db, obj)
        if not ok:
            return False, reason
    return True, ""
