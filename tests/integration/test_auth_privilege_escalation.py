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
        outsider = db.query(User).filter(User.id == other_tenant_user["id"]).first()
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
