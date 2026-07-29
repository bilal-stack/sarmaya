"""Every server-side timestamp default must record UTC.

All timestamp columns here are tz-naive and the application writes UTC into
them. A bare ``now()`` default returns a ``timestamptz`` that Postgres converts
into the *session's* zone on the way into a naive column, so a single column
ends up holding two different clocks depending on who wrote the row.

That skew is invisible on a UTC machine and silently wrong everywhere else,
and it matters here: ``state_entered_at`` prices SLA deadlines against
``utc_now()``, and ``audit_logs.timestamp`` orders the audit trail alongside
application-written timestamps. These tests fail if a new table reintroduces
the local-time default.
"""
import pytest
from sqlalchemy import text

from app.utils.datetime_helpers import utc_now


def _naive_timestamp_defaults(db):
    return db.execute(text("""
        SELECT table_name, column_name, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp without time zone'
          AND column_default IS NOT NULL
        ORDER BY table_name, column_name
    """)).fetchall()


class TestTimestampDefaultsAreUTC:

    def test_no_column_defaults_to_local_now(self, db):
        """A bare now() stores local time in a naive column."""
        offenders = [
            f"{t}.{c}" for t, c, default in _naive_timestamp_defaults(db)
            if "timezone" not in default.lower()
        ]
        assert not offenders, (
            "These columns default to local time instead of UTC: "
            f"{offenders}. Use UTC_NOW from app.models.base."
        )

    def test_schema_actually_has_timestamp_defaults(self, db):
        """Guards the test above from passing vacuously if the introspection
        query stops matching anything."""
        assert len(_naive_timestamp_defaults(db)) > 0

    def test_server_default_agrees_with_application_clock(self, db):
        """The end-to-end property: a server-defaulted timestamp and
        utc_now() must describe the same moment, not two zones."""
        before = utc_now().replace(tzinfo=None)
        server_now = db.execute(
            text("SELECT timezone('utc', now())")
        ).scalar()
        after = utc_now().replace(tzinfo=None)

        assert before <= server_now <= after, (
            f"server clock {server_now} is outside the application window "
            f"{before}..{after} — the database is writing a different zone"
        )
