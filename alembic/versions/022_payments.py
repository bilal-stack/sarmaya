"""add payments and their lines

Revision ID: 022_payments
Revises: 021_goods_receipts
Create Date: 2026-08-09 00:00:00.000000

Where money leaves. The system never moves it: a payment records the decision
to settle a set of approved invoices and produces an instruction file a
treasury user uploads to their own banking portal.

Preparer and releaser are separate columns because the whole control is that
they are different people. Bank details are copied onto each line at
preparation time rather than read through the vendor at export: details can
change after a run is released, and the file that went to the bank has to stay
reconstructable from what was true when it was built.

Both tables are tenant-scoped with the usual ENABLE + FORCE RLS isolation.
"""
from alembic import op
import sqlalchemy as sa

revision = '022_payments'
down_revision = '021_goods_receipts'
branch_labels = None
depends_on = None

UTC_NOW = sa.text("timezone('utc', now())")
PAYMENTS = 'payments'
LINES = 'payment_lines'


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
        PAYMENTS,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_number', sa.String(100), nullable=False),
        sa.Column('payment_date', sa.Date(), nullable=False),
        sa.Column('method', sa.String(50), nullable=False, server_default='bank_transfer'),
        sa.Column('reference', sa.String(255), nullable=True),
        sa.Column('notes', sa.String(), nullable=True),
        sa.Column('currency', sa.String(10), nullable=True),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('correlation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('current_state', sa.String(50), nullable=True),
        sa.Column('state_entered_at', sa.DateTime(), server_default=UTC_NOW, nullable=True),
        sa.Column('prepared_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('released_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.Column('rejection_reason', sa.String(), nullable=True),
        sa.Column('bank_file_hash', sa.String(64), nullable=True),
        sa.Column('bank_file_generated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['prepared_by'], ['users.id']),
        sa.ForeignKeyConstraint(['released_by'], ['users.id']),
    )
    op.create_index(f'ix_{PAYMENTS}_tenant_id', PAYMENTS, ['tenant_id'])
    op.create_index(f'ix_{PAYMENTS}_payment_number', PAYMENTS, ['payment_number'])
    op.create_index(f'ix_{PAYMENTS}_correlation_id', PAYMENTS, ['correlation_id'])

    op.create_table(
        LINES,
        sa.Column('id', sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('payment_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('invoice_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('line_number', sa.Numeric(6, 0), nullable=False),
        sa.Column('amount', sa.Numeric(15, 2), nullable=False),
        sa.Column('vendor_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('vendor_name', sa.String(255), nullable=False),
        sa.Column('bank_account_name', sa.String(255), nullable=True),
        sa.Column('bank_account_number', sa.String(100), nullable=True),
        sa.Column('bank_name', sa.String(255), nullable=True),
        sa.Column('iban', sa.String(50), nullable=True),
        sa.Column('swift_code', sa.String(20), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW, nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['payment_id'], [f'{PAYMENTS}.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id']),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
    )
    op.create_index(f'ix_{LINES}_tenant_id', LINES, ['tenant_id'])
    op.create_index(f'ix_{LINES}_payment_id', LINES, ['payment_id'])
    op.create_index(f'ix_{LINES}_invoice_id', LINES, ['invoice_id'])

    for table in (PAYMENTS, LINES):
        _enable_rls(table)


def downgrade() -> None:
    for table in (LINES, PAYMENTS):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
    op.drop_table(LINES)
    op.drop_table(PAYMENTS)
