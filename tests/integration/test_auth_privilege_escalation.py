"""Role and password changes must not be self-service or URL-borne.

Two defects these tests pin down:

  * `PUT /auth/me` accepted `role` as a bare argument, which FastAPI binds to
    the query string. Any authenticated user could run
    `PUT /auth/me?role=admin` and grant themselves administrator — defeating
    segregation of duties, the approval matrix, delegation and autopilot
    bounds in a single request.
  * `POST /auth/change-password` took both passwords the same way, putting
    credentials in the request URL and therefore into access logs, proxy logs
    and browser history.

Both are wiring defects rather than logic errors, so they are only visible
over HTTP — the service layer never sees where a value came from.
"""
import pytest

from app.core.enums import UserRole
from app.core.roles import DEFAULT_ROLE
from app.models.user import User

pytestmark = pytest.mark.integration


def _role_of(db, user: dict) -> str:
    row = db.query(User).filter(User.id == user["id"]).first()
    return str(getattr(row.role, "value", row.role)).lower()


class TestSelfServiceRoleChangeIsRefused:

    def test_query_param_role_does_not_escalate(self, client, db, make_user, as_user):
        """The original exploit: PUT /auth/me?role=admin."""
        clerk = as_user(make_user(UserRole.AP_CLERK))

        client.put("/api/v1/auth/me?role=admin")

        # However the request is rejected, the one thing that must never
        # happen is the role actually changing.
        assert _role_of(db, clerk) == "ap_clerk"

    def test_role_in_the_body_is_ignored_too(self, client, db, make_user, as_user):
        clerk = as_user(make_user(UserRole.AP_CLERK))

        client.put("/api/v1/auth/me", json={"full_name": "Casey", "role": "admin"})

        assert _role_of(db, clerk) == "ap_clerk"

    def test_profile_name_still_updates(self, client, make_user, as_user):
        """The legitimate use of the endpoint keeps working."""
        as_user(make_user(UserRole.AP_CLERK))

        response = client.put("/api/v1/auth/me", json={"full_name": "Casey Clerk"})

        assert response.status_code == 200
        assert response.json()["full_name"] == "Casey Clerk"


class TestRoleChangeEndpoint:

    def test_requires_users_manage(self, client, db, make_user, as_user):
        target = make_user(UserRole.AP_CLERK)
        as_user(make_user(UserRole.MANAGER))

        response = client.patch(f"/api/v1/users/{target['id']}/role",
                                json={"role": "admin"})

        assert response.status_code == 403
        assert _role_of(db, target) == "ap_clerk"

    def test_admin_can_change_another_users_role(self, client, db, make_user, as_user):
        target = make_user(UserRole.AP_CLERK)
        as_user(make_user(UserRole.ADMIN))

        response = client.patch(f"/api/v1/users/{target['id']}/role",
                                json={"role": "manager"})

        assert response.status_code == 200
        assert _role_of(db, target) == "manager"

    def test_role_change_revokes_the_users_existing_tokens(self, client, db, make_user, as_user):
        """A demoted user must not keep their old authority until their token
        happens to expire."""
        target = make_user(UserRole.MANAGER)
        before = db.query(User).filter(User.id == target["id"]).first().token_version or 0
        as_user(make_user(UserRole.ADMIN))

        client.patch(f"/api/v1/users/{target['id']}/role", json={"role": "ap_clerk"})

        after = db.query(User).filter(User.id == target["id"]).first().token_version
        assert after == before + 1

    def test_cannot_change_your_own_role(self, client, db, make_user, as_user):
        admin = as_user(make_user(UserRole.ADMIN))

        response = client.patch(f"/api/v1/users/{admin['id']}/role",
                                json={"role": "auditor"})

        assert response.status_code == 400
        assert "own role" in response.json()["detail"]
        assert _role_of(db, admin) == "admin"

    def test_rejects_an_unknown_role(self, client, db, make_user, as_user):
        target = make_user(UserRole.AP_CLERK)
        as_user(make_user(UserRole.ADMIN))

        response = client.patch(f"/api/v1/users/{target['id']}/role",
                                json={"role": "superuser"})

        assert response.status_code == 400
        assert _role_of(db, target) == "ap_clerk"

    def test_last_admin_cannot_be_demoted(self, client, db, make_user, as_user, monkeypatch):
        """Otherwise a tenant can be left with nobody able to administer it.

        Today only `admin` holds users.manage, so this guard is unreachable in
        practice: the caller is always an admin and cannot change their own
        role, which leaves at least one behind. It is kept as defence in depth
        for when another role gains users.manage (the Build Book adds HR and
        procurement administration), so the permission gate is stubbed here to
        reach the code path the guard actually protects.
        """
        import app.api.users as users_api
        monkeypatch.setattr(users_api, "has_permission", lambda role, perm: True)

        only_admin = make_user(UserRole.ADMIN)
        as_user(make_user(UserRole.AUDITOR))

        response = client.patch(f"/api/v1/users/{only_admin['id']}/role",
                                json={"role": "manager"})

        assert response.status_code == 400
        assert "last remaining administrator" in response.json()["detail"]
        assert _role_of(db, only_admin) == "admin"


class TestPasswordChangeUsesTheBody:

    def test_query_string_credentials_are_refused(self, client, make_user, as_user):
        """Passwords in the URL must not be a working code path."""
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post(
            "/api/v1/auth/change-password"
            "?current_password=testpassword123&new_password=brandnewpass456"
        )

        assert response.status_code == 422

    def test_new_password_must_differ(self, client, make_user, as_user):
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post("/api/v1/auth/change-password", json={
            "current_password": "testpassword123",
            "new_password": "testpassword123",
        })

        assert response.status_code == 422

    def test_new_password_must_meet_minimum_length(self, client, make_user, as_user):
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post("/api/v1/auth/change-password", json={
            "current_password": "testpassword123",
            "new_password": "short",
        })

        assert response.status_code == 422


class TestTenantBoundary:
    """The users endpoints scope by tenant themselves.

    Tenant isolation here is normally RLS, but RLS is created by migration 003
    and is therefore absent from any database built with create_all — which is
    every developer and test database in this project, including the one these
    tests run against. Without an explicit filter the directory listed other
    tenants' staff and a role change could be applied across tenants; both were
    reproduced against a running server before this was added.
    """

    def test_directory_excludes_other_tenants(self, client, db, make_user, as_user, other_tenant_user):
        as_user(make_user(UserRole.ADMIN))

        response = client.get("/api/v1/users")

        assert response.status_code == 200
        emails = [u["email"] for u in response.json()]
        assert other_tenant_user["email"] not in emails

    def test_role_change_cannot_cross_tenants(self, client, db, make_user, as_user, other_tenant_user):
        as_user(make_user(UserRole.ADMIN))

        response = client.patch(
            f"/api/v1/users/{other_tenant_user['id']}/role", json={"role": "user"}
        )

        assert response.status_code == 404

        # Read the outsider back with *their* tenant bound. The request left the
        # caller's tenant bound to this session, and the scoping that just
        # refused the change would equally hide the row from this check —
        # leaving the assertion unable to tell "unchanged" from "invisible".
        from app.core.database import set_tenant_context

        set_tenant_context(db, other_tenant_user["tenant_id"])
        outsider = db.query(User).filter(User.id == other_tenant_user["id"]).first()
        assert outsider is not None
        assert str(getattr(outsider.role, "value", outsider.role)).lower() == "admin"


class TestAdminDemotionIsStillPossible:
    """The counterpart to the last-admin guard: it must not block legitimate
    demotions. Without a test for this, a guard that always fired would look
    like a passing suite."""

    def test_an_admin_can_be_demoted_when_another_remains(self, client, db, make_user, as_user):
        make_user(UserRole.ADMIN)          # the one who will remain
        target = make_user(UserRole.ADMIN)  # the one being demoted
        as_user(make_user(UserRole.ADMIN))  # the caller

        response = client.patch(f"/api/v1/users/{target['id']}/role",
                                json={"role": "manager"})

        assert response.status_code == 200
        assert _role_of(db, target) == "manager"


class TestRegistrationCannotGrantItselfAuthority:
    """`PUT /auth/me?role=admin` was the first version of this bug. The second
    was on the way in: `/auth/register` took `UserCreate`, which inherits
    `role`, so an unauthenticated request could name its own.

    Posting `{"role": "admin"}` at any tenant slug returned 201 with an
    administrator's token — remote takeover of any tenant whose slug could be
    guessed, needing no isolation bypass at all. Confirmed against a running
    server before it was fixed.
    """

    @pytest.fixture
    def open_registration(self, monkeypatch):
        """Self-registration is off by default; these tests are about what it
        does when a deployment turns it on."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", True)
        return settings

    @pytest.fixture
    def signup_client(self, db):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.core.database import get_db

        app.dependency_overrides[get_db] = lambda: db
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    def test_a_requested_role_is_not_honoured(
        self, signup_client, db, tenant, open_registration
    ):
        response = signup_client.post(
            f"/api/v1/auth/register?tenant={tenant.slug}",
            json={"email": "stranger@evil.test", "password": "Str0ngPassw0rd!",
                  "full_name": "Stranger", "role": "admin"},
        )

        assert response.status_code == 201, response.text
        created = db.query(User).filter(User.email == "stranger@evil.test").first()
        role = str(getattr(created.role, "value", created.role)).lower()
        assert role != "admin", "self-registration granted administrator rights"
        assert role == DEFAULT_ROLE

    def test_the_returned_token_is_not_an_admins(
        self, signup_client, db, tenant, open_registration
    ):
        """The response hands back a working session, so the role inside it is
        what an attacker would actually wield."""
        response = signup_client.post(
            f"/api/v1/auth/register?tenant={tenant.slug}",
            json={"email": "stranger2@evil.test", "password": "Str0ngPassw0rd!",
                  "role": "admin"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["user"]["role"] != "admin"

    def test_registration_is_closed_unless_a_deployment_opens_it(
        self, signup_client, db, tenant
    ):
        """Even at the clerk role a stranger could create vendors, raise
        invoices and prepare payment runs."""
        response = signup_client.post(
            f"/api/v1/auth/register?tenant={tenant.slug}",
            json={"email": "walkin@evil.test", "password": "Str0ngPassw0rd!"},
        )
        assert response.status_code == 403, response.text
        assert db.query(User).filter(User.email == "walkin@evil.test").first() is None


class TestAdministratorsCreateAccountsInstead:
    """What closing self-registration replaces it with: an act by someone who
    already holds users.manage, confined to their tenant, and audited."""

    def test_an_admin_creates_a_user(self, client, db, make_user, as_user):
        as_user(make_user(UserRole.ADMIN))

        response = client.post("/api/v1/users", json={
            "email": "New.Clerk@example.com", "password": "Str0ngPassw0rd!",
            "full_name": "New Clerk", "role": "ap_clerk",
        })

        assert response.status_code == 201, response.text
        assert response.json()["email"] == "new.clerk@example.com"

    def test_a_clerk_cannot_create_users(self, client, db, make_user, as_user):
        as_user(make_user(UserRole.AP_CLERK))

        response = client.post("/api/v1/users", json={
            "email": "sidekick@example.com", "password": "Str0ngPassw0rd!",
            "role": "admin",
        })

        assert response.status_code == 403, response.text
        assert db.query(User).filter(User.email == "sidekick@example.com").first() is None

    def test_a_weak_password_is_refused(self, client, db, make_user, as_user):
        as_user(make_user(UserRole.ADMIN))

        response = client.post("/api/v1/users", json={
            "email": "weak@example.com", "password": "short",
        })

        assert response.status_code == 422, response.text

    def test_the_creation_is_audited_with_the_role_granted(
        self, client, db, make_user, as_user
    ):
        from app.models.audit_log import AuditLog

        as_user(make_user(UserRole.ADMIN))
        response = client.post("/api/v1/users", json={
            "email": "audited@example.com", "password": "Str0ngPassw0rd!",
            "role": "cfo",
        })
        assert response.status_code == 201, response.text

        entry = (
            db.query(AuditLog)
            .filter(AuditLog.object_id == response.json()["id"],
                    AuditLog.action == "user_created")
            .first()
        )
        assert entry is not None
        assert entry.after_value["role"] == "cfo"

    def test_a_duplicate_email_in_the_same_tenant_is_refused(
        self, client, db, make_user, as_user
    ):
        admin = make_user(UserRole.ADMIN)
        as_user(admin)

        response = client.post("/api/v1/users", json={
            "email": admin["email"], "password": "Str0ngPassw0rd!",
        })
        assert response.status_code == 400, response.text
