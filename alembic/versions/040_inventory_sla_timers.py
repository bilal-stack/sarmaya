"""give the inventory workflows a clock

Revision ID: 040_inventory_sla_timers
Revises: 039_link_receiving_to_stock
Create Date: 2026-08-20 00:00:00.000000

Migration 038 gave inventory adjustments and vendor returns their states, and
`config_defaults` gave those states SLA settings — 24 hours on pending
approval, 30 days on a dispatched return waiting for its credit note.

None of it did anything. `sla_status` computes a deadline from
`state_entered_at`, which neither model had, and the escalation runner scans
models declaring `WORKFLOW_TYPE`, which neither declared. So both records sat
outside every clock in the system: the Decision Inbox showed them as never
overdue, and nothing would ever have escalated them.

That is precisely the failure DR-009 recorded for escalations generally and
DR-037 for tenders specifically — a deadline nobody is watching is not a
deadline — arriving a third time in a new module. The column is what the timer
reads; `WORKFLOW_TYPE` on the models is what puts them in front of the runner.

Backfilled from `created_at` rather than left null: an existing adjustment has
been waiting since it was raised, and starting its clock at zero on deploy
would hide exactly the backlog the SLA exists to surface.
"""
from alembic import op
import sqlalchemy as sa

revision = '040_inventory_sla_timers'
down_revision = '039_link_receiving_to_stock'
branch_labels = None
depends_on = None

TABLES = ('inventory_adjustments', 'vendor_returns')


def upgrade():
    for table in TABLES:
        op.add_column(table, sa.Column('state_entered_at', sa.DateTime(), nullable=True))
        op.execute(f"UPDATE {table} SET state_entered_at = created_at")


def downgrade():
    for table in TABLES:
        op.drop_column(table, 'state_entered_at')
