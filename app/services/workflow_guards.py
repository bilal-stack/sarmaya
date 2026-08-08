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
from app.core.enums import VendorStatus


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


GUARD_REGISTRY = {
    "required_fields_present": _required_fields_present,
    "po_has_lines": _po_has_lines,
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
