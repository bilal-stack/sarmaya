"""A request's log lines have to be findable from the id the client was given.

The gap this covers: a correlation id existed, but it was minted inside the
500 handler. So somebody quoting the id from an error message could find
exactly one log entry — the exception — and nothing about what led to it,
which is the half you actually need. The id now starts at the edge and rides a
ContextVar through everything the request touches.

The ContextVar matters more than it looks. This app is async: one thread
serves many concurrent requests, so a thread-local would tag a log line with
whichever request last touched that thread — wrong in a way that reads as
right, and only under load.
"""
import json
import logging

import pytest

from app.core.enums import UserRole
from app.core.logging_config import (
    JsonFormatter, RequestIdFilter, configure_logging, request_id_var,
)

pytestmark = pytest.mark.integration


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="probe", level=logging.INFO, pathname=__file__, lineno=1,
        msg="payment released", args=None, exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    RequestIdFilter().filter(record)
    return record


class TestTheJsonFormat:
    def test_it_emits_one_object_per_line(self):
        token = request_id_var.set("req-1")
        try:
            line = JsonFormatter().format(_record())
        finally:
            request_id_var.reset(token)

        payload = json.loads(line)
        assert "\n" not in line
        assert payload["message"] == "payment released"
        assert payload["request_id"] == "req-1"
        assert payload["level"] == "INFO"

    def test_extras_become_fields(self):
        """The point of a structured format. A number buried in a sentence
        cannot be filtered on; a field can."""
        payload = json.loads(
            JsonFormatter().format(_record(payment_number="PAY-7", amount=18400))
        )

        assert payload["payment_number"] == "PAY-7"
        assert payload["amount"] == 18400

    def test_a_traceback_stays_one_entry(self):
        """A multi-line stack trailing the message becomes forty log lines no
        filter can reassemble. It goes in a field instead."""
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = _record()
            record.exc_info = sys.exc_info()
            line = JsonFormatter().format(record)

        payload = json.loads(line)
        assert "\n" not in line
        assert "ValueError: boom" in payload["exception"]

    def test_an_unserialisable_extra_does_not_break_the_line(self):
        """A log call must never be the thing that fails. Anything json cannot
        take is repr'd rather than raising inside the formatter."""
        payload = json.loads(JsonFormatter().format(_record(obj=object())))

        assert "obj" in payload

    def test_an_extra_cannot_overwrite_a_real_field(self):
        """`extra` is merged into the record's __dict__, so a key colliding
        with a LogRecord attribute would silently replace what the formatter
        reads."""
        payload = json.loads(JsonFormatter().format(_record(message="hijacked")))

        assert payload["message"] == "payment released"


class TestConfiguringItTwiceIsSafe:
    def test_handlers_are_replaced_not_added(self):
        """Called by the app and again by a script importing it, this would
        otherwise duplicate every line — the failure that makes people stop
        trusting log volume."""
        configure_logging(debug=True)
        first = len(logging.getLogger().handlers)
        configure_logging(debug=True)

        assert len(logging.getLogger().handlers) == first == 1


class TestTheRequestId:
    def test_it_comes_back_on_the_response(self, client, as_user, make_user):
        response = client.get("/api/v1/dashboard/stats")

        assert response.headers.get("X-Request-ID")

    def test_an_inbound_id_is_kept(self, client, as_user, make_user):
        """A caller that already has one — a gateway, the frontend, another
        service — keeps the same thread through our logs rather than starting
        a second nobody can join up."""
        as_user(make_user(UserRole.ADMIN))

        response = client.get(
            "/api/v1/dashboard/stats", headers={"X-Request-ID": "caller-supplied"},
        )

        assert response.headers["X-Request-ID"] == "caller-supplied"

    def test_it_does_not_leak_into_the_next_request(self, client, as_user, make_user):
        """The context is reused, so an id left set would label the next
        request's logs with the previous request's identity."""
        as_user(make_user(UserRole.ADMIN))
        client.get("/api/v1/dashboard/stats", headers={"X-Request-ID": "first"})

        assert request_id_var.get() != "first"

    def test_two_requests_get_different_ids(self, client, as_user, make_user):
        as_user(make_user(UserRole.ADMIN))

        one = client.get("/api/v1/dashboard/stats").headers["X-Request-ID"]
        two = client.get("/api/v1/dashboard/stats").headers["X-Request-ID"]

        assert one != two
class TestTheIdOnAFailure:
    """The 500 path, which this file's docstring is about and nothing tested.

    The handler for an unhandled exception runs in Starlette's outermost
    layer, after the request-id middleware's `finally` has already reset the
    ContextVar. It read the id only from there, found None, and minted a fresh
    one — so the id a client quoted from its error payload matched no log line
    at all. The opposite of what the docstring above says was fixed.
    """

    @pytest.fixture
    def failing(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.main import internal_exception_handler, request_id_middleware

        # The real middleware and the real handler, on an app of their own, so
        # a deliberately failing route never joins the application's routes.
        app = FastAPI()
        app.middleware("http")(request_id_middleware)
        app.add_exception_handler(Exception, internal_exception_handler)

        @app.get("/fails")
        def fails():
            raise RuntimeError("deliberate")

        return TestClient(app, raise_server_exceptions=False)

    def test_the_payload_carries_the_requests_own_id(self, failing):
        response = failing.get("/fails", headers={"X-Request-ID": "quoted-by-client"})

        assert response.status_code == 500
        assert response.json()["error"]["correlation_id"] == "quoted-by-client"

    def test_the_header_comes_back_on_a_failure_too(self, failing):
        """Every other response gets it from the middleware. This one is built
        after the middleware has unwound, so it has to set its own."""
        response = failing.get("/fails", headers={"X-Request-ID": "on-the-500"})

        assert response.headers.get("X-Request-ID") == "on-the-500"

    def test_a_minted_id_matches_between_payload_and_header(self, failing):
        response = failing.get("/fails")

        assert response.json()["error"]["correlation_id"] == response.headers["X-Request-ID"]

    def test_the_exception_log_line_carries_the_id(self, failing, caplog):
        """The record a person searches for. It used to be the one line from
        the request that the id did not find."""
        with caplog.at_level(logging.ERROR, logger="app.main"):
            failing.get("/fails", headers={"X-Request-ID": "find-me"})

        records = [r for r in caplog.records if r.name == "app.main"]
        assert records, "the handler logged nothing"
        assert records[-1].request_id == "find-me"
        assert "find-me" in records[-1].getMessage()

    def test_the_client_address_is_a_field_not_part_of_the_message(
        self, failing, caplog
    ):
        """Still filterable in the JSON logs; no longer free text that leaves
        the process inside an error report."""
        with caplog.at_level(logging.ERROR, logger="app.main"):
            failing.get("/fails")

        record = [r for r in caplog.records if r.name == "app.main"][-1]
        assert record.ip_address == "testclient"
        assert "testclient" not in record.getMessage()
