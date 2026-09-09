"""Logging that can be searched, and that can tie a request together.

Two gaps this closes, both of which only hurt in production.

**Nothing tied a request's log lines together.** A correlation id existed, but
it was minted inside the 500 handler — so the client got an id that appeared in
exactly one log line, and the twenty lines leading up to the failure carried
nothing to find them by. The id now starts at the edge, rides a ContextVar
through everything the request touches, and comes back on the response, so the
id in somebody's error message is the id that filters the log.

**The format was for a human at a terminal.** `%(asctime)s %(levelname)s ...`
is right in development and wrong on a host where the only way to ask "what
else happened on that request" is a text search across interleaved lines from
concurrent workers. In production the records are JSON objects with the fields
already separated, so the platform's log viewer can filter on them.

Deliberately no logging framework and no new dependency. structlog and friends
earn their place when there is a pipeline to feed; here the requirement is a
formatter and a ContextVar, and the standard library has both. What matters is
that the *fields* are structured — anything that reads JSON can consume this,
including whatever error tracker gets wired up later.
"""
import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

#: The current request's id. A ContextVar rather than a thread-local because
#: this app is async: one thread serves many concurrent requests, and a
#: thread-local would hand a log line whichever request happened to touch that
#: thread last — wrong in a way that looks right.
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

#: Third-party loggers that are informative once and noise forever after.
NOISY = ("httpx", "httpcore", "openai", "anthropic", "urllib3", "PIL")

#: Never emitted, whatever a caller passes. `logging` merges `extra` into the
#: record's __dict__, so a key colliding with a LogRecord attribute would
#: overwrite it — and these are the fields the formatter reads.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class RequestIdFilter(logging.Filter):
    """Attaches the current request id to every record.

    A filter rather than an adapter, because it has to apply to records this
    codebase never creates directly — SQLAlchemy's, uvicorn's, a library's —
    and those are exactly the ones worth correlating when something is slow.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            # The traceback goes in a field rather than trailing the message,
            # so a multi-line stack stays one log entry instead of becoming
            # forty that no filter can reassemble.
            payload["exception"] = self.formatException(record.exc_info)

        # Anything a caller passed as `extra`, which is the point of having a
        # structured format at all.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and key != "request_id":
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    """The development format, with the request id when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        request_id = getattr(record, "request_id", None)
        if request_id:
            return f"{base}  [{request_id[:8]}]"
        return base


def configure_logging(debug: bool = False) -> None:
    """Install handlers on the root logger. Safe to call more than once.

    JSON in production, readable in development. The switch is DEBUG rather
    than a separate setting: a deployment that has DEBUG on has bigger problems
    than its log format, and one more knob is one more thing to get wrong.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # Replace rather than add. Called twice — by the app and then by a script
    # importing it — this would otherwise duplicate every line, which is
    # exactly the failure that makes people distrust log volume.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    if debug:
        handler.setFormatter(HumanFormatter(
            fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    for name in NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def new_request_id() -> str:
    import uuid
    return str(uuid.uuid4())
