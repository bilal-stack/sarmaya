"""Demo-video seed: users, vendors, and invoices that make every scene work.

Run after _demo_bootstrap.py. Idempotent — safe to re-run; existing rows are
kept (matched by email / legal_name / invoice_number) and the demo tenant's
workflow config is refreshed to the current defaults (guards + SLA).

Creates, in tenant "demo" (all passwords: demo1234):
  users    manager@demo.com (manager), cfo@demo.com (cfo), clerk@demo.com (ap_clerk)
  vendors  Orion Supplies Ltd (ACTIVE), Meridian Tech (Pvt) Ltd (ACTIVE),
           Nimbus Traders (PENDING_VERIFICATION -> the vendor-gate scene)
  invoices INV-2001 pending, 72h in state  -> OVERDUE vs the 48h SLA
           INV-2002 pending, 790,000       -> routes to CFO
           INV-2003 pending, Nimbus vendor -> blocked on vendor verification
           INV-2004 pending, created by manager -> SoD demo (manager can't approve it)
           INV-2005 approved                -> mark-paid demo
           INV-2006 paid, INV-2007 draft    -> dashboard variety
"""
import uuid
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
import app.models  # noqa: F401
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import Vendor
from app.models.invoice import Invoice
from app.models.workflow_state import WorkflowState
from app.core.enums import UserRole, VendorStatus
from app.core.security import get_password_hash
from app.services.config_defaults import DEFAULT_INVOICE_STATES
from app.utils.datetime_helpers import utc_now

engine = create_engine(settings.ADMIN_DATABASE_URL)
db = sessionmaker(bind=engine)()

tenant = db.query(Tenant).filter_by(slug="demo").first()
if not tenant:
    raise SystemExit("Run _demo_bootstrap.py first (no demo tenant found).")

PW = get_password_hash("demo1234")


def ensure_user(email, name, role):
    u = db.query(User).filter_by(tenant_id=tenant.id, email=email).first()
    if not u:
        u = User(id=uuid.uuid4(), tenant_id=tenant.id, email=email, full_name=name,
                 password=PW, role=role, is_active=True)
        db.add(u)
        db.flush()
        print(f"user    + {email} ({role.value})")
    return u


def ensure_vendor(name, status, created_by, email=None):
    v = db.query(Vendor).filter_by(tenant_id=tenant.id, legal_name=name).first()
    if not v:
        v = Vendor(id=uuid.uuid4(), tenant_id=tenant.id, legal_name=name, status=status,
                   created_by=created_by, email=email)
        db.add(v)
        db.flush()
        print(f"vendor  + {name} ({status.value})")
    return v


def ensure_invoice(number, vendor, amount, state, created_by, hours_in_state=1, **extra):
    inv = db.query(Invoice).filter_by(tenant_id=tenant.id, invoice_number=number).first()
    if not inv:
        inv = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number=number,
            vendor_id=vendor.id, vendor_name=vendor.legal_name,
            invoice_date=date.today() - timedelta(days=5),
            due_date=date.today() + timedelta(days=25),
            total_amount=amount, currency="PKR", current_state=state,
            created_by=created_by,
            state_entered_at=utc_now() - timedelta(hours=hours_in_state),
            **extra,
        )
        db.add(inv)
        db.flush()
        print(f"invoice + {number} {state} {amount:,.0f}")
    return inv


# --- purge previous takes ----------------------------------------------------
# Uploading a PDF on camera creates a real invoice (numbered from the OCR text,
# e.g. INV-1042) plus its file row and audit trail. Those survive a re-seed, so
# the next take's upload hits the exact-duplicate hard block. Delete everything
# that isn't part of the scripted set, in FK-safe order.
import os  # noqa: E402

from app.models.file import File  # noqa: E402
from app.models.audit_log import AuditLog  # noqa: E402
from app.models.ai_action_log import AIActionLog  # noqa: E402

SCRIPTED_NUMBERS = [f"INV-200{i}" for i in range(1, 8)]
SCRIPTED_VENDORS = ["Orion Supplies Ltd", "Meridian Tech (Pvt) Ltd", "Nimbus Traders"]

stale = (
    db.query(Invoice)
    .filter(Invoice.tenant_id == tenant.id, Invoice.invoice_number.notin_(SCRIPTED_NUMBERS))
    .all()
)
if stale:
    stale_ids = [i.id for i in stale]
    file_ids = [i.pdf_file_id for i in stale if i.pdf_file_id]

    # Nothing may point at an invoice we're about to delete.
    db.query(Invoice).filter(Invoice.potential_duplicate_id.in_(stale_ids)).update(
        {Invoice.potential_duplicate_id: None}, synchronize_session=False
    )
    # audit_logs.file_id is a real FK, so these must go before the files do.
    db.query(AuditLog).filter(AuditLog.object_id.in_(stale_ids)).delete(synchronize_session=False)
    if file_ids:
        db.query(AuditLog).filter(AuditLog.file_id.in_(file_ids)).delete(synchronize_session=False)
    db.query(AIActionLog).filter(AIActionLog.object_id.in_(stale_ids)).delete(synchronize_session=False)
    db.query(Invoice).filter(Invoice.id.in_(stale_ids)).delete(synchronize_session=False)

    # Remove the stored PDFs from disk, then their rows.
    if file_ids:
        for f in db.query(File).filter(File.id.in_(file_ids)).all():
            try:
                if f.file_path and os.path.exists(f.file_path):
                    os.remove(f.file_path)
            except OSError:
                pass
        db.query(File).filter(File.id.in_(file_ids)).delete(synchronize_session=False)

    db.flush()
    print(f"purge  - {len(stale)} invoice(s) from previous takes: "
          f"{', '.join(sorted(i.invoice_number for i in stale)[:6])}"
          f"{' ...' if len(stale) > 6 else ''}")

# Vendors auto-created by an upload (an OCR'd name that matched nothing) would
# otherwise pile up in the review queue and clutter the vendor scene.
orphan_vendors = (
    db.query(Vendor)
    .filter(Vendor.tenant_id == tenant.id, Vendor.legal_name.notin_(SCRIPTED_VENDORS))
    .all()
)
for v in orphan_vendors:
    if db.query(Invoice).filter(Invoice.vendor_id == v.id).count() == 0:
        db.query(AuditLog).filter(AuditLog.object_id == v.id).delete(synchronize_session=False)
        db.delete(v)
if orphan_vendors:
    db.flush()
    print(f"purge  - {len(orphan_vendors)} auto-created vendor(s) from previous takes")


# --- users -------------------------------------------------------------------
admin = db.query(User).filter_by(tenant_id=tenant.id, email="admin@demo.com").first()
manager = ensure_user("manager@demo.com", "Maya Manager", UserRole.MANAGER)
cfo = ensure_user("cfo@demo.com", "Farid CFO", UserRole.CFO)
clerk = ensure_user("clerk@demo.com", "Casey Clerk", UserRole.AP_CLERK)

# --- vendors (names match the demo_assets PDFs exactly) ----------------------
orion = ensure_vendor("Orion Supplies Ltd", VendorStatus.ACTIVE, clerk.id,
                      email="orion.supplies@example.com")
meridian = ensure_vendor("Meridian Tech (Pvt) Ltd", VendorStatus.ACTIVE, clerk.id,
                         email="billing@meridiantech.example.com")
nimbus = ensure_vendor("Nimbus Traders", VendorStatus.PENDING_VERIFICATION, clerk.id)

# --- refresh workflow config to current defaults (guards + SLA) --------------
for name, display, order, is_initial, is_final, transitions, color, guards, sla in DEFAULT_INVOICE_STATES:
    s = db.query(WorkflowState).filter_by(
        tenant_id=tenant.id, workflow_type="invoice", state_name=name
    ).first()
    if s:
        s.allowed_transitions, s.guards, s.sla = transitions, guards, sla
    else:
        db.add(WorkflowState(
            tenant_id=tenant.id, workflow_type="invoice", state_name=name,
            display_name=display, state_order=order, is_initial=is_initial,
            is_final=is_final, allowed_transitions=transitions, guards=guards,
            sla=sla, color=color,
        ))
print("workflow config refreshed (guards + 48h SLA on pending_approval -> CFO)")

# --- invoices ----------------------------------------------------------------
ensure_invoice("INV-2001", orion, 145_000, "pending_approval", clerk.id, hours_in_state=72)
ensure_invoice("INV-2002", meridian, 790_000, "pending_approval", clerk.id, hours_in_state=5)
ensure_invoice("INV-2003", nimbus, 98_000, "pending_approval", clerk.id, hours_in_state=8)
ensure_invoice("INV-2004", orion, 60_000, "pending_approval", manager.id, hours_in_state=3)
ensure_invoice("INV-2005", meridian, 210_000, "approved", clerk.id, hours_in_state=20,
               approved_by=admin.id if admin else None, approved_at=utc_now() - timedelta(hours=20))
ensure_invoice("INV-2006", orion, 33_500, "paid", clerk.id, hours_in_state=48)
ensure_invoice("INV-2007", meridian, 12_000, "draft", clerk.id, hours_in_state=2)

# --- reset to camera-ready state (safe between takes) ------------------------
# Re-runnable: restore the scripted invoices to pending with fresh timers, and
# clear prior SLA escalations so the escalation scene shows a real escalation
# again (the runner is idempotent per state entry).
scripted = {
    "INV-2001": ("pending_approval", 72),
    "INV-2002": ("pending_approval", 5),
    "INV-2003": ("pending_approval", 8),
    "INV-2004": ("pending_approval", 3),
    "INV-2005": ("approved", 20),
}
for number, (state, hours) in scripted.items():
    inv = db.query(Invoice).filter_by(tenant_id=tenant.id, invoice_number=number).first()
    if inv:
        inv.current_state = state
        inv.state_entered_at = utc_now() - timedelta(hours=hours)
        if state != "approved":
            inv.approved_by = inv.approved_at = None
        inv.duplicate_acknowledged = False

cleared = (
    db.query(AuditLog)
    .filter(AuditLog.tenant_id == tenant.id, AuditLog.action == "sla_escalated")
    .delete(synchronize_session=False)
)
print(f"reset: scripted invoices restored, {cleared} prior escalation event(s) cleared")

db.commit()
print("OK - demo data ready. Logins: admin/manager/cfo/clerk @demo.com, password demo1234")
