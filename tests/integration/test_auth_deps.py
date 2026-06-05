"""Integration tests for app/api/deps.get_current_user.

Identity (role, active) is resolved from the live user row, not the JWT claims,
so a role change or deactivation takes effect on the next request instead of
waiting out the token's lifetime.
"""
import pytest
from fastapi import HTTPException

from app.core.enums import UserRole
from app.core.security import create_access_token
from app.api.deps import get_current_user
from app.models.user import User

pytestmark = pytest.mark.integration


def _token(user: dict, role: str) -> str:
    return create_access_token({
        "sub": user["id"],
        "tenant_id": user["tenant_id"],
        "email": user["email"],
        "role": role,
    })


class TestGetCurrentUser:
    def test_role_is_read_live_from_db_not_token(self, db, make_user):
        user = make_user(UserRole.AP_CLERK)
        # Token deliberately carries a stale/forged role.
        token = _token(user, "admin")

        result = get_current_user(token=token, db=db)

        # The live DB role wins, not the token's "admin".
        assert result["role"] == "ap_clerk"
        assert result["id"] == user["id"]

    def test_role_change_takes_effect_immediately(self, db, make_user):
        user = make_user(UserRole.AP_CLERK)
        token = _token(user, "ap_clerk")

        # An admin downgrades... actually upgrades the user to manager in the DB.
        row = db.query(User).filter(User.id == user["id"]).first()
        row.role = UserRole.MANAGER
        db.flush()

        result = get_current_user(token=token, db=db)
        assert result["role"] == "manager"

    def test_inactive_user_is_rejected(self, db, make_user):
        user = make_user(UserRole.AP_CLERK)
        row = db.query(User).filter(User.id == user["id"]).first()
        row.is_active = False
        db.flush()

        token = _token(user, "ap_clerk")
        with pytest.raises(HTTPException) as exc:
            get_current_user(token=token, db=db)
        assert exc.value.status_code == 401

    def test_invalid_token_rejected(self, db):
        with pytest.raises(HTTPException) as exc:
            get_current_user(token="not-a-jwt", db=db)
        assert exc.value.status_code == 401


class TestTokenRevocation:
    """token_version embedded in the JWT is the revocation mechanism: logout
    (and password change) bumps the user's counter, so older tokens stop
    matching and are rejected on their next request."""

    def _token(self, user: dict, token_version) -> str:
        claims = {
            "sub": user["id"],
            "tenant_id": user["tenant_id"],
            "email": user["email"],
            "role": user["role"],
        }
        if token_version is not None:
            claims["token_version"] = token_version
        return create_access_token(claims)

    def test_token_matching_current_version_is_accepted(self, db, make_user):
        user = make_user(UserRole.AP_CLERK)
        token = self._token(user, token_version=0)

        result = get_current_user(token=token, db=db)
        assert result["id"] == user["id"]
        assert result["token_version"] == 0

    def test_token_rejected_after_version_bump(self, db, make_user):
        user = make_user(UserRole.AP_CLERK)
        # Token issued at version 0 (i.e. before logout).
        token = self._token(user, token_version=0)

        # Logout / password-change bumps the live counter.
        row = db.query(User).filter(User.id == user["id"]).first()
        row.token_version = 1
        db.flush()

        with pytest.raises(HTTPException) as exc:
            get_current_user(token=token, db=db)
        assert exc.value.status_code == 401
        assert "revoked" in exc.value.detail.lower()

    def test_token_without_version_claim_defaults_to_zero(self, db, make_user):
        """Tokens minted before this feature carry no token_version claim; they
        must keep working (treated as version 0) until they expire or a logout
        bumps the counter."""
        user = make_user(UserRole.AP_CLERK)
        token = self._token(user, token_version=None)

        result = get_current_user(token=token, db=db)
        assert result["id"] == user["id"]
