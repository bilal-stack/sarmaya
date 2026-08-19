"""Generate a realistic dataset to demo against and to validate dashboards.

Build Book Definition of Done: *"Reports and dashboards for that variant
shipped and validated against seed dataset."* A dashboard built against an
empty database is not validated, it is unfalsifiable — every panel reads zero
and nothing tells you whether the query is right or the data is missing.

So this writes ninety days of plausible history: invoices that flowed through
cleanly, invoices stuck with an approver, ones blocked on an unverified vendor,
duplicates caught, payment runs released and pending, bank lines that matched
and three that did not, a vendor bank change, and the watchlist alerts that go
with it. The shape is deliberately uneven — a dashboard whose every bar is the
same height demonstrates nothing.

**On writing history.** The audit trail is hash-chained and the timestamp is
inside the hash, so entries cannot be written now and backdated afterwards
without the chain reporting tampering — which would leave the demo data failing
the product's own integrity check, the one feature you would most want to show.
Entries are therefore built with their historical timestamp and then chained
normally, in chronological order per object. `log_audit` deliberately does not
take a timestamp: a production function that lets its caller choose when
something happened is not an audit trail.

    python -m scripts.seed_demo_data --confirm

Refuses to run without --confirm, and refuses outright on a database that
already holds invoices, so it cannot quietly double a real dataset.
"""
import argparse
import logging
import random
import sys
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.core.database import SessionLocal, set_tenant_context
from app.core.enums import (
    InvoiceState, PaymentState, UserRole, VendorStatus, BankChangeState,
)
from app.models.audit_log import AuditLog
from app.models.ai_action_log import AIActionLog
from app.models.bank_statement import BankStatement, BankStatementLine
from app.models.file import File
from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentLine
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_bank_change import VendorBankChange
from app.models.watchlist_alert import WatchlistAlert
from app.services.audit_integrity import append_to_chain
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("seed")


def _now() -> datetime:
    """Naive UTC, matching every timestamp column in this schema.

    Not `datetime.utcnow()`: deprecated, and this project already learned the
    hard way (DR-020) that mixing aware and naive values here silently stores
    local time in columns everything else reads as UTC.
    """
    return make_naive(to_utc(utc_now()))

#: Fixed so two runs produce the same dataset. A demo that looks different
#: every time is one nobody can write notes against.
SEED = 20260819
DAYS = 90

VENDORS = [
    ("Orion Supplies Ltd", VendorStatus.ACTIVE, "PK36SCBL0000001123456702"),
    ("Meridian Tech (Pvt) Ltd", VendorStatus.ACTIVE, "PK11HABB0000002234567813"),
    ("Kestrel Logistics", VendorStatus.ACTIVE, "PK22UBLA0000003345678924"),
    ("Northwind Facilities", VendorStatus.ACTIVE, "PK33MCBL0000004456789035"),
    ("Ashgrove Print & Media", VendorStatus.ACTIVE, "PK44ALFH0000005567890146"),
    ("Copperline Industrial", VendorStatus.ACTIVE, "PK55BAHL0000006678901257"),
    ("Ridgeway Consulting", VendorStatus.PENDING_VERIFICATION, None),
    ("Silverbrook Trading", VendorStatus.BLOCKED, "PK66FAYS0000007789012368"),
]


def _audit(db, tenant_id, user, obj_type, obj_id, action, at, **kw):
    """One audit entry, dated, then chained.

    Mirrors log_audit except that the timestamp is chosen rather than "now",
    which is the whole reason this lives in a seed script and not in the
    application.
    """
    entry = AuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user.id if user else None,
        user_email=user.email if user else None,
        user_role=getattr(user.role, "value", user.role) if user else None,
        object_type=obj_type,
        object_id=obj_id,
        action=action,
        timestamp=at,
        custom_metadata={},
        **kw,
    )
    db.add(entry)
    db.flush()
    db.refresh(entry)
    append_to_chain(db, entry)
    db.flush()
    return entry


def _people(db, tenant):
    """The cast. Created if missing so the script works on a bootstrapped
    tenant that only has an administrator."""
    from app.core.security import get_password_hash

    wanted = [
        ("clerk@demo.com", "Casey Clerk", UserRole.AP_CLERK),
        ("manager@demo.com", "Morgan Manager", UserRole.MANAGER),
        ("cfo@demo.com", "Frances Finance", UserRole.CFO),
        ("auditor@demo.com", "Ada Auditor", UserRole.AUDITOR),
    ]
    people = {}
    for email, name, role in wanted:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(
                id=uuid.uuid4(), tenant_id=tenant.id, email=email, full_name=name,
                role=role, password=get_password_hash("DemoLocal!2026"),
                is_active=True,
            )
            db.add(user)
            db.flush()
        people[role.value] = user

    admin = db.query(User).filter(User.role == UserRole.ADMIN).first()
    people["admin"] = admin
    return people


def _vendors(db, tenant, people, start):
    created = []
    for index, (name, status, iban) in enumerate(VENDORS):
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name=name, status=status,
            vendor_code=f"V-{1000 + index}",
            email=f"ap@{name.split()[0].lower()}.example",
            iban=iban, bank_account_name=name if iban else None,
            bank_name="Standard Chartered" if iban else None,
            created_by=people["ap_clerk"].id,
        )
        db.add(vendor)
        db.flush()
        at = start + timedelta(days=index)
        _audit(db, tenant.id, people["ap_clerk"], "vendor", vendor.id, "created", at,
               after_value={"legal_name": name, "status": status.value})
        if status == VendorStatus.ACTIVE:
            _audit(db, tenant.id, people["manager"], "vendor", vendor.id,
                   "status_changed", at + timedelta(hours=6),
                   before_value={"status": "pending_verification"},
                   after_value={"status": "active"})
        created.append(vendor)
    return created


def _invoices(db, tenant, people, vendors, rng, start):
    """The bulk of the dataset, shaped so the dashboards have something to say.

    Roughly: most flow through, a tenth stall with an approver, a few are
    rejected, a few are blocked on an unverified or blocked vendor, and a
    couple are flagged as duplicates.
    """
    clerk, manager, cfo = people["ap_clerk"], people["manager"], people["cfo"]
    active = [v for v in vendors if v.status == VendorStatus.ACTIVE]
    blocked = [v for v in vendors if v.status != VendorStatus.ACTIVE]

    invoices = []
    for n in range(110):
        raised = start + timedelta(days=rng.randint(0, DAYS - 1),
                                   hours=rng.randint(8, 17))
        # A long tail of small invoices with a few large ones, which is what
        # real AP looks like and what makes an approval-threshold chart useful.
        amount = rng.choice([
            rng.randint(2_000, 40_000), rng.randint(2_000, 40_000),
            rng.randint(40_000, 240_000), rng.randint(260_000, 900_000),
        ])
        roll = rng.random()
        use_blocked = roll > 0.94 and blocked
        vendor = rng.choice(blocked if use_blocked else active)

        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id,
            invoice_number=f"INV-{2600 + n}",
            vendor_name=vendor.legal_name, vendor_id=vendor.id,
            invoice_date=(raised - timedelta(days=rng.randint(1, 10))).date(),
            total_amount=amount,
            tax_amount=round(amount * 0.17, 2),
            cost_center=rng.choice(["CC-OPS", "CC-IT", "CC-FAC", "CC-MKT"]),
            current_state=InvoiceState.DRAFT,
            created_by=clerk.id,
            state_entered_at=raised,
        )
        db.add(invoice)
        db.flush()
        _audit(db, tenant.id, clerk, "invoice", invoice.id, "created", raised,
               workflow_type="invoice", workflow_step="draft",
               after_value={"total_amount": str(amount)})

        # --- validated ---
        validated_at = raised + timedelta(hours=rng.randint(1, 20))
        invoice.current_state = InvoiceState.VALIDATED
        invoice.state_entered_at = validated_at
        _audit(db, tenant.id, clerk, "invoice", invoice.id, "validated", validated_at,
               workflow_type="invoice", workflow_step="validated")

        if use_blocked:
            # Blocked at the governance gate — an exception, not a failure.
            _audit(db, tenant.id, clerk, "invoice", invoice.id,
                   "approval_blocked", validated_at + timedelta(hours=2),
                   workflow_type="invoice", workflow_step="validated",
                   comment=f"Blocked: vendor_{vendor.status.value}",
                   after_value={"reason": f"vendor_{vendor.status.value}",
                                "vendor_name": vendor.legal_name})
            invoices.append(invoice)
            continue

        # --- submitted ---
        submitted_at = validated_at + timedelta(hours=rng.randint(1, 30))
        approver = cfo if amount > 250_000 else manager
        invoice.current_state = InvoiceState.PENDING_APPROVAL
        invoice.state_entered_at = submitted_at
        _audit(db, tenant.id, clerk, "invoice", invoice.id,
               "submitted_for_approval", submitted_at,
               workflow_type="invoice", workflow_step="pending_approval",
               after_value={"required_role": getattr(approver.role, "value", ""),
                            "policy_name": "CFO approval over 250k" if amount > 250_000
                            else "Manager approval up to 250k"})

        if roll > 0.90:
            # Still sitting with the approver. Ages vary, so "how long has this
            # been waiting" has a distribution rather than a single value.
            invoices.append(invoice)
            continue

        # Deliberately slow for a slice of them, so cycle-time charts show a
        # spread rather than one flat number.
        decided_at = submitted_at + timedelta(
            hours=rng.choice([2, 5, 9, 18, 30, 52, 96, 140])
        )
        if decided_at > _now():
            invoices.append(invoice)
            continue

        if roll > 0.84:
            invoice.current_state = InvoiceState.REJECTED
            invoice.state_entered_at = decided_at
            _audit(db, tenant.id, approver, "invoice", invoice.id, "rejected",
                   decided_at, workflow_type="invoice", workflow_step="rejected",
                   comment=rng.choice([
                       "No purchase order referenced.",
                       "Amount disagrees with the delivery note.",
                       "Wrong cost centre; resubmit against CC-OPS.",
                   ]))
            invoices.append(invoice)
            continue

        invoice.current_state = InvoiceState.APPROVED
        invoice.state_entered_at = decided_at
        _audit(db, tenant.id, approver, "invoice", invoice.id, "approved",
               decided_at, workflow_type="invoice", workflow_step="approved",
               after_value={"approved_by_role": getattr(approver.role, "value", "")})
        invoices.append(invoice)

    return invoices


def _duplicates(db, tenant, people, invoices, rng):
    """A few flagged pairs, one of them overridden with a reason — which is
    what the policy-overrides dashboard is counting."""
    approved = [i for i in invoices if i.current_state == InvoiceState.APPROVED]
    for original in rng.sample(approved, min(4, len(approved))):
        original.potential_duplicate_id = None
        copy_at = original.state_entered_at + timedelta(days=1)
        duplicate = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id,
            invoice_number=f"{original.invoice_number}-A",
            vendor_name=original.vendor_name, vendor_id=original.vendor_id,
            invoice_date=original.invoice_date, total_amount=original.total_amount,
            current_state=InvoiceState.PENDING_APPROVAL,
            created_by=people["ap_clerk"].id,
            potential_duplicate_id=original.id,
            duplicate_acknowledged=False,
            state_entered_at=copy_at,
        )
        db.add(duplicate)
        db.flush()
        _audit(db, tenant.id, people["ap_clerk"], "invoice", duplicate.id,
               "duplicate_flagged", copy_at, workflow_type="invoice",
               comment=f"Possible duplicate of {original.invoice_number}",
               after_value={"potential_duplicate_id": str(original.id)})

        if rng.random() > 0.5:
            # Overridden with a stated reason: a policy override, which is a
            # thing to count rather than a thing to hide.
            duplicate.duplicate_acknowledged = True
            _audit(db, tenant.id, people["manager"], "invoice", duplicate.id,
                   "duplicate_acknowledged", copy_at + timedelta(hours=4),
                   workflow_type="invoice",
                   comment="Vendor re-issued with a corrected tax line; both are payable.")


def _payments(db, tenant, people, invoices, rng, start):
    clerk, cfo = people["ap_clerk"], people["cfo"]
    approved = [i for i in invoices if i.current_state == InvoiceState.APPROVED]
    rng.shuffle(approved)

    runs = []
    batch_index = 0
    while approved and batch_index < 6:
        batch = [approved.pop() for _ in range(min(rng.randint(2, 5), len(approved)))]
        prepared_at = max(i.state_entered_at for i in batch) + timedelta(hours=6)
        if prepared_at > _now():
            break

        payment = Payment(
            id=uuid.uuid4(), tenant_id=tenant.id,
            payment_number=f"PAY-{100 + batch_index}",
            payment_date=prepared_at.date(),
            total_amount=sum(i.total_amount for i in batch),
            currency="PKR",
            current_state=PaymentState.DRAFT,
            prepared_by=clerk.id,
            state_entered_at=prepared_at,
        )
        db.add(payment)
        db.flush()
        for line_no, invoice in enumerate(batch, start=1):
            vendor = db.query(Vendor).filter(Vendor.id == invoice.vendor_id).first()
            db.add(PaymentLine(
                id=uuid.uuid4(), tenant_id=tenant.id, payment_id=payment.id,
                line_number=line_no, invoice_id=invoice.id,
                amount=invoice.total_amount, vendor_id=invoice.vendor_id,
                vendor_name=invoice.vendor_name,
                iban=getattr(vendor, "iban", None),
                bank_account_name=getattr(vendor, "bank_account_name", None),
                bank_name=getattr(vendor, "bank_name", None),
            ))
        _audit(db, tenant.id, clerk, "payment", payment.id, "prepared", prepared_at,
               workflow_type="payment", workflow_step="draft")

        submitted_at = prepared_at + timedelta(hours=rng.randint(1, 8))
        payment.current_state = PaymentState.PENDING_RELEASE
        payment.state_entered_at = submitted_at
        _audit(db, tenant.id, clerk, "payment", payment.id, "submitted_for_release",
               submitted_at, workflow_type="payment", workflow_step="pending_release")

        # One run left awaiting release, so the inbox and the dashboards both
        # have something outstanding.
        if batch_index < 4:
            released_at = submitted_at + timedelta(hours=rng.randint(2, 26))
            if released_at <= _now():
                payment.current_state = PaymentState.RELEASED
                payment.released_by = cfo.id
                payment.released_at = released_at
                payment.state_entered_at = released_at
                _audit(db, tenant.id, cfo, "payment", payment.id, "released",
                       released_at, workflow_type="payment", workflow_step="released")
                for invoice in batch:
                    invoice.current_state = InvoiceState.PAID
                    invoice.state_entered_at = released_at
                    _audit(db, tenant.id, cfo, "invoice", invoice.id, "marked_paid",
                           released_at, workflow_type="invoice", workflow_step="paid")
        runs.append(payment)
        batch_index += 1
    return runs


def _bank(db, tenant, people, runs, rng, start):
    """A statement that mostly reconciles, and three debits that do not.

    The unexplained ones are the point: they are the highest-priority item in
    the Decision Inbox and the headline of Reconciliation Health.
    """
    clerk = people["ap_clerk"]
    imported_at = _now() - timedelta(days=2)
    statement = BankStatement(
        id=uuid.uuid4(), tenant_id=tenant.id,
        statement_reference="STMT-2026-08", source_format="csv",
        file_hash=uuid.uuid4().hex * 2, imported_by=clerk.id,
    )
    db.add(statement)
    db.flush()

    line_no = 0
    for payment in runs:
        if payment.current_state != PaymentState.RELEASED:
            continue
        line_no += 1
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id, bank_statement_id=statement.id,
            line_number=line_no, value_date=payment.released_at.date(),
            amount=payment.total_amount, is_debit=True,
            description=f"TRANSFER {payment.payment_number}",
            bank_reference=f"TRF-{5000 + line_no}",
            matched_payment_id=payment.id,
        ))

    for n, (amount, description) in enumerate([
        (410_000, "TRANSFER TO ACCT 8891"),
        (86_500, "SEPA DD RECURRING 4471"),
        (12_940, "CARD SETTLEMENT MISC"),
    ], start=1):
        line_no += 1
        db.add(BankStatementLine(
            id=uuid.uuid4(), tenant_id=tenant.id, bank_statement_id=statement.id,
            line_number=line_no,
            value_date=(imported_at - timedelta(days=n * 3)).date(),
            amount=amount, is_debit=True, description=description,
            bank_reference=f"UNK-{900 + n}",
        ))
    _audit(db, tenant.id, clerk, "bank_statement", statement.id, "imported",
           imported_at, comment=f"{line_no} lines imported.")


def _bank_change_and_watchlist(db, tenant, people, vendors, rng):
    """One applied bank change with the alerts it raises, so the watchlist and
    the overrides dashboard are not empty."""
    clerk, manager = people["ap_clerk"], people["manager"]
    vendor = vendors[0]
    requested_at = _now() - timedelta(days=12)

    change = VendorBankChange(
        id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
        reason="Vendor emailed new account details on headed paper; confirmed by "
               "phone on the number already on file.",
        old_iban=vendor.iban, new_iban="PK24ALFH0000009988776655",
        current_state=BankChangeState.EFFECTIVE,
        requested_by=clerk.id, requested_at=requested_at,
        approved_by=manager.id, approved_at=requested_at + timedelta(hours=5),
        effective_at=requested_at + timedelta(hours=29),
        applied_at=requested_at + timedelta(hours=30),
        applied_by=clerk.id,
    )
    db.add(change)
    vendor.iban = change.new_iban
    db.flush()

    for event, offset, actor in [
        ("bank_change_requested", 0, clerk),
        ("bank_change_approved", 5, manager),
        ("bank_change_applied", 30, clerk),
    ]:
        _audit(db, tenant.id, actor, "vendor", vendor.id, event,
               requested_at + timedelta(hours=offset),
               comment=change.reason if event.endswith("requested") else None)

    for event, offset, severity in [
        ("requested", 0, "high"), ("approved", 5, "high"), ("applied", 30, "high"),
    ]:
        db.add(WatchlistAlert(
            id=uuid.uuid4(), tenant_id=tenant.id,
            category="vendor_bank_change", severity=severity,
            object_type="vendor_bank_change", object_id=change.id,
            summary=f"Bank details for {vendor.legal_name} — {event}",
            detail={"event": event, "vendor": vendor.legal_name,
                    "old_iban": "••••6702", "new_iban": "••••6655"},
            actor_id=clerk.id,
            created_at=requested_at + timedelta(hours=offset),
        ))

    # One master-data edit, reviewed, so the watchlist shows both states.
    reviewed = WatchlistAlert(
        id=uuid.uuid4(), tenant_id=tenant.id,
        category="master_data_edit", severity="medium",
        object_type="vendor", object_id=vendors[2].id,
        summary=f"{vendors[2].legal_name}: vendor_code changed",
        detail={"before": {"vendor_code": "V-1002"},
                "after": {"vendor_code": "V-1002-B"}},
        actor_id=clerk.id,
        acknowledged_by=people["cfo"].id,
        acknowledged_at=_now() - timedelta(days=3),
        acknowledgement_note="Confirmed with procurement; the old code was a typo.",
        created_at=_now() - timedelta(days=4),
    )
    db.add(reviewed)
    db.flush()


def _documents(db, tenant, people, invoices, rng):
    """A scanned document behind most invoices, but not all.

    Not all, deliberately. Evidence completeness reading 100% proves nothing —
    the panel exists to surface the ones with nothing behind them, and a demo
    dataset where that can never happen cannot show it working.
    """
    clerk = people["ap_clerk"]
    attached = 0
    for invoice in invoices:
        if invoice.current_state == InvoiceState.DRAFT or rng.random() > 0.88:
            continue
        file_row = File(
            id=uuid.uuid4(), tenant_id=tenant.id,
            original_filename=f"{invoice.invoice_number}.pdf",
            stored_filename=f"{uuid.uuid4().hex}.pdf",
            file_path=f"uploads/{uuid.uuid4().hex}.pdf",
            mime_type="application/pdf",
            file_size=rng.randint(45_000, 380_000),
            file_hash=uuid.uuid4().hex * 2,
            object_type="invoice", object_id=invoice.id,
            storage_type="local", uploaded_by=clerk.id,
        )
        db.add(file_row)
        db.flush()
        invoice.pdf_file_id = file_row.id
        attached += 1
    db.flush()
    return attached


def _escalations(db, tenant, people, invoices):
    """The long-waiting ones would have escalated by now, so they have.

    Without these the evidence dashboard reports no late approvals against a
    dataset that plainly contains some, which would be the dashboard lying
    rather than the data being clean.
    """
    cfo = people["cfo"]
    now = _now()
    escalated = 0
    for invoice in invoices:
        if invoice.current_state != InvoiceState.PENDING_APPROVAL:
            continue
        entered = invoice.state_entered_at
        if entered is None or (now - entered) < timedelta(hours=48):
            continue
        _audit(db, tenant.id, cfo, "invoice", invoice.id, "sla_escalated",
               entered + timedelta(hours=48), workflow_type="invoice",
               workflow_step="pending_approval",
               comment="SLA of 48h in pending_approval breached; escalated to CFO.",
               after_value={"escalate_to": "cfo", "sla_hours": 48})
        escalated += 1
    return escalated


def _autopilot(db, tenant, people, invoices, rng):
    """A stretch where autopilot was on, with one decision reverted.

    The reversal is the point. Autopilot approving a lot is only good news
    while the reversal rate stays near zero, so a dataset with no reversals at
    all cannot demonstrate the number anybody should actually be watching.
    """
    admin, manager = people["admin"], people["manager"]
    candidates = [
        i for i in invoices
        if i.current_state in (InvoiceState.APPROVED, InvoiceState.PAID)
        and i.total_amount < 40_000
    ]
    chosen = candidates[:12]
    for index, invoice in enumerate(chosen):
        at = invoice.state_entered_at
        _audit(db, tenant.id, admin, "invoice", invoice.id, "autopilot_approved",
               at, workflow_type="invoice", workflow_step="approved",
               comment="Auto-approved: within bounds and every policy check passed.",
               after_value={"autopilot": True, "amount": str(invoice.total_amount)})
        db.add(AIActionLog(
            id=uuid.uuid4(), tenant_id=tenant.id, user_id=admin.id,
            action="autopilot_evaluate",
            status="completed" if index % 7 else "failed_schema",
            ai_provider="gemini", ai_model="gemini-2.5-flash",
            prompt_version="invoice_next_action-v1",
            confidence=round(rng.uniform(0.72, 0.97), 2),
            latency_ms=rng.randint(400, 2200),
            input_summary=f"invoice={invoice.invoice_number}",
            output_summary="approve",
            object_type="invoice", object_id=invoice.id,
            created_at=at,
        ))

    if chosen:
        reverted = chosen[0]
        _audit(db, tenant.id, manager, "invoice", reverted.id, "autopilot_reverted",
               reverted.state_entered_at + timedelta(hours=20),
               workflow_type="invoice",
               comment="Reverted on review: the delivery note did not match.")
    db.flush()
    return len(chosen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="Required. Writes ~90 days of demo data.")
    parser.add_argument("--tenant", default=None, help="Tenant slug (default: the first).")
    args = parser.parse_args()

    if not args.confirm:
        logger.error(
            "Refusing to run without --confirm. This writes a large demo "
            "dataset and is not something to do by accident."
        )
        return 2

    rng = random.Random(SEED)
    db = SessionLocal()
    try:
        tenant = (
            db.query(Tenant).filter(Tenant.slug == args.tenant).first()
            if args.tenant else db.query(Tenant).first()
        )
        if not tenant:
            logger.error("No tenant. Run scripts.bootstrap_tenant first.")
            return 1
        set_tenant_context(db, str(tenant.id))

        existing = db.query(Invoice).count()
        if existing:
            logger.error(
                "This tenant already has %s invoice(s). Refusing rather than "
                "doubling a dataset somebody may be using.", existing,
            )
            return 1

        start = _now() - timedelta(days=DAYS)
        people = _people(db, tenant)
        logger.info("people      : %s", ", ".join(sorted(people)))

        vendors = _vendors(db, tenant, people, start)
        logger.info("vendors     : %s", len(vendors))

        invoices = _invoices(db, tenant, people, vendors, rng, start)
        logger.info("invoices    : %s", len(invoices))

        _duplicates(db, tenant, people, invoices, rng)
        runs = _payments(db, tenant, people, invoices, rng, start)
        logger.info("payment runs: %s", len(runs))

        _bank(db, tenant, people, runs, rng, start)
        _bank_change_and_watchlist(db, tenant, people, vendors, rng)

        logger.info("documents   : %s", _documents(db, tenant, people, invoices, rng))
        logger.info("escalations : %s", _escalations(db, tenant, people, invoices))
        logger.info("autopilot   : %s", _autopilot(db, tenant, people, invoices, rng))

        db.commit()
        logger.info("audit events: %s", db.query(AuditLog).count())
        logger.info("\nSeeded. Sign in as any of clerk@ / manager@ / cfo@ / "
                    "auditor@demo.com with DemoLocal!2026")
        return 0
    except Exception:
        db.rollback()
        logger.exception("Seeding failed; nothing was written.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
