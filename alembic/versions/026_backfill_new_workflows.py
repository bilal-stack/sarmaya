"""seed newly-added workflows for tenants provisioned before they existed

Revision ID: 026_backfill_new_workflows
Revises: 025_requisitions_and_sourcing
Create Date: 2026-08-12 00:00:00.000000

`ConfigProvisioningService` seeds workflow states per workflow type precisely so
a tenant provisioned before a workflow existed can pick it up on the next run —
but nothing runs it again on its own. An existing tenant therefore came up with
requisitions and RFQs it could create but not move: the first submit failed with
"No workflow configured for 'requisition' state 'draft'".

Found by using the new screens against a tenant that predates them, which is the
only place it shows. A fresh deployment is unaffected, so it would have shipped
looking fine and broken for exactly the customers who already had data.

This backfills any workflow type a tenant is missing, leaving every existing
state untouched — a tenant that has edited its own workflow keeps it. Idempotent:
running it again adds nothing.
"""
from alembic import op
from sqlalchemy import text
from datetime import datetime
import json
import uuid

revision = '026_backfill_new_workflows'
down_revision = '025_requisitions_and_sourcing'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Imported rather than duplicated: this migration exists to apply the
    # canonical defaults to tenants that missed them, so a copy here would be a
    # second source of truth that silently drifts from the first.
    from app.services.config_defaults import DEFAULT_WORKFLOWS

    tenants = [row[0] for row in conn.execute(text("SELECT id FROM tenants"))]
    if not tenants:
        print("  [026] No tenants yet; nothing to backfill.")
        return

    seeded = 0
    for tenant_id in tenants:
        for workflow_type, states in DEFAULT_WORKFLOWS.items():
            existing = conn.execute(
                text(
                    "SELECT COUNT(*) FROM workflow_states "
                    "WHERE tenant_id = :tid AND workflow_type = :wt"
                ),
                {"tid": tenant_id, "wt": workflow_type},
            ).scalar()
            if existing:
                continue

            for (name, display, order, is_initial, is_final,
                 transitions, color, guards, sla) in states:
                conn.execute(
                    text("""
                        INSERT INTO workflow_states (
                            id, tenant_id, workflow_type, state_name, display_name,
                            state_order, is_initial, is_final, allowed_transitions,
                            guards, sla, color, created_at, updated_at
                        ) VALUES (
                            :id, :tenant_id, :workflow_type, :state_name, :display_name,
                            :state_order, :is_initial, :is_final,
                            CAST(:allowed_transitions AS json),
                            CAST(:guards AS json), CAST(:sla AS json),
                            :color, :now, :now
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "tenant_id": tenant_id,
                        "workflow_type": workflow_type,
                        "state_name": name,
                        "display_name": display,
                        "state_order": order,
                        "is_initial": is_initial,
                        "is_final": is_final,
                        "allowed_transitions": json.dumps(transitions),
                        "guards": json.dumps(guards or {}),
                        "sla": json.dumps(sla or {}),
                        "color": color,
                        "now": datetime.utcnow(),
                    },
                )
            seeded += 1
            print(f"  [026] Seeded '{workflow_type}' for tenant {tenant_id}.")

    if not seeded:
        print("  [026] Every tenant already had every workflow.")


def downgrade() -> None:
    """Deliberately does nothing.

    There is no way to tell a state this migration inserted from one a tenant
    has since edited or come to depend on, and deleting a workflow a tenant is
    mid-flight on would strand every record in it.
    """
    pass
