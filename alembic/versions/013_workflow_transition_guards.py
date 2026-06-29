"""add guards to workflow_states (configurable transition guards)

Revision ID: 013_workflow_transition_guards
Revises: 012_ai_action_logs
Create Date: 2026-06-05 00:00:00.000000

The Build Book's state-machine template declares guards per transition. This
adds a `guards` JSON column to workflow_states holding {target_state:
[guard_name, ...]} so a transition's preconditions are configuration-first and
versioned, resolved by app/services/workflow_guards.py.
"""
from alembic import op
import sqlalchemy as sa

revision = '013_workflow_transition_guards'
down_revision = '012_ai_action_logs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'workflow_states',
        sa.Column('guards', sa.JSON(), nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('workflow_states', 'guards')
