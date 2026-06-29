"""Integration test for SoD enforcement at the service layer.

The vendor-activation rule bites in practice: AP clerks and managers both hold
vendors.manage, so the maker-checker control must stop the creator from also
activating the vendor.
"""
import pytest

from app.core.enums import UserRole, VendorStatus
from app.schemas.vendor import VendorCreate
from app.services.vendor_service import VendorService

pytestmark = pytest.mark.integration


class TestVendorActivationSoD:
    def test_creator_cannot_activate_own_vendor(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        svc = VendorService(db)
        vendor = svc.create_vendor(
            VendorCreate(legal_name="Acme", status=VendorStatus.PENDING_VERIFICATION), clerk
        )
        with pytest.raises(PermissionError):
            svc.set_status(vendor.id, VendorStatus.ACTIVE, clerk)

    def test_a_different_user_can_activate(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        svc = VendorService(db)
        vendor = svc.create_vendor(
            VendorCreate(legal_name="Beta", status=VendorStatus.PENDING_VERIFICATION), clerk
        )
        activated = svc.set_status(vendor.id, VendorStatus.ACTIVE, manager)
        assert activated.status == VendorStatus.ACTIVE

    def test_admin_can_activate_own_vendor(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        svc = VendorService(db)
        vendor = svc.create_vendor(
            VendorCreate(legal_name="Gamma", status=VendorStatus.PENDING_VERIFICATION), admin
        )
        activated = svc.set_status(vendor.id, VendorStatus.ACTIVE, admin)
        assert activated.status == VendorStatus.ACTIVE
