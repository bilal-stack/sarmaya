"""add universal correlation_id

Revision ID: 016_correlation_id
Revises: 015_policy_evals
Create Date: 2026-06-05 00:00:00.000000

Build Book: "Every transaction chain carries a correlation_id that links every
record across modules. Search must support correlation_id to reconstruct the
entire story instantly."

Adds correlation_id to invoices (where an AP chain starts today) and to the
three record types that describe what happened to it: audit_logs, policy_evals
and ai_action_logs. Existing invoices get a generated id and their history is
back-filled from it, so chains work for data created before this migration.

correlation_id is deliberately NOT added to the audit integrity hash (DR-011):
it is a linking field rather than a claim about what happened, and including it
would invalidate every hash written before this migration.
"""
from alembic import op
import sqlalchemy as sa

revision = '016_correlation_id'
down_revision = '015_policy_evals'
branch_labels = None
depends_on = None

TABLES = ('invoices', 'audit_logs', 'policy_evals', 'ai_action_logs')


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_index(f'ix_{table}_correlation_id', table, ['correlation_id'])

    conn = op.get_bind()
    # Every existing invoice starts its own chain.
    conn.execute(sa.text(
        "UPDATE invoices SET correlation_id = gen_random_uuid() WHERE correlation_id IS NULL"
    ))
    # Its existing history joins that chain.
    for table in ('audit_logs', 'policy_evals', 'ai_action_logs'):
        conn.execute(sa.text(f"""
            UPDATE {table} AS t
            SET correlation_id = i.correlation_id
            FROM invoices AS i
            WHERE t.object_type = 'invoice' AND t.object_id = i.id
              AND t.correlation_id IS NULL
        """))


def downgrade() -> None:
    for table in TABLES:
        op.drop_index(f'ix_{table}_correlation_id', table_name=table)
        op.drop_column(table, 'correlation_id')
