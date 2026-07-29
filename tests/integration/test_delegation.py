"""Integration tests for delegation of approval authority.

Build Book: temporary assignment of approvals with start and end dates. The
security-critical properties: delegation is time-bounded, revocable, never
widens SoD, and the trail names both the actor and the authority they used.
"""
import uuid
from datetime import date, timedelta

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.vendor import Vendor
from app.models.audit_log import AuditLog
from app.schemas.invoice import InvoiceCreate
from app.services.invoice_service import InvoiceService
from app.services.config_provisioning import ConfigProvisioningService
from app.services.delegation import DelegationService, resolve_authority
from app.services.notification_service import NotificationService
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


def _window(offset_hours=-1, length_hours=48):
    start = utc_now() + timedelta(hours=offset_hours)
    return start, start + timedelta(hours=length_hours)


def _pending_invoice(db, tenant, creator, amount=100_000):
    """An invoice submitted and awaiting approval (routes to manager at 100k)."""
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant.id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=VendorStatus.ACTIVE, created_by=creator["id"])
    db.add(v)
    db.flush()
    svc = InvoiceService(db)
    inv = svc.create_manual_invoice(
        InvoiceCreate(vendor_name="V", invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                      vendor_id=v.id, invoice_date=date(2026, 1, 1), total_amount=amount),
        creator,
    )
    svc.validate_invoice(inv.id, creator)
    svc.submit_for_approval(inv.id, creator)
    return inv


class TestLifecycle:
    def test_create_and_list(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        clerk = make_user(UserRole.AP_CLERK)
        start, end = _window()
        d = DelegationService(db).create(manager, to_user_id=clerk["id"],
                                         starts_at=start, ends_at=end, reason="Annual leave")
        assert d.from_user_id == uuid.UUID(manager["id"])
        assert d.to_user_id == uuid.UUID(clerk["id"])
        assert DelegationService(db).list_for_user(clerk)[0].id == d.id

    def test_cannot_delegate_to_self(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        start, end = _window()
        with pytest.raises(ValueError):
            DelegationService(db).create(manager, to_user_id=manager["id"],
                                         starts_at=start, ends_at=end)

    def test_end_must_follow_start(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        now = utc_now()
        with pytest.raises(ValueError):
            DelegationService(db).create(manager, to_user_id=clerk["id"],
                                         starts_at=now, ends_at=now - timedelta(hours=1))

    def test_cannot_delegate_someone_elses_authority(self, db, tenant, make_user):
        manager, cfo, clerk = (make_user(UserRole.MANAGER), make_user(UserRole.CFO),
                               make_user(UserRole.AP_CLERK))
        start, end = _window()
        with pytest.raises(PermissionError):
            DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start,
                                         ends_at=end, from_user_id=cfo["id"])

    def test_admin_may_delegate_on_behalf_of_others(self, db, tenant, make_user):
        admin, cfo, clerk = (make_user(UserRole.ADMIN), make_user(UserRole.CFO),
                             make_user(UserRole.AP_CLERK))
        start, end = _window()
        d = DelegationService(db).create(admin, to_user_id=clerk["id"], starts_at=start,
                                         ends_at=end, from_user_id=cfo["id"])
        assert d.from_user_id == uuid.UUID(cfo["id"])

    def test_overlapping_delegation_rejected(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        start, end = _window()
        svc = DelegationService(db)
        svc.create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        with pytest.raises(ValueError):
            svc.create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)


class TestAuthorityWindow:
    def test_active_delegation_confers_the_role(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        start, end = _window()
        DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        allowed, used = resolve_authority(db, clerk, "manager")
        assert allowed and used is not None

    def test_future_delegation_confers_nothing_yet(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        start, end = _window(offset_hours=48)
        DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        assert resolve_authority(db, clerk, "manager")[0] is False

    def test_expired_delegation_confers_nothing(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        start, end = _window(offset_hours=-72, length_hours=24)
        DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        assert resolve_authority(db, clerk, "manager")[0] is False

    def test_revoked_delegation_confers_nothing(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        start, end = _window()
        svc = DelegationService(db)
        d = svc.create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        svc.revoke(d.id, manager)
        assert resolve_authority(db, clerk, "manager")[0] is False

    def test_only_the_delegator_may_revoke(self, db, tenant, make_user):
        manager, clerk, other = (make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK),
                                 make_user(UserRole.APPROVER))
        start, end = _window()
        svc = DelegationService(db)
        d = svc.create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)
        with pytest.raises(PermissionError):
            svc.revoke(d.id, other)


class TestApprovalUnderDelegation:
    def test_delegate_can_approve_and_the_trail_names_both(self, db, tenant, make_user):
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        inv = _pending_invoice(db, tenant, admin)   # a third party is the maker

        start, end = _window()
        DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start,
                                     ends_at=end, reason="Leave cover")

        # The clerk has neither the manager role nor invoices.approve of their own.
        InvoiceService(db).approve_invoice(inv.id, clerk)

        approved = db.query(AuditLog).filter(
            AuditLog.object_id == inv.id, AuditLog.action == "approved"
        ).first()
        assert approved.user_email == clerk["email"]                     # who acted
        assert approved.after_value["delegated_authority_of"] == str(manager["id"])  # whose authority

    def test_without_delegation_the_clerk_is_refused(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        inv = _pending_invoice(db, tenant, admin)
        with pytest.raises(PermissionError):
            InvoiceService(db).approve_invoice(inv.id, clerk)

    def test_delegation_never_defeats_segregation_of_duties(self, db, tenant, make_user):
        """The whole point: borrowed authority must not make you your own checker."""
        manager, clerk = make_user(UserRole.MANAGER), make_user(UserRole.AP_CLERK)
        ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
        inv = _pending_invoice(db, tenant, clerk)     # the CLERK created it

        start, end = _window()
        DelegationService(db).create(manager, to_user_id=clerk["id"], starts_at=start, ends_at=end)

        with pytest.raises(PermissionError, match="[Ss]egregation"):
            InvoiceService(db).approve_invoice(inv.id, clerk)
