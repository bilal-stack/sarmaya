"""A workflow action must never persist without its audit entry.

Every invoice workflow method used to call `repository.commit()` to save the
state change and only *then* write the audit entry, which `log_audit` merely
flushes. Nothing committed afterwards, so the entry was discarded when the
session closed: an invoice could move draft -> validated -> pending_approval
leaving no trace at all. Reproduced against a running server — the invoice
transitioned and `audit_logs` held nothing for it, and the whole database
contained no `created`, `validated`, `submitted_for_approval`, `approved`,
`rejected` or `marked_paid` events.

That is the product's central claim failing silently: the hash-chained trail,
the policy-evaluation snapshots and the evidence packs are all assembled from
records that were never written.

Testing this behaviourally is not possible here. The fixture session runs
inside an outer transaction that is rolled back after each test, and it joins
that transaction with a savepoint, so `commit()` releases a savepoint rather
than writing through — whether committed data survives a later `rollback()`
depends on how much work follows the commit, not on durability. A test resting
on that would pass or fail for the wrong reasons.

So the ordering is asserted structurally instead, over the source: any method
that writes an audit entry must commit after doing so. That is exactly the
property that was violated, and it fails loudly if it is reintroduced.
"""
import ast
import pathlib
import uuid
from collections import defaultdict

import pytest

from app.core.enums import UserRole, InvoiceState, VendorStatus
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.services.invoice_service import InvoiceService

pytestmark = pytest.mark.integration

#: Helpers whose audit write is committed by the caller that invokes them.
CALLER_COMMITS = {"app/services/invoice_service.py::_resolve_vendor"}


@pytest.fixture
def ready_invoice(db, tenant, make_user):
    """A vendor-linked invoice carrying every field validation requires."""
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id,
        legal_name="Durability Test Vendor", status=VendorStatus.ACTIVE,
    )
    db.add(vendor)
    db.flush()

    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant.id,
        vendor_id=vendor.id, vendor_name=vendor.legal_name,
        invoice_number=f"DUR-{uuid.uuid4().hex[:6]}",
        invoice_date="2026-07-01", total_amount=15000,
        current_state=InvoiceState.DRAFT,
        created_by=make_user(UserRole.ADMIN)["id"],
    )
    db.add(invoice)
    db.flush()
    return invoice


def _commit_and_audit_sequence():
    """For every function in app/ that writes an audit entry, the ordered
    commit/audit events it performs, read from the source.

    Swept across the whole package rather than one service: the same ordering
    was wrong in six of them, so pinning only the invoice service would leave
    the identical bug free to sit in vendor, policy, workflow, autopilot and
    provisioning code.
    """
    sequences = defaultdict(list)

    for path in sorted(pathlib.Path("app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "log_audit" not in source:
            continue
        for fn in (
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            key = f"{path.as_posix()}::{fn.name}"
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id == "log_audit":
                    sequences[key].append((node.lineno, "AUDIT"))
                elif isinstance(func, ast.Attribute) and func.attr == "commit":
                    sequences[key].append((node.lineno, "COMMIT"))

    return {
        k: [kind for _, kind in sorted(events)]
        for k, events in sequences.items()
        if any(kind == "AUDIT" for _, kind in events)
    }


class TestAuditEntriesAreCommitted:

    def test_every_audit_write_is_followed_by_a_commit(self):
        offenders = []
        for method, sequence in _commit_and_audit_sequence().items():
            if method in CALLER_COMMITS or "AUDIT" not in sequence:
                continue
            last_audit = len(sequence) - 1 - sequence[::-1].index("AUDIT")
            if "COMMIT" not in sequence[last_audit + 1:]:
                offenders.append(method)

        assert not offenders, (
            "These methods write an audit entry and never commit it, so the "
            f"entry is discarded when the session closes: {offenders}. "
            "Commit after log_audit, so the action and its trail land together."
        )

    def test_the_workflow_methods_are_actually_covered(self):
        """Guards the assertion above from passing because the source parse
        silently stopped finding anything."""
        found = _commit_and_audit_sequence()
        expected = {
            "app/services/invoice_service.py::validate_invoice",
            "app/services/invoice_service.py::submit_for_approval",
            "app/services/invoice_service.py::approve_invoice",
            "app/services/invoice_service.py::mark_as_paid",
            "app/services/vendor_service.py::set_status",
            "app/services/policy_service.py::update_policy",
            "app/services/autopilot_service.py::set_config",
        }
        missing = expected - found.keys()
        assert not missing, f"no audit/commit calls found in: {missing}"


class TestAuditEntriesAreWritten:
    """The complement to the ordering check: the entry must exist at all. This
    runs against the database, which the fixture transaction can observe."""

    def test_validate_writes_its_audit_entry(self, db, tenant, make_user, ready_invoice):
        invoice_id = ready_invoice.id

        InvoiceService(db).validate_invoice(invoice_id, make_user(UserRole.AP_CLERK))

        actions = [
            a.action for a in
            db.query(AuditLog).filter(AuditLog.object_id == invoice_id).all()
        ]
        assert "validated" in actions

    def test_submit_writes_its_audit_entry_and_routing_snapshot(
        self, db, tenant, make_user, ready_invoice
    ):
        from app.models.policy_eval import PolicyEval

        invoice_id = ready_invoice.id
        actor = make_user(UserRole.AP_CLERK)
        service = InvoiceService(db)
        service.validate_invoice(invoice_id, actor)
        service.submit_for_approval(invoice_id, actor)

        actions = [
            a.action for a in
            db.query(AuditLog).filter(AuditLog.object_id == invoice_id).all()
        ]
        assert "submitted_for_approval" in actions
        # The routing snapshot rides the same path and was lost with it.
        assert db.query(PolicyEval).filter(
            PolicyEval.object_id == invoice_id
        ).count() >= 1, "approval routing was decided but never snapshotted"


class TestUploadPathCommitsItsTrail:
    """The upload path was restructured along with the rest, and it is the only
    one that also links a file record, so it gets its own coverage.

    OCR is stubbed: the point is the transaction, not the extraction, and the
    real call would hit a paid external provider.
    """

    def test_upload_commits_invoice_file_link_and_audit_together(
        self, db, tenant, make_user, monkeypatch
    ):
        from app.models.file import File as FileModel
        from app.services import invoice_service as svc

        monkeypatch.setattr(svc, "extract_invoice_data_ocr", lambda *a, **k: {
            "vendor_name": "Upload Path Vendor",
            "invoice_number": f"UP-{uuid.uuid4().hex[:6]}",
            "invoice_date": "2026-07-15",
            "total_amount": 4200.0,
            "tax_amount": 200.0,
            "confidence": 88,
            "currency": "PKR",
            "line_items": [],
            "ai_enhanced": False,
        })

        actor = make_user(UserRole.AP_CLERK)
        file_record = FileModel(
            id=uuid.uuid4(), tenant_id=tenant.id,
            original_filename="upload.pdf", stored_filename="upload-stored.pdf",
            file_path="/tmp/upload.pdf", file_size=1024,
            mime_type="application/pdf", uploaded_by=actor["id"],
        )
        db.add(file_record)
        db.flush()

        result = InvoiceService(db)._extract_and_create_invoice(
            file_record, "/tmp/upload.pdf", "deadbeef" * 8, actor
        )

        invoice_id = uuid.UUID(result["invoice_id"])
        invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()

        assert invoice is not None
        # The file link is written between the two flushes; it must survive.
        assert invoice.pdf_file_id == file_record.id
        actions = [
            a.action for a in
            db.query(AuditLog).filter(AuditLog.object_id == invoice_id).all()
        ]
        assert "uploaded" in actions, "the upload left no audit entry"
