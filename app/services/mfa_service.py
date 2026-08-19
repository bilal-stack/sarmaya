"""Enrolling in, and passing, multi-factor authentication.

The flows matter more than the cryptography here, because the mistakes are in
the flows:

  * **Enrolment does not enable anything until a code is verified.** Turning
    MFA on the moment a secret is generated locks people out of their own
    accounts when the QR code did not scan properly, and the person who finds
    out is the one who can no longer log in to tell you.
  * **Login with MFA does not issue an access token.** It issues a challenge
    that can do exactly one thing: be exchanged for a token by someone holding
    the second factor. If the challenge were an ordinary token, MFA would be a
    screen rather than a control.
  * **Disabling requires the password and a current code.** Otherwise a stolen
    session removes the protection against stolen sessions.
  * **Recovery codes are single use and hashed**, and spending one is recorded.
"""
import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core import mfa
from app.core.config import settings
from app.core.security import verify_password
from app.models.mfa_recovery_code import MfaRecoveryCode
from app.models.user import User
from app.services.audit import log_audit
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "user"


def _now():
    return make_naive(to_utc(utc_now()))


class MfaService:
    def __init__(self, db: Session):
        self.db = db

    # --- enrolment -----------------------------------------------------------

    def begin_enrolment(self, user: User) -> Dict:
        """Generate a secret and return what an authenticator app needs.

        Nothing is enabled yet. The secret is stored so the confirmation step
        can check against it, but `mfa_enabled` stays false until a code proves
        the app and the server agree.
        """
        if user.mfa_enabled:
            raise ValueError(
                "Multi-factor authentication is already on for this account. "
                "Turn it off first if you need to enrol a new device."
            )

        secret = mfa.new_secret()
        user.mfa_secret = mfa.encrypt_secret(secret)
        user.mfa_enabled = False
        user.mfa_last_timestep = None
        user.mfa_failed_attempts = 0
        self.db.add(user)
        self.db.commit()

        return {
            "secret": secret,
            "provisioning_uri": mfa.provisioning_uri(
                secret, user.email, settings.APP_NAME
            ),
        }

    def confirm_enrolment(self, user: User, code: str) -> List[str]:
        """Prove the app works, then switch it on. Returns the recovery codes.

        Shown exactly once. Storing them in a form we could show again would
        make them a second copy of the second factor sitting in the database.
        """
        if user.mfa_enabled:
            raise ValueError("Multi-factor authentication is already on")
        if not user.mfa_secret:
            raise ValueError("Start enrolment first")

        secret = mfa.decrypt_secret(user.mfa_secret)
        if not secret:
            raise ValueError(
                "The stored secret could not be read. Start enrolment again."
            )

        accepted, timestep = mfa.verify_code(secret, code)
        if not accepted:
            raise ValueError("That code is not right. Check the app and try again.")

        user.mfa_enabled = True
        user.mfa_confirmed_at = _now()
        user.mfa_last_timestep = timestep
        user.mfa_failed_attempts = 0
        self.db.add(user)

        codes = self._issue_recovery_codes(user)

        log_audit(
            db=self.db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            object_type=OBJECT_TYPE,
            object_id=user.id,
            action="mfa_enabled",
            comment="Multi-factor authentication enabled.",
        )
        self.db.commit()
        return codes

    def disable(self, user: User, password: str, code: str) -> None:
        """Turn it off. Needs the password *and* a current code.

        Both, deliberately. Requiring only the session would let a stolen
        session remove the protection against stolen sessions; requiring only
        the code would let someone who briefly had the phone do it.
        """
        if not user.mfa_enabled:
            raise ValueError("Multi-factor authentication is not on")
        if not verify_password(password, user.password):
            raise PermissionError("Password is not correct")

        if not self._accept_code_or_recovery(user, code):
            raise PermissionError("That code is not right")

        user.mfa_enabled = False
        user.mfa_secret = None
        user.mfa_confirmed_at = None
        user.mfa_last_timestep = None
        user.mfa_failed_attempts = 0
        self.db.add(user)
        self._clear_recovery_codes(user)

        log_audit(
            db=self.db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            object_type=OBJECT_TYPE,
            object_id=user.id,
            action="mfa_disabled",
            comment="Multi-factor authentication disabled by the account holder.",
        )
        self.db.commit()

    # --- passing the challenge ----------------------------------------------

    def verify(self, user: User, code: str) -> None:
        """Accept a TOTP code or a recovery code, or raise.

        Raises rather than returning False so no caller can treat a failure as
        a pass by forgetting to check the result — the one place in this system
        where that mistake would be invisible and total.
        """
        if not user.mfa_enabled:
            raise ValueError("Multi-factor authentication is not on for this account")

        if (user.mfa_failed_attempts or 0) >= mfa.MAX_FAILED_ATTEMPTS:
            raise PermissionError(
                "Too many incorrect codes. Sign in again to start over, or use "
                "a recovery code."
            )

        if self._accept_code_or_recovery(user, code):
            user.mfa_failed_attempts = 0
            self.db.add(user)
            self.db.commit()
            return

        user.mfa_failed_attempts = (user.mfa_failed_attempts or 0) + 1
        self.db.add(user)
        log_audit(
            db=self.db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            object_type=OBJECT_TYPE,
            object_id=user.id,
            action="mfa_failed",
            comment=f"Incorrect code ({user.mfa_failed_attempts} in a row).",
        )
        self.db.commit()
        raise PermissionError("That code is not right")

    def _accept_code_or_recovery(self, user: User, code: str) -> bool:
        secret = mfa.decrypt_secret(user.mfa_secret) if user.mfa_secret else None
        if secret:
            accepted, timestep = mfa.verify_code(
                secret, code, last_used_timestep=user.mfa_last_timestep
            )
            if accepted:
                # Remembering the step is what stops the same code working
                # twice inside its own validity window.
                user.mfa_last_timestep = timestep
                self.db.add(user)
                return True
        return self._spend_recovery_code(user, code)

    # --- recovery codes ------------------------------------------------------

    def _issue_recovery_codes(self, user: User) -> List[str]:
        self._clear_recovery_codes(user)
        codes = mfa.new_recovery_codes()
        for code in codes:
            self.db.add(MfaRecoveryCode(
                tenant_id=user.tenant_id,
                user_id=user.id,
                code_hash=mfa.hash_recovery_code(code),
            ))
        self.db.flush()
        return codes

    def regenerate_recovery_codes(self, user: User, code: str) -> List[str]:
        """Replace the set. Needs a current code, because whoever can mint new
        recovery codes can bypass the second factor whenever they like."""
        if not user.mfa_enabled:
            raise ValueError("Multi-factor authentication is not on")
        if not self._accept_code_or_recovery(user, code):
            raise PermissionError("That code is not right")

        codes = self._issue_recovery_codes(user)
        log_audit(
            db=self.db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            object_type=OBJECT_TYPE,
            object_id=user.id,
            action="mfa_recovery_codes_reissued",
        )
        self.db.commit()
        return codes

    def _spend_recovery_code(self, user: User, code: str) -> bool:
        unused = (
            self.db.query(MfaRecoveryCode)
            .filter(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.used_at.is_(None),
            )
            .all()
        )
        for candidate in unused:
            if mfa.verify_recovery_code(code, candidate.code_hash):
                candidate.used_at = _now()
                self.db.add(candidate)
                log_audit(
                    db=self.db,
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    object_type=OBJECT_TYPE,
                    object_id=user.id,
                    action="mfa_recovery_code_used",
                    comment=(
                        f"{len(unused) - 1} recovery code(s) left. A code was "
                        "used instead of the authenticator app."
                    ),
                )
                return True
        return False

    def _clear_recovery_codes(self, user: User) -> None:
        self.db.query(MfaRecoveryCode).filter(
            MfaRecoveryCode.user_id == user.id
        ).delete(synchronize_session=False)

    def remaining_recovery_codes(self, user: User) -> int:
        return (
            self.db.query(MfaRecoveryCode)
            .filter(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.used_at.is_(None),
            )
            .count()
        )

    # --- administration ------------------------------------------------------

    def reset_for_user(self, target: User, current_user: dict) -> None:
        """Clear somebody's MFA so they can enrol again.

        For the person who lost the phone and the recovery codes. Deliberately
        an administrator action and deliberately audited under *their* name:
        this is a way past the second factor, and the only thing that keeps it
        honest is that it cannot be done quietly.
        """
        from app.core.roles import has_permission, PERM_MANAGE_USERS

        if not has_permission(current_user["role"], PERM_MANAGE_USERS):
            raise PermissionError(
                "You do not have permission to reset multi-factor authentication"
            )

        target.mfa_enabled = False
        target.mfa_secret = None
        target.mfa_confirmed_at = None
        target.mfa_last_timestep = None
        target.mfa_failed_attempts = 0
        self.db.add(target)
        self._clear_recovery_codes(target)

        log_audit(
            db=self.db,
            tenant_id=target.tenant_id,
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=target.id,
            action="mfa_reset",
            comment=(
                f"Multi-factor authentication reset for {target.email} by an "
                "administrator. They must enrol again."
            ),
        )
        self.db.commit()
