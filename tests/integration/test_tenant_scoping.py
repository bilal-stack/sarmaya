"""Every ORM read is confined to the session's bound tenant.

Tenant isolation was left entirely to Postgres RLS. Those policies are created
by migration 003, so they do not exist in a database built with create_all —
which is every developer and test database in this project. The gap was
confirmed against a running dev server: `GET /users` listed another tenant's
staff, and a demo-tenant admin applied a role change to a user belonging to a
different tenant.

Around 39 functions across the services and repositories query a tenant-owned
table without naming tenant_id. Filtering each one leaves the next query
written to miss it silently, so the restriction is applied once, in a
do_orm_execute listener in app.core.database, to every ORM SELECT on a session
that has a tenant bound.

These tests exercise that listener directly, because the failure they guard
against is silent: a query that returns another tenant's rows looks exactly
like a query that works.
"""
import uuid

import pytest

from app.core.database import set_tenant_context
from app.core.enums import UserRole, InvoiceState, VendorStatus
from app.models.invoice import Invoice
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import Vendor

pytestmark = pytest.mark.integration


@pytest.fixture
def two_tenants(db):
    """Two tenants, each with a vendor, an invoice and a user."""
    made = {}
    for label in ("alpha", "beta"):
        tenant = Tenant(
            id=uuid.uuid4(), name=f"{label.title()} Co",
            slug=f"{label}-{uuid.uuid4().hex[:8]}", isolation_level="rls",
        )
        db.add(tenant)
        db.flush()

        user = User(
            id=uuid.uuid4(), tenant_id=tenant.id,
            email=f"{label}-{uuid.uuid4().hex[:6]}@test.com",
            password="x", role=UserRole.ADMIN, is_active=True,
        )
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id,
            legal_name=f"{label.title()} Vendor", status=VendorStatus.ACTIVE,
        )
        db.add_all([user, vendor])
        db.flush()

        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id,
            vendor_id=vendor.id, vendor_name=vendor.legal_name,
            invoice_number=f"{label.upper()}-001",
            invoice_date="2026-07-01", total_amount=1000,
            current_state=InvoiceState.DRAFT, created_by=user.id,
        )
        db.add(invoice)
        db.flush()

        made[label] = {"tenant": tenant, "user": user,
                       "vendor": vendor, "invoice": invoice}
    return made


class TestBoundTenantSeesOnlyItsOwnRows:

    def test_invoices_are_scoped(self, db, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        set_tenant_context(db, str(alpha["tenant"].id))

        numbers = [i.invoice_number for i in db.query(Invoice).all()]

        assert alpha["invoice"].invoice_number in numbers
        assert beta["invoice"].invoice_number not in numbers

    def test_vendors_are_scoped(self, db, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        set_tenant_context(db, str(alpha["tenant"].id))

        names = [v.legal_name for v in db.query(Vendor).all()]

        assert alpha["vendor"].legal_name in names
        assert beta["vendor"].legal_name not in names

    def test_users_are_scoped(self, db, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        set_tenant_context(db, str(alpha["tenant"].id))

        emails = [u.email for u in db.query(User).all()]

        assert alpha["user"].email in emails
        assert beta["user"].email not in emails

    def test_fetching_another_tenants_row_by_id_returns_nothing(self, db, two_tenants):
        """The dangerous case: the caller already knows the UUID. This is the
        shape of the cross-tenant role change that was reproduced live."""
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]
        set_tenant_context(db, str(alpha["tenant"].id))

        found = db.query(User).filter(User.id == beta["user"].id).first()

        assert found is None


class TestScopeFollowsTheBoundTenant:
    """The listener must read the tenant per execution.

    with_loader_criteria caches lambdas by code location, so a closed-over
    tenant would bake the first request's tenant into every later one — every
    tenant served another's data. This is the test that catches that, and it
    is why the listener uses the eager expression form.
    """

    def test_rebinding_the_session_changes_what_is_visible(self, db, two_tenants):
        alpha, beta = two_tenants["alpha"], two_tenants["beta"]

        set_tenant_context(db, str(alpha["tenant"].id))
        first = [i.invoice_number for i in db.query(Invoice).all()]

        set_tenant_context(db, str(beta["tenant"].id))
        second = [i.invoice_number for i in db.query(Invoice).all()]

        assert first == [alpha["invoice"].invoice_number]
        assert second == [beta["invoice"].invoice_number], (
            "the second tenant was served the first tenant's rows — the "
            "tenant was cached into the query instead of read per execution"
        )

    def test_an_unbound_session_is_not_filtered(self, db, two_tenants):
        """Provisioning, migrations and multi-tenant fixtures run without a
        bound tenant and must keep working."""
        count = db.query(Invoice).count()
        assert count >= 2
