"""store server-side timestamp defaults in UTC, not the server's local zone

Revision ID: 019_utc_timestamp_defaults
Revises: 018_delegations
Create Date: 2026-07-30 00:00:00.000000

Every timestamp column in this schema is ``TIMESTAMP WITHOUT TIME ZONE``, and
the application writes UTC into all of them (utc_now / make_naive). The server
-side defaults did not: ``now()`` yields a ``timestamptz``, which Postgres
converts to the *session's* zone when storing it into a naive column. So
``created_at``, ``audit_logs.timestamp`` and ``invoices.state_entered_at`` were
being written in local time while everything around them was UTC.

That is not cosmetic in this system:

  * ``state_entered_at`` starts the SLA timer and is compared against
    ``utc_now()``, so every deadline was skewed by the server's offset.
  * ``audit_logs.timestamp`` orders the audit trail and is interleaved with
    application-written timestamps when a correlation chain or evidence pack
    is assembled, so events could be ordered wrongly.

Existing rows are deliberately left as they are. ``audit_logs.timestamp`` is
part of the hash-chain payload (see services/audit_integrity), so rewriting
historical timestamps would invalidate every chain — destroying the exact
tamper-evidence the column exists to provide, in order to fix a display
offset. Correcting historical data is therefore an operational decision, not a
schema migration; the statement to do it for the non-hashed tables is recorded
in docs/DECISION_REGISTER.md (DR-012) and is not run here.
"""
from alembic import op

revision = '019_utc_timestamp_defaults'
down_revision = '018_delegations'
branch_labels = None
depends_on = None

UTC_NOW = "timezone('utc', now())"
LOCAL_NOW = "now()"


def _repoint(from_default: str, to_default: str) -> None:
    """Repoint every naive timestamp column currently defaulting to
    `from_default`.

    Discovered from the catalog rather than hardcoded: the set of tables grows
    with each module, and a stale list here would quietly leave new tables on
    local time — the same bug, reintroduced.
    """
    conn = op.get_bind()
    columns = conn.exec_driver_sql(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp without time zone'
          AND column_default = %s
        ORDER BY table_name, column_name
        """,
        (from_default,),
    ).fetchall()
    for table, column in columns:
        conn.exec_driver_sql(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {to_default}'
        )


def upgrade() -> None:
    _repoint(LOCAL_NOW, UTC_NOW)


def downgrade() -> None:
    _repoint(UTC_NOW, LOCAL_NOW)
