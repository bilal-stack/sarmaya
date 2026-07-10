"""add SLA config to workflow_states + state timer to invoices

Revision ID: 014_workflow_sla
Revises: 013_workflow_transition_guards
Create Date: 2026-06-05 00:00:00.000000

Build Book: "SLA timers start when a task enters a state" with per-state
escalation config ({"hours": 48, "escalate_to": "..."}). Adds:
  * workflow_states.sla — per-state SLA config (configuration-first, versioned
    in the workflow snapshot);
  * invoices.state_entered_at — when the invoice entered its current state
    (the timer start), maintained by transition_state. Existing rows are
    back-filled from updated_at.
"""
from alembic import op
import sqlalchemy as sa

revision = '014_workflow_sla'
down_revision = '013_workflow_transition_guards'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'workflow_states',
        sa.Column('sla', sa.JSON(), nullable=False, server_default='{}'),
    )
    op.add_column(
        'invoices',
        sa.Column('state_entered_at', sa.DateTime(), server_default=sa.func.now(), nullable=True),
    )
    op.get_bind().execute(sa.text(
        "UPDATE invoices SET state_entered_at = updated_at WHERE state_entered_at IS NULL"
    ))


def downgrade() -> None:
    op.drop_column('invoices', 'state_entered_at')
    op.drop_column('workflow_states', 'sla')
