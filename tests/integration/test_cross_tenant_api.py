"""One tenant must not reach another tenant's records through the HTTP API.

`test_tenant_scoping.py` proves the ORM listener filters queries, which is the
mechanism. This proves the promise it exists to keep, at the edge a customer
actually touches — and it runs as an **administrator** of the other tenant,
deliberately: an admin holds every permission, so anything that gets through
does so because a tenant boundary is missing rather than a role check.

Written after probing a live two-tenant deployment endpoint by endpoint. That
probe found no data leak, but it did find `GET /purchase-orders/{id}/receipts`
answering 200 `[]` for another tenant's order where every sibling answers 404 —
isolation by accident of the receipts being scoped, rather than by checking the
order. The reads below are the shape that would have caught it as a test.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import (
    UserRole, InvoiceState, VendorStatus, PaymentState, PurchaseOrderState,
)
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.purchase_order import PurchaseOrder
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vendor import Vendor

pytestmark = pytest.mark.integration


@pytest.fixture
def outsider(db):
    """An admin of a different tenant, and one record of each kind they own.

    Their records carry "Outsider" in their own text, so a leak is visible in
    the response body rather than something to correlate by id.
    """
    tenant = Tenant(
        id=uuid.uuid4(), name="Outsider Corp",
        slug=f"outsider-{uuid.uuid4().hex[:8]}", isolation_level="rls",
    )
    db.add(tenant)
    db.flush()

    admin = User(
        id=uuid.uuid4(), tenant_id=tenant.id,
        email=f"admin-{uuid.uuid4().hex[:6]}@outsider.test", password="x",
        role=UserRole.ADMIN, is_active=True,
    )
    db.add(admin)
    db.flush()

    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Outsider Vendor",
        status=VendorStatus.ACTIVE, created_by=admin.id,
    )
    db.add(vendor)
    db.flush()

    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
        vendor_name=vendor.legal_name, invoice_number="OUTSIDER-INV-001",
        invoice_date=date(2026, 8, 1), total_amount=Decimal("50000"),
        current_state=InvoiceState.APPROVED, created_by=admin.id,
    )
    order = PurchaseOrder(
        id=uuid.uuid4(), tenant_id=tenant.id, po_number="OUTSIDER-PO-001",
        vendor_id=vendor.id, vendor_name=vendor.legal_name,
        order_date=date(2026, 8, 1), total_amount=Decimal("50000"),
        current_state=PurchaseOrderState.APPROVED, created_by=admin.id,
    )
    payment = Payment(
        id=uuid.uuid4(), tenant_id=tenant.id, payment_number="OUTSIDER-PAY-001",
        payment_date=date(2026, 8, 3), total_amount=Decimal("50000"),
        current_state=PaymentState.RELEASED, prepared_by=admin.id,
    )
    db.add_all([invoice, order, payment])
    db.flush()

    return {
        "tenant_id": str(tenant.id), "admin_id": str(admin.id),
        "vendor": vendor, "invoice": invoice, "order": order, "payment": payment,
        "marker": "Outsider",
    }


@pytest.fixture
def insider(make_user, as_user):
    """An administrator of the tenant under test, authenticated."""
    return as_user(make_user(UserRole.ADMIN))


class TestListsExcludeOtherTenants:

    @pytest.mark.parametrize("path", [
        "/api/v1/invoices/",
        "/api/v1/vendors/",
        "/api/v1/purchase-orders",
        "/api/v1/payments",
        "/api/v1/users",
        "/api/v1/inbox",
        "/api/v1/bank-statements",
        "/api/v1/delegations",
    ])
    def test_no_outsider_record_appears(self, client, insider, outsider, path):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert outsider["marker"] not in response.text, (
            f"{path} returned another tenant's data: {response.text[:300]}"
        )


class TestFetchByKnownIdIsRefused:
    """The dangerous case: the caller already has the UUID, so no listing is
    needed to exploit a gap."""

    def test_invoice(self, client, insider, outsider):
        response = client.get(f"/api/v1/invoices/{outsider['invoice'].id}")
        assert response.status_code == 404, response.text

    def test_vendor(self, client, insider, outsider):
        response = client.get(f"/api/v1/vendors/{outsider['vendor'].id}")
        assert response.status_code == 404, response.text

    def test_purchase_order(self, client, insider, outsider):
        response = client.get(f"/api/v1/purchase-orders/{outsider['order'].id}")
        assert response.status_code == 404, response.text

    def test_goods_receipts_for_another_tenants_order(self, client, insider, outsider):
        """Answered 200 [] before the order itself was checked — isolation by
        accident of the receipts being scoped, not by looking at the order."""
        response = client.get(
            f"/api/v1/purchase-orders/{outsider['order'].id}/receipts"
        )
        assert response.status_code == 404, response.text

    def test_payment(self, client, insider, outsider):
        response = client.get(f"/api/v1/payments/{outsider['payment'].id}")
        assert response.status_code == 404, response.text

    def test_bank_file_for_another_tenants_payment(self, client, insider, outsider):
        response = client.get(
            f"/api/v1/payments/{outsider['payment'].id}/bank-file"
        )
        assert response.status_code == 404, response.text

    def test_another_tenants_record_looks_like_one_that_does_not_exist(
        self, client, insider, outsider
    ):
        """No existence oracle: the two answers must be indistinguishable, or
        the API confirms which UUIDs are real for someone enumerating them."""
        real = client.get(f"/api/v1/invoices/{outsider['invoice'].id}")
        absent = client.get(f"/api/v1/invoices/{uuid.uuid4()}")
        assert real.status_code == absent.status_code
        assert real.json() == absent.json()


class TestWritesAgainstAnotherTenantAreRefused:
    """Worse than reading: approving someone else's invoice or releasing their
    payment is a control failure, not a confidentiality one."""

    def test_cannot_approve_their_invoice(self, client, insider, outsider):
        response = client.post(
            f"/api/v1/invoices/{outsider['invoice'].id}/approve", json={}
        )
        assert response.status_code >= 400, response.text

    def test_cannot_approve_their_purchase_order(self, client, insider, outsider):
        response = client.post(
            f"/api/v1/purchase-orders/{outsider['order'].id}/approve", json={}
        )
        assert response.status_code >= 400, response.text

    def test_cannot_release_their_payment(self, client, insider, outsider):
        response = client.post(
            f"/api/v1/payments/{outsider['payment'].id}/release", json={}
        )
        assert response.status_code >= 400, response.text

    def test_cannot_block_their_vendor(self, client, db, insider, outsider):
        response = client.patch(
            f"/api/v1/vendors/{outsider['vendor'].id}/status",
            json={"status": "blocked"},
        )
        assert response.status_code >= 400, response.text
        db.refresh(outsider["vendor"])
        status_value = getattr(
            outsider["vendor"].status, "value", outsider["vendor"].status
        )
        assert status_value == VendorStatus.ACTIVE.value

    def test_cannot_change_their_users_role(self, client, db, insider, outsider):
        response = client.patch(
            f"/api/v1/users/{outsider['admin_id']}/role", json={"role": "ap_clerk"}
        )
        assert response.status_code >= 400, response.text


class TestReferenceInjection:
    """Staying inside your own tenant but naming theirs in the body — the shape
    that slips past a check which only validates the object in the URL."""

    def test_cannot_pay_another_tenants_invoice(self, client, insider, outsider):
        response = client.post(
            "/api/v1/payments", json={"invoice_ids": [str(outsider["invoice"].id)]}
        )
        assert response.status_code >= 400, response.text

    def test_cannot_smuggle_one_into_a_valid_run(self, client, db, insider, outsider):
        """A mixed run is the nastier shape: one legitimate id carrying one
        that is not. It must fail whole, not settle the valid half."""
        from app.models.tenant import Tenant  # noqa: F401  (fixture ordering)

        own = Invoice(
            id=uuid.uuid4(), tenant_id=insider["tenant_id"],
            vendor_name="Own Vendor", invoice_number="OWN-INV-001",
            invoice_date=date(2026, 8, 1), total_amount=Decimal("100"),
            current_state=InvoiceState.APPROVED, created_by=insider["id"],
        )
        db.add(own)
        db.flush()

        response = client.post("/api/v1/payments", json={
            "invoice_ids": [str(own.id), str(outsider["invoice"].id)],
        })
        assert response.status_code >= 400, response.text

        from app.models.payment import Payment as PaymentModel
        assert db.query(PaymentModel).filter(
            PaymentModel.tenant_id == insider["tenant_id"]
        ).count() == 0, "a payment run was created despite the refusal"

    def test_cannot_delegate_authority_to_another_tenants_user(
        self, client, insider, outsider
    ):
        response = client.post("/api/v1/delegations", json={
            "to_user_id": outsider["admin_id"],
            "starts_at": "2026-08-12T00:00:00",
            "ends_at": "2026-08-20T00:00:00",
            "reason": "cross-tenant delegate",
        })
        assert response.status_code >= 400, response.text


class TestTheTokensTenantIsNotTrustedAlone:
    """A signed token cannot be edited by its holder, but its claims can
    disagree with each other if anything ever issues one carelessly. What must
    not happen is the tenant claim being honoured on its own.

    These run the *real* authentication dependency — overriding it would mean
    testing the scoping with the check it depends on switched off, which is how
    the first version of this test managed to fail against code that is fine.
    """

    @pytest.fixture
    def unauthenticated_client(self, db):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.core.database import get_db
        from app.api.deps import get_db_session

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_db_session] = lambda: db
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    @staticmethod
    def _token(user_id, tenant_id):
        from app.core.security import create_access_token

        return create_access_token({
            "sub": str(user_id), "tenant_id": str(tenant_id),
            "email": "probe@test.local", "role": "admin", "token_version": 0,
        })

    def test_a_token_naming_another_tenant_is_refused(
        self, unauthenticated_client, db, make_user, outsider
    ):
        """The user is real and the token is validly signed, but the tenant in
        it is not theirs. It must be rejected rather than honoured."""
        insider = make_user(UserRole.ADMIN)
        token = self._token(insider["id"], outsider["tenant_id"])

        response = unauthenticated_client.get(
            "/api/v1/invoices/", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401, response.text
        assert outsider["marker"] not in response.text

    def test_the_matching_token_works(
        self, unauthenticated_client, db, make_user, outsider
    ):
        """The control: same construction, correct tenant, and no sight of the
        other tenant's records."""
        insider = make_user(UserRole.ADMIN)
        token = self._token(insider["id"], insider["tenant_id"])

        response = unauthenticated_client.get(
            "/api/v1/invoices/", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text
        assert outsider["marker"] not in response.text
