"""Integration tests for Evidence Pack generation.

Build Book: a one-click audit-ready bundle with hashes, logs and policy
snapshots, including the hashes of every attachment referenced.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.vendor import Vendor
from app.models.file import File
from app.models.evidence_pack import EvidencePack
from app.schemas.invoice import InvoiceCreate
from app.services.invoice_service import InvoiceService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.evidence_pack import EvidencePackService
from app.services.ai_action_log import log_ai_action, STATUS_COMPLETED
from app.services.notification_service import NotificationService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


def _chain(db, tenant, user, with_file=False):
    """An invoice taken through create -> validate -> submit, optionally with
    an attachment, returning (invoice, correlation_id)."""
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant.id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=VendorStatus.ACTIVE, created_by=user["id"])
    db.add(v)
    db.flush()

    svc = InvoiceService(db)
    inv = svc.create_manual_invoice(
        InvoiceCreate(vendor_name="V", invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                      vendor_id=v.id, invoice_date=date(2026, 1, 1), total_amount=100_000),
        user,
    )
    if with_file:
        db.add(File(
            id=uuid.uuid4(), tenant_id=tenant.id, original_filename="invoice.pdf",
            stored_filename="stored.pdf", file_path="./uploads/stored.pdf",
            mime_type="application/pdf", file_size=1024,
            file_hash="a" * 64, object_type="invoice", object_id=inv.id,
            uploaded_by=user["id"],
        ))
        db.flush()

    svc.validate_invoice(inv.id, user)
    svc.submit_for_approval(inv.id, user)
    log_ai_action(db, tenant.id, user["id"], action="invoice_next_action",
                  status=STATUS_COMPLETED, ai_provider="claude", ai_model="claude-opus-4-8",
                  object_type="invoice", object_id=inv.id)
    db.flush()
    return inv, inv.correlation_id


class TestPackContents:
    def test_bundle_carries_every_evidence_type(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        inv, cid = _chain(db, tenant, admin, with_file=True)

        pack = EvidencePackService(db).build(cid, admin)
        c = pack["content"]

        assert c["objects"][0]["reference"] == inv.invoice_number
        assert len(c["audit_trail"]) >= 3          # created, validated, submitted
        assert len(c["policy_evaluations"]) >= 1
        assert len(c["ai_actions"]) == 1
        assert c["ai_actions"][0]["model"] == "claude-opus-4-8"
        assert pack["counts"]["objects"] == 1

    def test_attachments_carry_content_hashes(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, cid = _chain(db, tenant, admin, with_file=True)

        attachments = EvidencePackService(db).build(cid, admin)["content"]["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["sha256"] == "a" * 64
        assert attachments[0]["filename"] == "invoice.pdf"

    def test_pack_includes_chain_verification(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, cid = _chain(db, tenant, admin)

        pack = EvidencePackService(db).build(cid, admin)
        assert pack["all_chains_verified"] is True
        assert pack["content"]["integrity"][0]["verified"] is True


class TestPackSeal:
    def test_hash_is_stable_for_unchanged_data(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, cid = _chain(db, tenant, admin)
        svc = EvidencePackService(db)
        assert svc.build(cid, admin)["pack_hash"] == svc.build(cid, admin)["pack_hash"]

    def test_hash_changes_when_the_chain_changes(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        inv, cid = _chain(db, tenant, admin)
        svc = EvidencePackService(db)
        before = svc.build(cid, admin)["pack_hash"]

        InvoiceService(db).approve_invoice(inv.id, admin)   # more history
        assert svc.build(cid, admin)["pack_hash"] != before

    def test_distinct_chains_have_distinct_hashes(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, a = _chain(db, tenant, admin)
        _, b = _chain(db, tenant, admin)
        svc = EvidencePackService(db)
        assert svc.build(a, admin)["pack_hash"] != svc.build(b, admin)["pack_hash"]


class TestGenerationRecord:
    def test_generate_persists_a_record_with_the_seal(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, cid = _chain(db, tenant, admin, with_file=True)

        pack = EvidencePackService(db).generate(cid, admin)
        row = db.query(EvidencePack).filter(EvidencePack.id == pack["pack_id"]).first()

        assert row is not None
        assert row.pack_hash == pack["pack_hash"]
        assert row.correlation_id == cid
        assert row.generated_by is not None
        assert row.manifest["counts"]["attachments"] == 1
        assert row.manifest["attachment_hashes"][0]["sha256"] == "a" * 64

    def test_packs_are_listable_and_filterable(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _, a = _chain(db, tenant, admin)
        _, b = _chain(db, tenant, admin)
        svc = EvidencePackService(db)
        svc.generate(a, admin)
        svc.generate(b, admin)

        assert len(svc.list_packs(admin)) == 2
        assert len(svc.list_packs(admin, correlation_id=a)) == 1


class TestAccess:
    def test_empty_chain_produces_an_empty_but_valid_pack(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        pack = EvidencePackService(db).build(uuid.uuid4(), admin)
        assert pack["counts"]["objects"] == 0
        assert pack["all_chains_verified"] is True     # vacuously
        assert pack["pack_hash"]

    def test_requires_audit_permission(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            EvidencePackService(db).build(uuid.uuid4(), clerk)
