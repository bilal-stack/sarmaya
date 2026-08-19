"""Multi-factor authentication.

Every other control in this system reasons about identities — segregation of
duties, maker-checker, approval limits, the bank-change rules. A stolen
password makes all of them wrong at once, silently, and leaves an audit trail
naming the victim as the person who acted.

The tests that matter most here are not the happy path. They are:

  * a challenge token cannot authenticate a request (or MFA is decorative);
  * a code cannot be used twice inside its own validity window;
  * enrolment cannot switch MFA on before a code has proved the app works;
  * turning it off needs more than the session it is protecting.
"""
import time
import uuid

import pyotp
import pytest

from app.core import mfa
from app.core.enums import UserRole
from app.core.security import (
    create_mfa_challenge_token, decode_access_token, PURPOSE_MFA_CHALLENGE,
)
from app.models.audit_log import AuditLog
from app.models.mfa_recovery_code import MfaRecoveryCode
from app.models.user import User
from app.services.mfa_service import MfaService

pytestmark = pytest.mark.integration


def _user(db, make_user, role=UserRole.MANAGER, password="CorrectHorse!2026"):
    from app.core.security import get_password_hash

    created = make_user(role)
    row = db.query(User).filter(User.id == created["id"]).first()
    row.password = get_password_hash(password)
    db.add(row)
    db.flush()
    return row


def _enrol(db, user) -> str:
    """Take a user through enrolment. Returns the raw TOTP secret."""
    service = MfaService(db)
    started = service.begin_enrolment(user)
    secret = started["secret"]
    service.confirm_enrolment(user, pyotp.TOTP(secret).now())
    return secret


def _next_code(secret: str) -> str:
    """The code for the *next* window.

    Confirming enrolment spends the current timestep, so a test that then
    verifies with `.now()` is replaying a used code — which the service
    correctly refuses. Real users hit a 30-second wait at most; tests should
    not sleep for it.
    """
    return pyotp.TOTP(secret).at(time.time() + mfa.PERIOD)


class TestEnrolment:
    def test_starting_enrolment_does_not_switch_it_on(self, db, tenant, make_user):
        """The QR code might not scan. Enabling MFA before a code has proved
        the app works locks people out of their own accounts, and the person
        who finds out is the one who can no longer log in to say so."""
        user = _user(db, make_user)

        MfaService(db).begin_enrolment(user)

        assert user.mfa_secret is not None
        assert user.mfa_enabled is False

    def test_confirming_with_a_real_code_switches_it_on(self, db, tenant, make_user):
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)

        codes = MfaService(db).confirm_enrolment(
            user, pyotp.TOTP(started["secret"]).now()
        )

        assert user.mfa_enabled is True
        assert user.mfa_confirmed_at is not None
        assert len(codes) == mfa.RECOVERY_CODE_COUNT

    def test_confirming_with_a_wrong_code_does_not(self, db, tenant, make_user):
        user = _user(db, make_user)
        MfaService(db).begin_enrolment(user)

        with pytest.raises(ValueError, match="not right"):
            MfaService(db).confirm_enrolment(user, "000000")

        assert user.mfa_enabled is False

    def test_the_secret_is_not_stored_in_the_clear(self, db, tenant, make_user):
        """A database dump alone should not hand over the second factor."""
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)

        assert started["secret"] not in (user.mfa_secret or "")
        assert mfa.decrypt_secret(user.mfa_secret) == started["secret"]

    def test_recovery_codes_are_not_stored_in_the_clear(self, db, tenant, make_user):
        """They are passwords. A plaintext list turns one database read into a
        permanent MFA bypass for every enrolled user."""
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)
        codes = MfaService(db).confirm_enrolment(user, pyotp.TOTP(started["secret"]).now())

        stored = [
            r.code_hash for r in
            db.query(MfaRecoveryCode).filter(MfaRecoveryCode.user_id == user.id).all()
        ]
        assert stored
        for code in codes:
            assert code not in stored


class TestTheChallengeTokenIsNotASession:
    """The single most important property here. If a challenge authenticates a
    request, the password alone has already got you in and the second factor is
    decoration."""

    def test_a_challenge_token_cannot_authenticate_a_request(
        self, db, tenant, client, make_user
    ):
        user = _user(db, make_user)
        _enrol(db, user)
        db.commit()
        challenge = create_mfa_challenge_token(user.id, user.tenant_id)

        response = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {challenge}"}
        )

        assert response.status_code == 401
        assert "cannot be used" in response.json()["detail"]

    def test_it_carries_no_role(self, db, tenant, make_user):
        """It is a receipt for one half of a login, not an identity."""
        user = _user(db, make_user, role=UserRole.ADMIN)
        payload = decode_access_token(
            create_mfa_challenge_token(user.id, user.tenant_id)
        )

        assert payload["purpose"] == PURPOSE_MFA_CHALLENGE
        assert "role" not in payload
        assert "token_version" not in payload

    def test_login_returns_a_challenge_and_no_access_token(
        self, db, tenant, client, make_user
    ):
        user = _user(db, make_user)
        _enrol(db, user)
        db.commit()

        response = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "CorrectHorse!2026"},
            params={"tenant": tenant.slug},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["mfa_required"] is True
        assert body["challenge_token"]
        assert body["access_token"] is None

    def test_an_account_without_mfa_is_unaffected(self, db, tenant, client, make_user):
        """The default path must not change for everybody who has not enrolled."""
        user = _user(db, make_user)
        db.commit()

        response = client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "CorrectHorse!2026"},
            params={"tenant": tenant.slug},
        )

        body = response.json()
        assert body["access_token"]
        assert body["mfa_required"] is False


class TestPassingTheChallenge:
    def test_a_correct_code_completes_the_sign_in(self, db, tenant, client, make_user):
        user = _user(db, make_user)
        secret = _enrol(db, user)
        db.commit()
        challenge = create_mfa_challenge_token(user.id, user.tenant_id)

        response = client.post(
            "/api/v1/auth/mfa/verify",
            json={"challenge_token": challenge, "code": _next_code(secret)},
        )

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    def test_a_wrong_code_does_not(self, db, tenant, client, make_user):
        user = _user(db, make_user)
        _enrol(db, user)
        db.commit()
        challenge = create_mfa_challenge_token(user.id, user.tenant_id)

        response = client.post(
            "/api/v1/auth/mfa/verify",
            json={"challenge_token": challenge, "code": "000000"},
        )

        assert response.status_code == 401

    def test_a_code_cannot_be_used_twice(self, db, tenant, make_user):
        """A TOTP code is a bearer credential for thirty seconds. If it is
        captured — over a shoulder, in a screenshot, by a proxy — it must not
        work a second time inside its own window."""
        user = _user(db, make_user)
        secret = _enrol(db, user)
        code = _next_code(secret)

        MfaService(db).verify(user, code)

        with pytest.raises(PermissionError):
            MfaService(db).verify(user, code)

    def test_repeated_wrong_codes_lock_verification(self, db, tenant, make_user):
        """Six digits against a 30-second window is brute-forceable without a
        limit."""
        user = _user(db, make_user)
        _enrol(db, user)

        for _ in range(mfa.MAX_FAILED_ATTEMPTS):
            with pytest.raises(PermissionError):
                MfaService(db).verify(user, "000000")

        with pytest.raises(PermissionError, match="Too many"):
            MfaService(db).verify(user, "000000")

    def test_a_failure_is_recorded(self, db, tenant, make_user):
        user = _user(db, make_user)
        _enrol(db, user)

        with pytest.raises(PermissionError):
            MfaService(db).verify(user, "000000")

        actions = [
            a.action for a in
            db.query(AuditLog).filter(AuditLog.object_id == user.id).all()
        ]
        assert "mfa_failed" in actions


class TestRecoveryCodes:
    def test_one_gets_you_in_when_the_phone_is_gone(self, db, tenant, make_user):
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)
        codes = MfaService(db).confirm_enrolment(user, pyotp.TOTP(started["secret"]).now())

        MfaService(db).verify(user, codes[0])   # does not raise

    def test_it_only_works_once(self, db, tenant, make_user):
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)
        codes = MfaService(db).confirm_enrolment(user, pyotp.TOTP(started["secret"]).now())

        MfaService(db).verify(user, codes[0])
        with pytest.raises(PermissionError):
            MfaService(db).verify(user, codes[0])

    def test_using_one_is_recorded_with_how_many_are_left(self, db, tenant, make_user):
        """A question somebody asks exactly once, in the worst circumstances."""
        user = _user(db, make_user)
        started = MfaService(db).begin_enrolment(user)
        codes = MfaService(db).confirm_enrolment(user, pyotp.TOTP(started["secret"]).now())

        MfaService(db).verify(user, codes[0])

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == user.id,
            AuditLog.action == "mfa_recovery_code_used",
        ).first()
        assert entry is not None
        assert "9 recovery code(s) left" in entry.comment
        assert MfaService(db).remaining_recovery_codes(user) == 9

    def test_reissuing_invalidates_the_old_set(self, db, tenant, make_user):
        user = _user(db, make_user)
        secret = _enrol(db, user)
        old = MfaService(db).regenerate_recovery_codes(user, _next_code(secret))
        # Move past the code just used, so the next call is not a replay.
        user.mfa_last_timestep = None
        db.flush()
        new = MfaService(db).regenerate_recovery_codes(user, _next_code(secret))

        assert set(old) != set(new)
        with pytest.raises(PermissionError):
            MfaService(db).verify(user, old[0])


class TestTurningItOff:
    def test_it_needs_the_password_as_well_as_a_code(self, db, tenant, make_user):
        """The session alone must not be able to remove the protection against
        a stolen session."""
        user = _user(db, make_user)
        secret = _enrol(db, user)

        with pytest.raises(PermissionError, match="Password"):
            MfaService(db).disable(user, "wrong-password", pyotp.TOTP(secret).now())

        assert user.mfa_enabled is True

    def test_it_needs_a_code_as_well_as_the_password(self, db, tenant, make_user):
        user = _user(db, make_user)
        _enrol(db, user)

        with pytest.raises(PermissionError, match="code"):
            MfaService(db).disable(user, "CorrectHorse!2026", "000000")

        assert user.mfa_enabled is True

    def test_with_both_it_comes_off_and_takes_the_secret_with_it(
        self, db, tenant, make_user
    ):
        user = _user(db, make_user)
        secret = _enrol(db, user)

        MfaService(db).disable(user, "CorrectHorse!2026", _next_code(secret))

        assert user.mfa_enabled is False
        assert user.mfa_secret is None
        assert MfaService(db).remaining_recovery_codes(user) == 0


class TestAdministrativeReset:
    """For the person who lost the phone and the codes. A way past the second
    factor, so what keeps it honest is that it cannot be done quietly."""

    def test_an_administrator_can_reset_it(self, db, tenant, make_user):
        target = _user(db, make_user)
        _enrol(db, target)
        admin = make_user(UserRole.ADMIN)

        MfaService(db).reset_for_user(target, admin)

        assert target.mfa_enabled is False
        assert target.mfa_secret is None

    def test_it_is_audited_under_the_administrator_who_did_it(
        self, db, tenant, make_user
    ):
        target = _user(db, make_user)
        _enrol(db, target)
        admin = make_user(UserRole.ADMIN)

        MfaService(db).reset_for_user(target, admin)

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == target.id, AuditLog.action == "mfa_reset"
        ).first()
        assert entry is not None
        assert str(entry.user_id) == str(admin["id"])

    def test_an_ordinary_role_cannot(self, db, tenant, make_user):
        target = _user(db, make_user)
        _enrol(db, target)
        clerk = make_user(UserRole.AP_CLERK)

        with pytest.raises(PermissionError):
            MfaService(db).reset_for_user(target, clerk)
