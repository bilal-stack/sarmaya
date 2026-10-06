"""Send one deliberate error to Sentry, to prove SENTRY_DSN reaches it.

Nothing in CI can prove this. The tests show what a report contains and what
is kept out of it, against a transport that writes to a list; whether this
DSN, from this host, actually lands in your project is a fact about the
network and the key, and the only way to know is to send one.

    python -m scripts.sentry_check

Run with the same environment the app runs with. It sends a single
RuntimeError tagged component=check, prints the event id, and prints the
SDK's own account of the send — a 401 or 403 there means the key is wrong
or revoked, not that the code is. Resolve the issue in Sentry once you have
seen it.
"""
import logging
import sys

import sentry_sdk

from app.core.config import settings
from app.core.error_tracking import build_options
from app.core.logging_config import configure_logging

logger = logging.getLogger(__name__)


def main() -> int:
    # Inside main rather than at import, so importing this module - as its
    # test does - does not reconfigure logging for everything else.
    configure_logging(debug=True)
    dsn = settings.SENTRY_DSN
    if not dsn:
        logger.error(
            "SENTRY_DSN is not set, so there is nothing to check. Put it in "
            ".env (or the host's environment) and run this again."
        )
        return 1

    options = build_options(dsn)
    # debug=True so the SDK reports how the send went. This is the one place
    # that is wanted: the app itself never runs with it.
    sentry_sdk.init(**options, debug=True)
    sentry_sdk.get_global_scope().set_tag("component", "check")

    try:
        raise RuntimeError(
            "Sentry check from scripts.sentry_check - deliberate, safe to resolve"
        )
    except RuntimeError as exc:
        event_id = sentry_sdk.capture_exception(exc)

    sentry_sdk.flush(timeout=15)
    logger.info(
        "Sent event %s to environment %r. Look in Sentry's Issues for "
        "'RuntimeError: Sentry check' within a minute or so. If the SDK lines "
        "above show a 401 or 403, the DSN's key is wrong or revoked.",
        event_id, options["environment"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
