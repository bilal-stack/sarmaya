"""Integration tests for universal correlation IDs.

Build Book: every transaction chain carries a correlation_id linking every
record across modules, and search must reconstruct the whole story from it.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.vendor import Vendor
from app.models.audit_log import AuditLog
from app.models.policy_eval import PolicyEval
from app.models.ai_action_log import AIActionLog
from app.schemas.invoice import InvoiceCreate
from app.services.invoice_service import InvoiceService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.correlation import CorrelationService, resolve_correlation_id
from app.services.ai_action_log import log_ai_action, STATUS_COMPLETED
from app.services.notification_service import NotificationService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


def _vendor(db, tenant_id, user_id):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=VendorStatus.ACTIVE, created_by=user_id)
    db.add(v)
    db.flush()
    return v


class TestChainCreation:
    def test_manual_invoice_starts_a_chain(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, admin["id"])
        inv = InvoiceService(db).create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C1", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=50_000),
            admin,
        )
        assert inv.correlation_id is not None

    def test_two_invoices_get_distinct_chains(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, admin["id"])
        svc = InvoiceService(db)
        a = svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C2", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=10_000), admin)
        b = svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C3", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=20_000), admin)
        assert a.correlation_id != b.correlation_id


class TestPropagation:
    def test_audit_policy_and_ai_records_inherit_the_chain(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        vendor = _vendor(db, tenant.id, admin["id"])
        svc = InvoiceService(db)
        inv = svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C4", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=100_000),
            admin,
        )
        cid = inv.correlation_id

        svc.validate_invoice(inv.id, admin)
        svc.submit_for_approval(inv.id, admin)
        log_ai_action(db, tenant.id, admin["id"], action="invoice_next_action",
                      status=STATUS_COMPLETED, object_type="invoice", object_id=inv.id)
        db.flush()

        # Audit events written through log_audit inherit the invoice's chain.
        audits = db.query(AuditLog).filter(AuditLog.object_id == inv.id).all()
        assert audits and all(a.correlation_id == cid for a in audits)

        # So do policy evaluations and AI actions.
        evals = db.query(PolicyEval).filter(PolicyEval.object_id == inv.id).all()
        assert evals and all(e.correlation_id == cid for e in evals)

        ai = db.query(AIActionLog).filter(AIActionLog.object_id == inv.id).all()
        assert ai and all(l.correlation_id == cid for l in ai)

    def test_resolve_returns_none_for_unknown_object(self, db):
        assert resolve_correlation_id(db, "invoice", uuid.uuid4()) is None
        assert resolve_correlation_id(db, "vendor", uuid.uuid4()) is None
        assert resolve_correlation_id(db, None, None) is None


class TestChainReconstruction:
    def test_chain_merges_every_record_type_in_time_order(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        vendor = _vendor(db, tenant.id, admin["id"])
        svc = InvoiceService(db)
        inv = svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C5", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=100_000),
            admin,
        )
        svc.validate_invoice(inv.id, admin)
        svc.submit_for_approval(inv.id, admin)
        log_ai_action(db, tenant.id, admin["id"], action="invoice_next_action",
                      status=STATUS_COMPLETED, object_type="invoice", object_id=inv.id)
        db.flush()

        chain = CorrelationService(db).get_chain(inv.correlation_id, admin)

        assert chain["objects"][0]["reference"] == "INV-C5"
        assert chain["counts"]["audit_events"] >= 3        # created, validated, submitted
        assert chain["counts"]["policy_evaluations"] >= 1
        assert chain["counts"]["ai_actions"] == 1
        assert chain["total_events"] == sum(chain["counts"].values())
        kinds = {e["kind"] for e in chain["events"]}
        assert kinds == {"audit", "policy_eval", "ai_action"}

    def test_chains_do_not_bleed_into_each_other(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, admin["id"])
        svc = InvoiceService(db)
        a = svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C6", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=10_000), admin)
        svc.create_manual_invoice(
            InvoiceCreate(vendor_name="V", invoice_number="INV-C7", vendor_id=vendor.id,
                          invoice_date=date(2026, 1, 1), total_amount=20_000), admin)

        chain = CorrelationService(db).get_chain(a.correlation_id, admin)
        assert [o["reference"] for o in chain["objects"]] == ["INV-C6"]

    def test_unknown_chain_is_empty_not_an_error(self, db, make_user):
        admin = make_user(UserRole.ADMIN)
        chain = CorrelationService(db).get_chain(uuid.uuid4(), admin)
        assert chain["objects"] == [] and chain["total_events"] == 0

    def test_requires_audit_or_invoice_view_permission(self, db, make_user):
        system = make_user(UserRole.SYSTEM)  # holds invoices.view
        assert CorrelationService(db).get_chain(uuid.uuid4(), system)["total_events"] == 0
