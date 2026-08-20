"""people, and the HR records that need a signature

Revision ID: 041_hr
Revises: 040_inventory_sla_timers
Create Date: 2026-08-20 00:00:00.000000

Build Book Variant C, "HR OS".

`employees` is separate from `users` deliberately. A user is a login; an
employee is a person the company employs. Merging them would mean either
issuing logins to people who should not have one — an access-control decision
made accidentally by HR — or losing every employee who never signs in, which in
a real workforce is most of them. `user_id` is a nullable link for the overlap.

Three sensitive columns are stored in full and masked on the way out:
`base_salary`, `national_id` and `bank_account`. The Build Book asks for
field-level masking on national IDs and bank accounts; salary belongs with them
because an HR list that renders everybody's pay to whoever opens it is a data
breach with a UI. Payroll variance is arithmetic on real numbers, so the
masking is at the service boundary, exactly where vendor bank details already
do it — not in the column.

Every table carries `state_entered_at` from the start. Migration 040 exists
because the inventory workflows were added without it and their SLAs were
therefore inert; adding it here up front is that lesson applied rather than
repeated.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '041_hr'
down_revision = '040_inventory_sla_timers'
branch_labels = None
depends_on = None

NEW_TABLES = (
    'employees',
    'headcount_requests',
    'onboarding_tasks',
    'payroll_change_requests',
    'expense_reimbursements',
)


def _timestamps():
    return (
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.text("timezone('utc', now())")),
    )


def _soft_delete():
    return (
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('deletion_reason', sa.String(500), nullable=True),
    )


def _tenant_fk():
    return sa.Column(
        'tenant_id', postgresql.UUID(as_uuid=True),
        sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False,
        index=True,
    )


def upgrade():
    op.create_table(
        'employees',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('employee_number', sa.String(64), nullable=False, index=True),
        sa.Column('full_name', sa.String(255), nullable=False),
        sa.Column('work_email', sa.String(255), nullable=True, index=True),
        # SET NULL rather than CASCADE: deleting an account must never delete
        # the employment record behind it.
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('job_title', sa.String(255), nullable=False),
        sa.Column('employment_type', sa.String(30), nullable=False,
                  server_default='permanent'),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active', index=True),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        sa.Column('manager_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('employees.id'), nullable=True, index=True),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('base_salary', sa.Numeric(15, 2), nullable=True),
        sa.Column('pay_currency', sa.String(3), nullable=True),
        sa.Column('national_id', sa.String(64), nullable=True),
        sa.Column('bank_account', sa.String(64), nullable=True),
        sa.Column('is_sensitive_role', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('background_check_cleared', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'employee_number',
                            name='uq_employees_tenant_number'),
    )
    op.create_index(
        'ix_employees_org_unit_status', 'employees', ['org_unit_id', 'status'],
    )

    op.create_table(
        'headcount_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('request_number', sa.String(64), nullable=False, index=True),
        sa.Column('job_title', sa.String(255), nullable=False),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        sa.Column('positions', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('employment_type', sa.String(30), nullable=False,
                  server_default='permanent'),
        sa.Column('is_sensitive_role', sa.Boolean(), nullable=False,
                  server_default='false'),
        sa.Column('annual_cost', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('justification', sa.String(), nullable=True),
        sa.Column('target_start_date', sa.Date(), nullable=True),
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='draft', index=True),
        sa.Column('state_entered_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.String(), nullable=True),
        sa.Column('filled_by_employee_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('employees.id'), nullable=True),
        sa.Column('filled_at', sa.DateTime(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'request_number',
                            name='uq_headcount_tenant_number'),
    )

    op.create_table(
        'onboarding_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('employee_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('employees.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('flow', sa.String(20), nullable=False,
                  server_default='onboarding', index=True),
        sa.Column('category', sa.String(30), nullable=False, index=True),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('owning_team', sa.String(50), nullable=True, index=True),
        sa.Column('assigned_to', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='pending', index=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('completed_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('resolution_note', sa.String(500), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_timestamps(),
    )
    op.create_index(
        'ix_onboarding_employee_status', 'onboarding_tasks',
        ['employee_id', 'status'],
    )

    op.create_table(
        'payroll_change_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('request_number', sa.String(64), nullable=False, index=True),
        sa.Column('employee_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('employees.id'), nullable=False, index=True),
        sa.Column('reason_code', sa.String(40), nullable=False, index=True),
        sa.Column('reason_note', sa.String(), nullable=True),
        sa.Column('current_salary', sa.Numeric(15, 2), nullable=True),
        sa.Column('new_salary', sa.Numeric(15, 2), nullable=False),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('effective_date', sa.Date(), nullable=False),
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='draft', index=True),
        sa.Column('state_entered_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.String(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'request_number',
                            name='uq_payroll_change_tenant_number'),
    )

    op.create_table(
        'expense_reimbursements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant_fk(),
        sa.Column('claim_number', sa.String(64), nullable=False, index=True),
        sa.Column('employee_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('employees.id'), nullable=False, index=True),
        sa.Column('org_unit_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('org_units.id'), nullable=True, index=True),
        sa.Column('category', sa.String(50), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('total_amount', sa.Numeric(15, 2), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(3), nullable=True),
        sa.Column('incurred_date', sa.Date(), nullable=False),
        sa.Column('has_receipt', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('current_state', sa.String(30), nullable=False,
                  server_default='draft', index=True),
        sa.Column('state_entered_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('approved_by', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id'), nullable=True),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.String(), nullable=True),
        sa.Column('policy_override_reason', sa.String(), nullable=True),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True),
                  nullable=True, index=True),
        *_soft_delete(),
        *_timestamps(),
        sa.UniqueConstraint('tenant_id', 'claim_number',
                            name='uq_reimbursement_tenant_number'),
    )
    op.create_index(
        'ix_reimbursement_employee_state', 'expense_reimbursements',
        ['employee_id', 'current_state'],
    )

    for table in NEW_TABLES:
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


def downgrade():
    for table in NEW_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")

    op.drop_index('ix_reimbursement_employee_state',
                  table_name='expense_reimbursements')
    op.drop_table('expense_reimbursements')
    op.drop_table('payroll_change_requests')
    op.drop_index('ix_onboarding_employee_status', table_name='onboarding_tasks')
    op.drop_table('onboarding_tasks')
    op.drop_table('headcount_requests')
    op.drop_index('ix_employees_org_unit_status', table_name='employees')
    op.drop_table('employees')
