"""Error tracking: what reaches Sentry, and — more carefully — what does not.

**Off unless SENTRY_DSN is set.** Nothing is initialised, nothing is sent, and
every helper below is a no-op. That is the safe direction rather than a
convenience: a deployment that has not chosen a tracker sends nobody its
stack traces.

**Why the scrubbing is ours as well as Sentry's.** Sentry's default scrubber
removes values under keys like `password`, `token` and `cookie`. It knows
nothing about this domain, and it matches key names *exactly*. In an AP system
the things worth stealing are an `iban`, a `bank_account_number`, a
`swift_code`, a `national_id`, a `salary`, an `mfa_secret` — and the codebase
already spells IBAN five ways (`iban`, `old_iban`, `new_iban`,
`creditor_iban`, ...). An exact-match denylist would have to be kept in step
with every new column by hand, and the failure when it is not is silent. So
keys are matched by the fragments they are built from, and IBAN-shaped strings
are caught by shape wherever they appear — a log message, a repr, a
positional argument — since a value with no key cannot be caught by its key.

**Request bodies are never sent, and neither are stack-frame locals.** In this
application a body is an invoice, a bank change or a payroll change. Locals
are off for a reason found by testing rather than assumed — see build_options:
with them on, a 500 shipped the caller's bearer token.

**What IS sent, deliberately:** the correlation id (the same one on every log
line and in the client's error payload, so one id finds everything), the
tenant id and user id as opaque UUIDs — never an email, a name or an IP —
and which process the error came from: the API, or one of the scheduled jobs.
"""
import logging
import re
from typing import Any, Optional

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.scrubber import EventScrubber

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Sentry's own marker, so a value we removed and a value it removed look the
#: same in the issue view.
REDACTED = "[Filtered]"

#: Whole words in a key that mark its value as sensitive. Matched against the
#: key split on `_`, `-`, `.` and spaces, so `creditor_bic` matches `bic` but
#: `alembic_version` — a real table name here — does not, as it would under a
#: substring match. A trailing "s" is ignored, so `tokens` and `secrets` are
#: caught as well as `token` and `secret`.
SENSITIVE_WORDS = frozenset({
    "iban", "swift", "bic", "salary", "totp", "otp", "password", "passwd",
    "secret", "token",
})

#: Multi-word fragments, matched as substrings of the normalised key. Each is
#: specific enough that a substring cannot misfire.
SENSITIVE_PHRASES = (
    "account_number", "bank_account", "national_id", "tax_id",
    "recovery_code", "mfa_secret", "routing_number", "sort_code",
    "api_key", "apikey", "private_key",
)

#: An IBAN by shape: country code, two check digits, 11–30 alphanumerics. The
#: shortest real IBAN is 15 characters (Norway). Upper-case only, because that
#: is how IBANs are stored and printed, and a case-insensitive version starts
#: matching ordinary words.
IBAN_PATTERN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

_SPLIT = re.compile(r"[_\-.\s]+")

#: `name=value` or `"name": value` inside free text: a pydantic repr in a log
#: line, a dict printed into an exception message, JSON in a bytes literal.
#: The name is checked with is_sensitive_key, so this only ever removes a value
#: whose key would have been removed had it arrived as a dict.
_KEY_VALUE = re.compile(
    r"""(?P<key>["']?[A-Za-z_][A-Za-z0-9_]*["']?)"""
    r"""(?P<sep>\s*[=:]\s*)"""
    r"""(?P<value>'[^']*'|"[^"]*"|[^\s,;)}\]]+)"""
)


def is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalised = key.strip().lower().replace("-", "_")
    if any(phrase in normalised for phrase in SENSITIVE_PHRASES):
        return True
    return any(
        word in SENSITIVE_WORDS or (word.endswith("s") and word[:-1] in SENSITIVE_WORDS)
        for word in _SPLIT.split(normalised)
    )


def scrub_text(text: str) -> str:
    """Remove sensitive values from free text, by name and by shape."""
    def _redact(match: "re.Match[str]") -> str:
        if is_sensitive_key(match.group("key").strip("'\"")):
            return f"{match.group('key')}{match.group('sep')}{REDACTED}"
        return match.group(0)

    return IBAN_PATTERN.sub(REDACTED, _KEY_VALUE.sub(_redact, text))


def scrub(value: Any) -> Any:
    """Walk anything an event can contain and remove what should not leave.

    Returns a new structure rather than mutating, so a caller holding the
    original — a log record, a local variable — is never altered by the act
    of reporting it.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_sensitive_key(k) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v) for v in value)
    if isinstance(value, str):
        return scrub_text(value)
    return value


def before_send(event: dict, hint: dict) -> Optional[dict]:
    """Runs after Sentry's own scrubber, so this only has to add the domain.

    It walks the whole event, breadcrumbs included. Breadcrumbs are the log
    lines leading up to an error — and "bank change requested to GB29..." is
    an ordinary thing for this application to log — but they leave the
    process only inside an event, so scrubbing them here covers them. A
    separate breadcrumb hook was tried and removed: no test could tell it was
    there, because this had already done its work.
    """
    return scrub(event)


def default_environment() -> str:
    return "development" if settings.DEBUG else "production"


def build_options(dsn: str, *, environment: Optional[str] = None) -> dict:
    """Everything passed to sentry_sdk.init, in one place a reviewer can read.

    Separate from init_error_tracking so a test can initialise with exactly
    these options and a capturing transport, rather than with a copy of them
    that could drift.
    """
    return dict(
        dsn=dsn,
        environment=environment or settings.SENTRY_ENVIRONMENT or default_environment(),
        release=settings.SENTRY_RELEASE,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # No IP addresses, cookies or user headers.
        send_default_pii=False,
        # No request bodies. See the module docstring.
        max_request_body_size="never",
        # OFF, and not as a precaution. It is the SDK's default to send every
        # stack frame's local variables, and with it on, a 500 shipped the
        # caller's bearer token: a Starlette Request serialises as its ASGI
        # scope, whose headers are a list of byte pairs keyed by *position*,
        # so no key-based scrubber — Sentry's or ours — can see the
        # Authorization header inside it. Sentry filtered that header in the
        # event's request section and then sent it anyway through eighteen
        # frames' locals, one of them app.main's own middleware. The client
        # address rode along the same way, and a pydantic model's repr carries
        # `bank_account_number='...'` as plain text.
        #
        # Patching that one heuristic at a time is a game lost on the first
        # shape nobody thought of. Without locals a report still has the
        # traceback, the exception message, source context, the scrubbed log
        # lines leading up to it, and the correlation id that finds the full
        # server log — which is where the detail belongs.
        include_local_variables=False,
        event_scrubber=EventScrubber(recursive=True),
        before_send=before_send,
        # Tracing is off by default. Whoever turns SENTRY_TRACES_SAMPLE_RATE up
        # should not also have to remember that spans carry their own text —
        # a query, a URL — and need the same treatment.
        before_send_transaction=before_send,
        # Named rather than discovered. Sentry otherwise switches on an
        # integration for every library it finds installed — the AI SDKs,
        # outbound HTTP — and "what instruments this process" should be
        # answerable by reading this list.
        auto_enabling_integrations=False,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            # Query breadcrumbs. Statements are parameterised by the ORM, so
            # they carry the shape of a query and not its values.
            SqlalchemyIntegration(),
            # INFO and above as breadcrumbs, ERROR and above as events. The
            # scheduled jobs report per-tenant failures through
            # logger.exception and carry on, so this is how a job's error
            # reaches Sentry without the job being rewritten around it.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )


def init_error_tracking(component: str) -> bool:
    """Initialise Sentry for this process. Returns whether it is on.

    `component` is which process this is — `api`, or `job:<name>` for a
    scheduled job — so an error can be traced to the thing that raised it.
    """
    dsn = settings.SENTRY_DSN
    if not dsn:
        return False

    options = build_options(dsn)
    sentry_sdk.init(**options)
    # The global scope, so every event from this process carries it, whatever
    # request or thread raised.
    sentry_sdk.get_global_scope().set_tag("component", component)
    # One line per request, each with the client address. Not an error, and
    # the address is exactly what send_default_pii=False exists to withhold.
    ignore_logger("uvicorn.access")
    logger.info(
        "Error tracking on: environment=%s component=%s",
        options["environment"], component,
    )
    return True


def tag_request(correlation_id: str) -> None:
    """Attach the request's correlation id to anything this request reports.

    Called from the request-id middleware and set on Sentry's scope there,
    rather than read from the ContextVar when an event is built: the
    middleware resets the ContextVar in its `finally`, which runs before
    Sentry's outer layer captures an exception that escaped. Read late, the id
    would always be None.
    """
    sentry_sdk.set_tag("correlation_id", correlation_id)


def tag_user(user_id: str, tenant_id: str) -> None:
    """The authenticated user, by id only.

    Deliberately not the email or name. A UUID is enough to look somebody up
    from inside the system, and useless to anybody who only has the report.
    """
    sentry_sdk.set_user({"id": user_id})
    sentry_sdk.set_tag("tenant_id", tenant_id)


def is_enabled() -> bool:
    return sentry_sdk.is_initialized()
