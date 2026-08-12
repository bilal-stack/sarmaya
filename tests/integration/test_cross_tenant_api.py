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


class TestProvisioningNamesItsTenant:
    """Seeding a second tenant must not think the first tenant's config is its
    own.

    The "already configured?" checks named no tenant and relied on the session's
    bound tenant. Provisioning runs where nothing is bound — a setup script, an
    onboarding job — and there the check saw the previous tenant's rows and
    seeded nothing. The second tenant came up with no workflow states and no
    approval matrix, every routing decision silently falling back to the
    hardcoded defaults. Found provisioning two tenants in one script.
    """

    def test_a_second_tenant_gets_its_own_config(self, db, make_user):
        from app.models.policy import Policy
        from app.models.workflow_state import WorkflowState
        from app.services.config_provisioning import ConfigProvisioningService

        first = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(first)

        second_tenant = Tenant(
            id=uuid.uuid4(), name="Second Corp",
            slug=f"second-{uuid.uuid4().hex[:8]}", isolation_level="rls",
        )
        db.add(second_tenant)
        db.flush()
        second_admin = User(
            id=uuid.uuid4(), tenant_id=second_tenant.id,
            email=f"admin-{uuid.uuid4().hex[:6]}@second.test", password="x",
            role=UserRole.ADMIN, is_active=True,
        )
        db.add(second_admin)
        db.flush()

        created = ConfigProvisioningService(db).initialize_defaults({
            "id": str(second_admin.id), "tenant_id": str(second_tenant.id),
            "email": second_admin.email, "role": UserRole.ADMIN.value,
        })

        assert created["created_states"] > 0, (
            "the second tenant was left with no workflow states"
        )
        assert created["created_policies"] > 0, (
            "the second tenant was left with no approval matrix"
        )
        assert db.query(WorkflowState).filter(
            WorkflowState.tenant_id == second_tenant.id
        ).count() > 0
        assert db.query(Policy).filter(
            Policy.tenant_id == second_tenant.id
        ).count() > 0

    def test_reprovisioning_the_same_tenant_still_does_nothing(self, db, make_user):
        """The idempotence the tenant check must not break."""
        from app.services.config_provisioning import ConfigProvisioningService

        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        again = ConfigProvisioningService(db).initialize_defaults(admin)

        assert again == {"created_states": 0, "created_policies": 0}


class TestEvidencePacksAreNotSealedOverNothing:
    """A pack generated for another tenant's correlation id contained zero
    objects — no leak, since the caller cannot see those records — but it was
    still hashed, recorded and stamped `all_chains_verified: true`.

    A sealed evidence pack exists to be pointed at later. One certifying an
    absence is worse than an error.
    """

    def test_generating_over_an_invisible_chain_is_refused(
        self, client, insider, outsider
    ):
        from app.models.payment import Payment

        their_chain = uuid.uuid4()
        response = client.post(f"/api/v1/audit/evidence-pack/{their_chain}")
        assert response.status_code >= 400, response.text
        assert "nothing to evidence" in response.text.lower()

    def test_no_pack_row_is_recorded(self, client, db, insider, outsider):
        from app.models.evidence_pack import EvidencePack

        before = db.query(EvidencePack).count()
        client.post(f"/api/v1/audit/evidence-pack/{uuid.uuid4()}")
        assert db.query(EvidencePack).count() == before

    def test_a_real_chain_still_seals(self, client, db, insider):
        """The control: a correlation id with records behind it works."""
        from app.models.invoice import Invoice

        correlation_id = uuid.uuid4()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=insider["tenant_id"],
            vendor_name="Own Vendor", invoice_number="EVID-INV-001",
            invoice_date=date(2026, 8, 1), total_amount=Decimal("100"),
            current_state=InvoiceState.APPROVED, created_by=insider["id"],
            correlation_id=correlation_id,
        )
        db.add(invoice)
        db.flush()

        response = client.post(f"/api/v1/audit/evidence-pack/{correlation_id}")
        assert response.status_code == 200, response.text
        assert response.json()["counts"]["objects"] >= 1


class TestAutopilotActsOnlyOnItsOwnTenant:
    """The write with the widest blast radius in the system.

    Autopilot approves every eligible invoice it finds in one call, so a
    missing tenant boundary here does not leak one record — it approves another
    company's payables. The scan relies on the session's bound tenant rather
    than naming it, which is the same shape as the provisioning bug above, so
    it is worth pinning down rather than reasoning about.

    This went untested for a while because the endpoint refuses to run unless
    autopilot is enabled, and probes that never got past that refusal recorded
    a pass. Enabling it for *both* tenants is the point of the setup below.
    """

    ENABLED = {
        "enabled": True, "max_auto_approve_amount": 5000,
        "require_active_vendor": True, "require_no_duplicate": True,
    }

    @pytest.fixture
    def eligible_invoice(self, db, insider):
        """A pending invoice in the caller's tenant that autopilot may approve."""
        from app.models.vendor import Vendor as VendorModel

        vendor = VendorModel(
            id=uuid.uuid4(), tenant_id=insider["tenant_id"],
            legal_name="Own Vendor", status=VendorStatus.ACTIVE,
            created_by=insider["id"],
        )
        db.add(vendor)
        db.flush()

        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=insider["tenant_id"], vendor_id=vendor.id,
            vendor_name=vendor.legal_name, invoice_number="OWN-AUTO-001",
            invoice_date=date(2026, 8, 4), total_amount=Decimal("1000"),
            current_state=InvoiceState.PENDING_APPROVAL, created_by=insider["id"],
        )
        db.add(invoice)
        db.flush()
        return invoice

    @pytest.fixture
    def outsider_eligible(self, db, outsider):
        """The same invoice, in the other tenant, equally eligible."""
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=outsider["tenant_id"],
            vendor_id=outsider["vendor"].id, vendor_name=outsider["vendor"].legal_name,
            invoice_number="OUTSIDER-AUTO-001",
            invoice_date=date(2026, 8, 4), total_amount=Decimal("1000"),
            current_state=InvoiceState.PENDING_APPROVAL,
            created_by=outsider["admin_id"],
        )
        db.add(invoice)
        db.flush()
        return invoice

    def _enable(self, client):
        response = client.put("/api/v1/config/autopilot", json=self.ENABLED)
        assert response.status_code == 200, response.text

    def test_a_run_approves_only_its_own_invoices(
        self, client, db, insider, eligible_invoice, outsider_eligible
    ):
        self._enable(client)

        response = client.post("/api/v1/autopilot/run", json={})
        assert response.status_code == 200, response.text
        approved = {a["invoice_number"] for a in response.json()["approved"]}

        assert "OWN-AUTO-001" in approved
        assert "OUTSIDER-AUTO-001" not in approved, (
            "autopilot approved another tenant's payables"
        )

    def test_the_other_tenants_invoice_is_left_pending(
        self, client, db, insider, eligible_invoice, outsider_eligible
    ):
        """Checked in the database, not just in the response: an approval that
        happened but went unreported is the worse outcome."""
        self._enable(client)
        client.post("/api/v1/autopilot/run", json={})

        db.refresh(outsider_eligible)
        state = str(getattr(
            outsider_eligible.current_state, "value", outsider_eligible.current_state
        )).lower()
        assert state == InvoiceState.PENDING_APPROVAL.value
        assert outsider_eligible.approved_by is None

    def test_the_preview_does_not_see_them_either(
        self, client, insider, eligible_invoice, outsider_eligible
    ):
        self._enable(client)

        response = client.get("/api/v1/autopilot/preview")
        assert response.status_code == 200, response.text
        assert "OUTSIDER-AUTO-001" not in response.text

    def test_cannot_revert_another_tenants_auto_approval(
        self, client, db, insider, eligible_invoice, outsider
    ):
        response = client.post(
            f"/api/v1/autopilot/{outsider['invoice'].id}/revert", json={}
        )
        assert response.status_code >= 400, response.text
