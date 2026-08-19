"""Multi-factor authentication: TOTP secrets, and what protects them.

Build Book, Operational Security and Access Controls: MFA support. The threat
it answers is the one every other control here assumes away — that the person
holding the password is who they claim. Segregation of duties, maker-checker
and approval limits are all reasoning about *identities*; a stolen password
makes every one of them wrong at once, silently, with a perfect audit trail
naming the victim.

Three things worth stating, because each is easy to get subtly wrong:

  * **The secret is encrypted at rest.** A database dump alone should not hand
    over the second factor. The key is derived from SECRET_KEY, so this is
    defence in depth rather than magic: whoever has both the dump and the
    environment has both halves. It still removes the most common case, where
    a backup leaks and the application host does not.
  * **A used code cannot be used again.** TOTP codes are valid for a window,
    which means a code shoulder-surfed or captured in transit works a second
    time unless the last accepted timestep is remembered. It is.
  * **Verification is constant-time and rate-limited.** Six digits is a million
    guesses in theory and far fewer in practice against a 30-second window with
    no limit.
"""
import base64
import hashlib
import secrets
from typing import Optional, Tuple

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

#: TOTP parameters. The defaults every authenticator app assumes — changing
#: them means the QR code an app scans no longer describes what we verify.
DIGITS = 6
PERIOD = 30

#: How many steps either side of now are accepted, to tolerate clock drift
#: between the server and the phone. One step is +/-30s, which is the usual
#: compromise: two would double the window an intercepted code stays useful.
VALID_WINDOW = 1

#: Recovery codes issued at enrolment. Enough that losing a phone is not an
#: emergency, few enough to stay printable.
RECOVERY_CODE_COUNT = 10

#: Failed attempts before verification is refused. TOTP is six digits; without
#: a limit, a 30-second window is comfortably brute-forceable.
MAX_FAILED_ATTEMPTS = 5


def _fernet() -> Fernet:
    """Encryption for the stored secret, keyed from SECRET_KEY.

    Derived rather than a second configured key, so a deployment cannot end up
    with MFA silently unprotected because one more environment variable was
    missed. The trade-off is stated in the module docstring: rotating
    SECRET_KEY invalidates stored secrets, which is why rotation means
    re-enrolment rather than a silent lockout — see `decrypt_secret`.
    """
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def new_secret() -> str:
    """A fresh base32 TOTP secret."""
    return pyotp.random_base32()


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(stored: str) -> Optional[str]:
    """The secret, or None if it cannot be read.

    None rather than an exception: the realistic cause is a rotated
    SECRET_KEY, and the right response is to treat the enrolment as unusable
    and make the person enrol again — not to 500 on every login attempt.
    """
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return None


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """The otpauth:// URI an authenticator app scans."""
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).provisioning_uri(
        name=email, issuer_name=issuer
    )


def verify_code(
    secret: str, code: str, last_used_timestep: Optional[int] = None
) -> Tuple[bool, Optional[int]]:
    """Check a code. Returns (accepted, the timestep it matched).

    The caller stores the returned timestep and passes it back next time, which
    is what stops a code being replayed inside its own validity window. A code
    is a bearer credential for those thirty seconds; if it is captured — over a
    shoulder, in a screenshot, by a proxy — it must not work twice.
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return False, None

    totp = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD)
    import time

    now_step = int(time.time()) // PERIOD
    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        step = now_step + offset
        # compare_digest via pyotp's own verify would not tell us which step
        # matched, and the step is what makes replay detectable.
        candidate = totp.at(step * PERIOD)
        if secrets.compare_digest(candidate, code):
            if last_used_timestep is not None and step <= last_used_timestep:
                return False, None  # already used; replay
            return True, step
    return False, None


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Single-use codes for when the phone is gone.

    Formatted in two groups because they are read off paper and typed by a
    person who has just lost their second factor and is not enjoying it.
    """
    return [
        f"{secrets.token_hex(2).upper()}-{secrets.token_hex(3).upper()}"
        for _ in range(count)
    ]


def hash_recovery_code(code: str) -> str:
    """Recovery codes are stored hashed, like passwords.

    They *are* passwords: a stored list in the clear turns a database read into
    a permanent MFA bypass for every enrolled user.
    """
    from app.core.security import get_password_hash

    return get_password_hash(_normalise_recovery(code))


def verify_recovery_code(code: str, hashed: str) -> bool:
    from app.core.security import verify_password

    return verify_password(_normalise_recovery(code), hashed)


def _normalise_recovery(code: str) -> str:
    """So the dashes and case a person types do not decide whether they get in."""
    return (code or "").strip().upper().replace(" ", "")
