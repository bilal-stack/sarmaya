"""give a closed tender an SLA, for tenants that already have one

Revision ID: 031_rfq_award_sla
Revises: 030_watchlist_alerts
Create Date: 2026-08-18 00:00:00.000000

Every workflow had an SLA on its waiting state except RFQ, which had none on any
state. So a closed tender — quoting ended, vendors waiting on an answer, nothing
else chasing it — could sit unawarded indefinitely: never overdue in the Decision
Inbox, never escalated by the SLA runner, absent from the "overdue only" view.
A timer that does not exist never breaches, so the gap was invisible in exactly
the place built to make delay visible.

`config_defaults` now carries it for new tenants. This applies it to existing
ones, and only where the state still has no SLA at all — a tenant that has
configured its own is left alone, on the same principle as migration 026.
"""
from alembic import op
from sqlalchemy import text
import json

revision = '031_rfq_award_sla'
down_revision = '030_watchlist_alerts'
branch_labels = None
depends_on = None

SLA = {"hours": 48, "escalate_to": "manager"}


def upgrade() -> None:
    conn = op.get_bind()

    rows = conn.execute(text("""
        SELECT id, sla FROM workflow_states
        WHERE workflow_type = 'rfq' AND state_name = 'closed'
    """)).fetchall()

    updated = 0
    for row in rows:
        existing = row[1]
        if isinstance(existing, str):
            existing = json.loads(existing or '{}')
        # Only fill a genuinely empty SLA. Overwriting a configured one would
        # silently undo a tenant's own decision about how long an award may take.
        if existing:
            continue
        conn.execute(
            text("UPDATE workflow_states SET sla = :sla WHERE id = :id"),
            {"sla": json.dumps(SLA), "id": row[0]},
        )
        updated += 1

    print(f"  [031] Set an award SLA on {updated} closed-RFQ state(s).")


def downgrade() -> None:
    conn = op.get_bind()
    # Clears only the SLA this migration would have set, so a tenant that has
    # since configured a different one keeps it.
    #
    # Matched field by field with ->>, not with `sla = :value`: the column is
    # `json`, not `jsonb`, and Postgres gives `json` no equality operator at
    # all — that comparison does not return false, it raises. Comparing
    # `sla::text` would work only while the stored key order and whitespace
    # happened to match the literal here.
    conn.execute(
        text("""
            UPDATE workflow_states SET sla = '{}'
            WHERE workflow_type = 'rfq' AND state_name = 'closed'
              AND sla->>'hours' = :hours
              AND sla->>'escalate_to' = :escalate_to
        """),
        {"hours": str(SLA["hours"]), "escalate_to": SLA["escalate_to"]},
    )
