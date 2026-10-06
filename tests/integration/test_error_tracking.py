"""What an error report carries to Sentry, and — the point — what it does not.

Every assertion here was a probe first. The SDK's defaults were tried against
this application before any of them were trusted, and they leaked:

  * With local variables on — the SDK's default — a 500 shipped the caller's
    bearer token. A Starlette Request serialises as its ASGI scope, whose
    headers are byte pairs keyed by position, so no key-based scrubber could
    see the Authorization header inside it. Sentry filtered that header in the
    event's request section and then sent it through eighteen frames' locals.
  * The client address reached Sentry inside the 500 handler's log message,
    despite send_default_pii=False, because the message was free text.
  * The correlation id on the report did not match the id in the client's
    error payload, because the handler minted a fresh one.

So the load-bearing class is TestWhatAReportNeverCarries, and the load-bearing
test in it is the bearer token. The rest pins what a report SHOULD carry, so
that making it safe never quietly makes it useless.

No network. Events are captured by a transport that keeps them in a list.
"""
import json
import logging

import pytest
import sentry_sdk
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from app.core import error_tracking
from app.core.config import Settings
from app.core.error_tracking import (
    REDACTED, build_options, is_sensitive_key, scrub_text, tag_user,
)

pytestmark = pytest.mark.integration

#: Well-formed and unroutable. Nothing is sent anywhere: the capturing
#: transport below replaces the network one.
FAKE_DSN = "https://public@o0.ingest.sentry.io/0"

BEARER = "eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOAD.SECRETSIGNATURE"
IBAN = "GB29NWBK60161331926819"
SECOND_IBAN = "PK36SCBL0000001123456702"
ACCOUNT_NUMBER = "31926819"
SALARY = 150123


class _Capture(Transport):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def capture_envelope(self, envelope):
        event = envelope.get_event()
        if event is not None:
            self.sink.append(event)


def _shut_down_sentry():
    sentry_sdk.flush()
    sentry_sdk.get_client().close()
    sentry_sdk.get_global_scope().remove_tag("component")
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def sentry_events():
    """Sentry on, with exactly the production options, reporting into a list.

    Torn down completely afterwards — tags on the global scope and the client
    itself — because Sentry's state is process-wide and would otherwise leak
    into every test that runs after this one.
    """
    events = []
    sentry_sdk.init(**build_options(FAKE_DSN), transport=_Capture(events))
    sentry_sdk.get_global_scope().set_tag("component", "api")
    try:
        yield events
    finally:
        _shut_down_sentry()


@pytest.fixture
def failing_app(sentry_events):
    """The real middleware and 500 handler, on an app of their own, with a
    route that fails while holding every kind of sensitive value."""
    from app.main import internal_exception_handler, request_id_middleware

    app = FastAPI()
    app.middleware("http")(request_id_middleware)
    app.add_exception_handler(Exception, internal_exception_handler)
    log = logging.getLogger("app.services.vendor_bank_service")

    @app.post("/fails")
    def fails(payload: dict):
        tag_user("user-uuid-1", "tenant-uuid-1")
        new_iban = IBAN  # noqa: F841 — a local is exactly what is being tested
        log.warning("Bank change requested to %s", IBAN)
        log.info(
            "Change %r", {"bank_account_number": ACCOUNT_NUMBER, "note": SECOND_IBAN},
        )
        raise RuntimeError("failed while saving the bank change")

    @app.get("/forbidden")
    def forbidden():
        raise HTTPException(status_code=403, detail="no")

    return TestClient(app, raise_server_exceptions=False)


def _fail(client, request_id="corr-1"):
    return client.post(
        "/fails",
        json={"iban": IBAN, "new_salary": SALARY},
        headers={"Authorization": f"Bearer {BEARER}", "X-Request-ID": request_id},
    )


def _only_event(events) -> dict:
    sentry_sdk.flush()
    assert len(events) == 1, f"expected one report, got {len(events)}"
    return events[0]


class TestOffUnlessConfigured:
    def test_no_dsn_means_nothing_is_initialised(self, monkeypatch):
        monkeypatch.setattr(error_tracking.settings, "SENTRY_DSN", None)

        assert error_tracking.init_error_tracking("api") is False
        assert error_tracking.is_enabled() is False

    def test_a_blank_dsn_is_off_rather_than_a_startup_failure(self):
        """How CI and hosting dashboards pass an unset secret — and how every
        scheduled job died on SMTP_ENABLED=""."""
        settings = Settings(SENTRY_DSN="", SENTRY_TRACES_SAMPLE_RATE="")

        assert settings.SENTRY_DSN is None
        assert settings.SENTRY_TRACES_SAMPLE_RATE == 0.0

    @pytest.mark.parametrize("not_a_dsn", [
        "3f9a1c02e7",                          # part of a key, or a code
        "my-org",                              # the org slug
        "https://o123.ingest.sentry.io/456",   # a DSN missing its key
    ])
    def test_something_that_is_not_a_dsn_is_refused_with_directions(
        self, not_a_dsn
    ):
        """The values beside the DSN on Sentry's setup page are the easy ones
        to paste instead. Left to the SDK, that fails at import with
        "Unsupported scheme ''", which says nothing about what to fix."""
        with pytest.raises(ValueError, match="Client Keys"):
            Settings(SENTRY_DSN=not_a_dsn)

    def test_a_sample_rate_outside_zero_to_one_is_refused(self):
        with pytest.raises(ValueError, match="between 0 and 1"):
            Settings(SENTRY_TRACES_SAMPLE_RATE="2")

    def test_a_dsn_turns_it_on_and_names_the_process(self, monkeypatch):
        """Through the real init_error_tracking, with only the transport
        swapped, so the component tag is read off an actual event."""
        events = []
        real_options = error_tracking.build_options
        monkeypatch.setattr(error_tracking.settings, "SENTRY_DSN", FAKE_DSN)
        monkeypatch.setattr(
            error_tracking, "build_options",
            lambda dsn, **kw: {**real_options(dsn, **kw), "transport": _Capture(events)},
        )
        try:
            assert error_tracking.init_error_tracking("job:probe") is True
            assert error_tracking.is_enabled() is True
            sentry_sdk.capture_message("probe")
            assert _only_event(events)["tags"]["component"] == "job:probe"
        finally:
            _shut_down_sentry()

    def test_a_laptop_does_not_report_as_production(self, monkeypatch):
        monkeypatch.setattr(error_tracking.settings, "SENTRY_ENVIRONMENT", None)
        monkeypatch.setattr(error_tracking.settings, "DEBUG", True)

        assert build_options(FAKE_DSN)["environment"] == "development"


class TestWhatAReportNeverCarries:
    """The class that matters. Each of these leaked, or would have, before
    the options and the 500 handler were changed."""

    def test_the_callers_bearer_token(self, failing_app, sentry_events):
        """A live credential, valid for hours. With local variables on, the
        SDK default, it shipped with every 500 through the ASGI scope."""
        _fail(failing_app)

        assert BEARER not in json.dumps(_only_event(sentry_events), default=str)

    def test_any_stack_frame_variables(self, failing_app, sentry_events):
        """The root cause of the leak above, checked directly, so that turning
        locals back on fails here even if the token happens to be truncated
        out of the serialised scope."""
        _fail(failing_app)

        frames = _only_event(sentry_events)["exception"]["values"][0]["stacktrace"]["frames"]
        assert frames, "no stack trace at all"
        assert not [f for f in frames if f.get("vars")]

    def test_the_client_address(self, failing_app, sentry_events):
        """TestClient's address is the literal string "testclient". The
        user-agent header says the same thing, legitimately, so it is checked
        everywhere except there."""
        _fail(failing_app)

        event = _only_event(sentry_events)
        event.get("request", {}).get("headers", {}).pop("user-agent", None)
        assert "testclient" not in json.dumps(event, default=str)

    def test_the_request_body(self, failing_app, sentry_events):
        _fail(failing_app)

        event = _only_event(sentry_events)
        assert not event.get("request", {}).get("data")
        blob = json.dumps(event, default=str)
        assert str(SALARY) not in blob
        assert IBAN not in blob

    def test_a_bank_detail_logged_earlier_in_the_request(
        self, failing_app, sentry_events
    ):
        """Breadcrumbs are the log lines leading up to an error, and logging a
        bank change is ordinary for this application."""
        _fail(failing_app)

        crumbs = _only_event(sentry_events).get("breadcrumbs", {}).get("values", [])
        messages = " ".join(str(c.get("message")) for c in crumbs)
        assert "Bank change requested to" in messages, "the breadcrumb itself vanished"
        assert IBAN not in messages
        assert SECOND_IBAN not in messages
        assert ACCOUNT_NUMBER not in messages

    def test_the_users_email(self, failing_app, sentry_events):
        _fail(failing_app)

        assert _only_event(sentry_events)["user"] == {"id": "user-uuid-1"}


class TestWhatAReportCarries:
    """Safe and useless is not the goal. These are what make a report worth
    receiving."""

    def test_the_same_correlation_id_the_client_was_given(
        self, failing_app, sentry_events
    ):
        """One id joins the client's error message, every log line, and the
        report. It did not, until the 500 handler stopped minting its own."""
        response = _fail(failing_app, request_id="quoted-by-client")

        event = _only_event(sentry_events)
        assert event["tags"]["correlation_id"] == "quoted-by-client"
        assert response.json()["error"]["correlation_id"] == "quoted-by-client"

    def test_which_tenant_and_which_process(self, failing_app, sentry_events):
        _fail(failing_app)

        tags = _only_event(sentry_events)["tags"]
        assert tags["tenant_id"] == "tenant-uuid-1"
        assert tags["component"] == "api"

    def test_the_exception_itself(self, failing_app, sentry_events):
        _fail(failing_app)

        value = _only_event(sentry_events)["exception"]["values"][0]
        assert value["type"] == "RuntimeError"
        assert "saving the bank change" in value["value"]

    def test_one_failure_is_one_report(self, failing_app, sentry_events):
        """The 500 handler logs the exception and the framework integration
        captures it. Without de-duplication that is two issues per failure,
        which halves the free plan's quota and doubles the noise."""
        _fail(failing_app)

        _only_event(sentry_events)

    def test_a_refusal_is_not_an_error(self, failing_app, sentry_events):
        """A 403 is the system working. Reporting it would bury the 500s."""
        failing_app.get("/forbidden")
        sentry_sdk.flush()

        assert sentry_events == []


class TestTheScheduledJobs:
    def test_a_job_failure_that_is_logged_and_survived_is_reported(
        self, sentry_events
    ):
        """The jobs catch a per-tenant failure, log it, and move to the next
        tenant, so nothing ever crashes. The logging integration is the only
        way that failure reaches Sentry."""
        sentry_sdk.get_global_scope().set_tag("component", "job:run_workflow_timers")
        log = logging.getLogger("scripts.run_workflow_timers")
        try:
            raise ValueError("escalation failed")
        except ValueError:
            log.exception("Workflow timers failed for tenant %s", "Acme")

        event = _only_event(sentry_events)
        assert event["tags"]["component"] == "job:run_workflow_timers"
        assert event["exception"]["values"][0]["type"] == "ValueError"

    @pytest.mark.parametrize("job", [
        "dispatch_notifications", "run_workflow_timers", "dispatch_integration_posts",
    ])
    def test_every_job_initialises_under_its_own_name(self, job):
        """Checked against the source, like the SoD table: a job added to
        schedulers.yml without this line reports nothing at all."""
        with open(f"scripts/{job}.py", encoding="utf-8") as handle:
            source = handle.read()

        assert f'init_error_tracking(component="job:{job}")' in source


class TestTheCheckScript:
    """scripts.sentry_check is what proves a real DSN works. It is never run
    against sentry.io here - that would send junk to somebody's project - so
    it runs with the capturing transport instead."""

    def test_it_sends_one_deliberate_error(self, monkeypatch):
        import scripts.sentry_check as check

        events = []
        real_options = check.build_options
        monkeypatch.setattr(check.settings, "SENTRY_DSN", FAKE_DSN)
        monkeypatch.setattr(
            check, "build_options",
            lambda dsn, **kw: {**real_options(dsn, **kw), "transport": _Capture(events)},
        )
        monkeypatch.setattr(check, "configure_logging", lambda **kw: None)
        try:
            assert check.main() == 0
            event = _only_event(events)
            assert event["tags"]["component"] == "check"
            assert "Sentry check" in event["exception"]["values"][0]["value"]
        finally:
            _shut_down_sentry()

    def test_without_a_dsn_it_says_so_and_fails(self, monkeypatch):
        import scripts.sentry_check as check

        monkeypatch.setattr(check.settings, "SENTRY_DSN", None)
        monkeypatch.setattr(check, "configure_logging", lambda **kw: None)

        assert check.main() == 1


class TestTheScrubber:
    @pytest.mark.parametrize("key", [
        # Every spelling of a bank detail that exists in this codebase.
        "iban", "old_iban", "new_iban", "creditor_iban",
        "bank_account_number", "old_bank_account_number",
        "new_bank_account_number", "creditor_account_number",
        "bank_account_name", "swift_code", "old_swift_code", "creditor_bic",
        # HR and tax.
        "national_id", "tax_id", "salary", "base_salary", "current_salary",
        "new_salary",
        # Authentication.
        "mfa_secret", "mfa_recovery_codes", "mfa_recovery_code", "password",
        "client_secret", "access_token", "api_key", "apikey",
        # Plurals, which a whole-word match would otherwise let through.
        "tokens", "secrets", "passwords",
    ])
    def test_every_real_sensitive_field_is_caught(self, key):
        assert is_sensitive_key(key), key

    @pytest.mark.parametrize("key", [
        # Look close, are not. `routing` here is approval routing, and `bic`
        # must match as a word or the migrations table goes too.
        "alembic_version", "routing_reason", "explain_approval_routing",
        "employee_number", "correlation_id", "invoice_number", "amount",
    ])
    def test_ordinary_fields_survive(self, key):
        assert not is_sensitive_key(key), key

    def test_a_repr_in_free_text_loses_its_values_not_its_shape(self):
        text = (
            f"BankChangeRequest(reason='moved', iban='{IBAN}', "
            f"bank_account_number='{ACCOUNT_NUMBER}')"
        )

        assert scrub_text(text) == (
            f"BankChangeRequest(reason='moved', iban={REDACTED}, "
            f"bank_account_number={REDACTED})"
        )

    def test_json_inside_a_bytes_literal(self):
        text = f'b\'{{"new_salary": {SALARY}, "employee_number": "E-1"}}\''

        scrubbed = scrub_text(text)
        assert str(SALARY) not in scrubbed
        assert '"employee_number": "E-1"' in scrubbed

    def test_an_iban_with_no_key_at_all_is_caught_by_shape(self):
        assert scrub_text(f"pay to {SECOND_IBAN} today") == f"pay to {REDACTED} today"

    @pytest.mark.parametrize("text", [
        "correlation_id=abc method=POST path=/api/v1/x",
        "at 20:01:57 see https://example.com/a?b=c",
        "INV-2026-0042 for PO-123",
        "3f2b9c1e-8a7d-4e2f-9b1c-0d4e5f6a7b8c",
    ])
    def test_ordinary_text_is_left_alone(self, text):
        assert scrub_text(text) == text
