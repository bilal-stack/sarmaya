"""index the audit trail for the way the dashboards read it

Revision ID: 035_dashboard_indexes
Revises: 034_mfa
Create Date: 2026-08-19 00:00:00.000000

The dashboards ask the audit trail questions of the form "every entry with
action X in the last N days". The only index that touched action was
`ix_audit_logs_object_chain`, which leads with (tenant_id, object_type,
object_id) — perfect for reading one object's history, useless for finding
every entry of a kind, so those queries fell back to a sequential scan.

Measured before adding this, on 20k invoices and 60k audit rows: the approval
bottlenecks dashboard took 544ms of an 885ms page, all of it one sequential
scan. That is the shape that gets worse linearly and quietly — fine in
development, unusable at a year of real volume.

Adding the index rather than caching the result, deliberately. A cache would
have hidden a scan that was going to keep growing, and traded a slow page for
a page that is occasionally wrong about how much money is stuck — which is the
one thing this dashboard must never be.
"""
from alembic import op

revision = '035_dashboard_indexes'
down_revision = '034_mfa'
branch_labels = None
depends_on = None


def upgrade():
    # (tenant_id, action, timestamp) matches how every dashboard filters:
    # scoped to the tenant, one or more actions, over a window. Timestamp last
    # so the range scan happens after both equality columns have narrowed it.
    op.create_index(
        "ix_audit_logs_action_time",
        "audit_logs",
        ["tenant_id", "action", "timestamp"],
    )

    # The inbox and the control room both count invoices by state constantly.
    op.create_index(
        "ix_invoices_state_entered",
        "invoices",
        ["tenant_id", "current_state", "state_entered_at"],
    )


def downgrade():
    op.drop_index("ix_invoices_state_entered", table_name="invoices")
    op.drop_index("ix_audit_logs_action_time", table_name="audit_logs")
