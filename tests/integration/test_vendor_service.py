"""Integration tests for VendorService (vendor master governance).

Requires a live test Postgres (see conftest). Exercises the real service +
repository against the database, including permission gates and delete safety.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.services.vendor_service import VendorService
from app.schemas.vendor import VendorCreate, VendorUpdate
from app.models.invoice import Invoice
from app.core.enums import UserRole, VendorStatus, InvoiceState

pytestmark = pytest.mark.integration


def _vendor_payload(**overrides) -> VendorCreate:
    data = {
        "legal_name": f"Acme {uuid.uuid4().hex[:6]} Ltd",
        "email": "ap@acme.com",
    }
    data.update(overrides)
    return VendorCreate(**data)


def _make_invoice_for_vendor(db, tenant_id, created_by, vendor):
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_id=vendor.id,
        vendor_name=vendor.legal_name,
        invoice_date=date.today(),
        total_amount=Decimal("1000"),
        current_state=InvoiceState.DRAFT.value,
        created_by=created_by,
    )
    db.add(inv)
    db.flush()
    return inv


class TestCreate:
    def test_admin_creates_vendor(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)
        assert vendor.id is not None
        assert vendor.status == VendorStatus.ACTIVE
        assert str(vendor.tenant_id) == admin["tenant_id"]

    def test_auditor_cannot_create(self, db, tenant, make_user):
        # AP_CLERK and MANAGER now hold vendors.manage; AUDITOR has view-only,
        # so it is the role that should still be blocked from creating.
        auditor = make_user(UserRole.AUDITOR)
        with pytest.raises(PermissionError):
            VendorService(db).create_vendor(_vendor_payload(), auditor)

    def test_ap_clerk_can_create(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        vendor = VendorService(db).create_vendor(_vendor_payload(), clerk)
        assert vendor.id is not None

    def test_duplicate_legal_name_rejected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        payload = _vendor_payload(legal_name="Globex Limited")
        VendorService(db).create_vendor(payload, admin)
        with pytest.raises(ValueError):
            VendorService(db).create_vendor(
                _vendor_payload(legal_name="globex limited"), admin
            )


class TestRead:
    def test_ap_clerk_can_view(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        clerk = make_user(UserRole.AP_CLERK)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)

        fetched = VendorService(db).get_vendor(vendor.id, clerk)
        assert fetched.id == vendor.id

    def test_approver_cannot_view(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        approver = make_user(UserRole.APPROVER)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)

        with pytest.raises(PermissionError):
            VendorService(db).get_vendor(vendor.id, approver)

    def test_get_missing_vendor_raises(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            VendorService(db).get_vendor(uuid.uuid4(), admin)


class TestUpdate:
    def test_admin_updates_display_name(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)

        updated = VendorService(db).update_vendor(
            vendor.id, VendorUpdate(display_name="Acme Trading"), admin
        )
        assert updated.display_name == "Acme Trading"

    def test_update_to_existing_name_rejected(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = VendorService(db)
        svc.create_vendor(_vendor_payload(legal_name="Initech LLC"), admin)
        other = svc.create_vendor(_vendor_payload(legal_name="Umbrella Corp"), admin)

        with pytest.raises(ValueError):
            svc.update_vendor(other.id, VendorUpdate(legal_name="Initech LLC"), admin)


class TestStatus:
    def test_admin_blocks_vendor(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)

        updated = VendorService(db).set_status(vendor.id, VendorStatus.BLOCKED, admin)
        assert updated.status == VendorStatus.BLOCKED


class TestDelete:
    def test_admin_deletes_unreferenced_vendor(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = VendorService(db)
        vendor = svc.create_vendor(_vendor_payload(), admin)

        svc.delete_vendor(vendor.id, admin)
        with pytest.raises(ValueError):
            svc.get_vendor(vendor.id, admin)

    def test_cannot_delete_vendor_with_invoices(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = VendorService(db)
        vendor = svc.create_vendor(_vendor_payload(), admin)
        _make_invoice_for_vendor(db, tenant.id, admin["id"], vendor)

        with pytest.raises(ValueError):
            svc.delete_vendor(vendor.id, admin)

    def test_auditor_cannot_delete(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        auditor = make_user(UserRole.AUDITOR)
        vendor = VendorService(db).create_vendor(_vendor_payload(), admin)

        with pytest.raises(PermissionError):
            VendorService(db).delete_vendor(vendor.id, auditor)
