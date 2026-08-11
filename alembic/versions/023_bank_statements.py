"""add bank statements and their lines

Revision ID: 023_bank_statements
Revises: 022_payments
Create Date: 2026-08-11 00:00:00.000000

The bank's record, as opposed to ours. Everything else in the schema holds what
the company intended; these two tables hold what actually happened, and
reconciliation is the comparison.

`file_hash` is indexed and carries a unique constraint per tenant: importing the
same statement twice would duplicate every transaction on it, which both invents
money that never moved and offers a second candidate for a payment that is
already reconciled. The database refuses it rather than relying on the service
to check.

Amounts are stored positive with `is_debit` set. CAMT.053, MT940 and CSV
exports disagree about sign conventions, so the direction is decided once at
import and every reader downstream sees the same shape.

Both tables are tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '023_bank_statements'
down_revision = '022_payments'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")
STATEMENTS = 'bank_statements'
LINES = 'bank_statement_lines'


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {table}_tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)
    op.execute(f"""
        CREATE POLICY {table}_tenant_insert ON {table}
        FOR INSERT
        WITH CHECK (tenant_id::text = current_setting('app.current_tenant_id', TRUE))
    """)


def upgrade() -> None:
    op.create_table(
        STATEMENTS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('statement_reference', sa.String(100), nullable=False),
        sa.Column('account_identifier', sa.String(100), nullable=True),
        sa.Column('source_format', sa.String(20), nullable=False),
        sa.Column('statement_date', sa.Date(), nullable=True),
        sa.Column('opening_balance', sa.Numeric(15, 2), nullable=True),
        sa.Column('closing_balance', sa.Numeric(15, 2), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('file_hash', sa.String(64), nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=True),
        sa.Column('imported_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['imported_by'], ['users.id']),
        # Scoped to the tenant, not global: two tenants banking with the same
        # institution can legitimately hold byte-identical statement files.
        sa.UniqueConstraint('tenant_id', 'file_hash', name='uq_bank_statements_tenant_file'),
    )
    op.create_index(f'ix_{STATEMENTS}_tenant_id', STATEMENTS, ['tenant_id'])
    op.create_index(f'ix_{STATEMENTS}_statement_reference', STATEMENTS, ['statement_reference'])
    op.create_index(f'ix_{STATEMENTS}_file_hash', STATEMENTS, ['file_hash'])

    op.create_table(
        LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('bank_statement_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('value_date', sa.Date(), nullable=True),
        sa.Column('booking_date', sa.Date(), nullable=True),
        sa.Column('amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('is_debit', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('counterparty', sa.String(255), nullable=True),
        sa.Column('bank_reference', sa.String(255), nullable=True),
        sa.Column('matched_payment_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('matched_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('matched_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['bank_statement_id'], [f'{STATEMENTS}.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['matched_payment_id'], ['payments.id']),
        sa.ForeignKeyConstraint(['matched_by'], ['users.id']),
    )
    op.create_index(f'ix_{LINES}_tenant_id', LINES, ['tenant_id'])
    op.create_index(f'ix_{LINES}_bank_statement_id', LINES, ['bank_statement_id'])
    op.create_index(f'ix_{LINES}_bank_reference', LINES, ['bank_reference'])
    op.create_index(f'ix_{LINES}_matched_payment_id', LINES, ['matched_payment_id'])

    # One payment, one bank debit. A second line claiming the same payment means
    # the bank debited twice for one instruction — a duplicate payment, and the
    # reconciliation must not be able to paper over it by matching both.
    op.create_index(
        f'uq_{LINES}_matched_payment',
        LINES,
        ['matched_payment_id'],
        unique=True,
        postgresql_where=sa.text('matched_payment_id IS NOT NULL'),
    )

    for table in (STATEMENTS, LINES):
        _enable_rls(table)


def downgrade() -> None:
    for table in (LINES, STATEMENTS):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table(LINES)
    op.drop_table(STATEMENTS)
