"""make seeded workflow + approval config live

Revision ID: 008_seed_config_policies
Revises: 007_invoice_duplicate_review
Create Date: 2026-06-02 00:00:00.000000

Migration 002 seeded workflow_states with UPPERCASE state_name and NULL
allowed_transitions, and seeded no policies at all. The result was dead config:
transition_state (which looks up by lowercase state_name) never matched a row
and always fell back to the hardcoded change_state machine, and
evaluate_approval_role always fell back to the hardcoded 250k split.

This migration makes the demo tenant's config actually drive behaviour:
  * lowercases state_name and fills allowed_transitions with the linear flow
    (draft -> validated -> pending_approval -> approved -> paid, with reject and
    cancel branches), so transition_state reads the DB.
  * seeds two approval_limit policies (cfo for amount > 250k, manager otherwise)
    so evaluate_approval_role reads the DB.

The hardcoded fallbacks remain in code as a safety net for tenants that have no
config rows.
"""
from alembic import op
from sqlalchemy import text
from datetime import datetime
import json
import uuid

revision = '008_seed_config_policies'
down_revision = '007_invoice_duplicate_review'
branch_labels = None
depends_on = None

DEMO_TENANT_ID = '00000000-0000-0000-0000-000000000001'

# Linear invoice flow, keyed by lowercase state_name (matches InvoiceState values).
ALLOWED_TRANSITIONS = {
    'draft': ['validated', 'cancelled'],
    'validated': ['pending_approval', 'cancelled'],
    'pending_approval': ['approved', 'rejected'],
    'approved': ['paid', 'cancelled'],
    'rejected': ['draft'],
    'paid': [],
    'cancelled': [],
}

# Approval matrix: highest priority rule that matches wins (see
# evaluate_approval_role). cfo for > 250k, manager as the catch-all.
APPROVAL_POLICIES = [
    {
        'policy_name': 'CFO approval over 250k',
        'priority': 100,
        'rule_config': {'amount_threshold': 250_000, 'operator': 'greater_than', 'required_role': 'cfo'},
    },
    {
        'policy_name': 'Manager approval up to 250k',
        'priority': 0,
        'rule_config': {'amount_threshold': 0, 'operator': 'greater_equal', 'required_role': 'manager'},
    },
]


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Normalise state names to lowercase and populate allowed_transitions
    #    for the demo tenant's invoice workflow.
    conn.execute(text(
        "UPDATE workflow_states SET state_name = lower(state_name) "
        "WHERE tenant_id = :tid AND workflow_type = 'invoice'"
    ), {'tid': DEMO_TENANT_ID})

    for state_name, transitions in ALLOWED_TRANSITIONS.items():
        conn.execute(text(
            "UPDATE workflow_states SET allowed_transitions = CAST(:trans AS json) "
            "WHERE tenant_id = :tid AND workflow_type = 'invoice' AND state_name = :state"
        ), {'tid': DEMO_TENANT_ID, 'state': state_name, 'trans': json.dumps(transitions)})

    # 2. Seed the approval matrix as configuration rows (idempotent on re-run).
    for policy in APPROVAL_POLICIES:
        conn.execute(text(
            "INSERT INTO policies (id, tenant_id, policy_type, policy_name, description, "
            "rule_config, applies_to, is_active, priority, created_at, updated_at) "
            "SELECT :id, :tid, 'approval_limit', :name, :desc, CAST(:rule AS json), 'invoice', "
            "true, :priority, :now, :now "
            "WHERE NOT EXISTS (SELECT 1 FROM policies WHERE tenant_id = :tid "
            "AND policy_type = 'approval_limit' AND policy_name = :name)"
        ), {
            'id': str(uuid.uuid4()),
            'tid': DEMO_TENANT_ID,
            'name': policy['policy_name'],
            'desc': 'Seeded approval routing rule (configuration-first).',
            'rule': json.dumps(policy['rule_config']),
            'priority': policy['priority'],
            'now': datetime.utcnow(),
        })


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(text(
        "DELETE FROM policies WHERE tenant_id = :tid AND policy_type = 'approval_limit' "
        "AND policy_name IN (:n1, :n2)"
    ), {
        'tid': DEMO_TENANT_ID,
        'n1': APPROVAL_POLICIES[0]['policy_name'],
        'n2': APPROVAL_POLICIES[1]['policy_name'],
    })

    conn.execute(text(
        "UPDATE workflow_states SET allowed_transitions = NULL, "
        "state_name = upper(state_name) "
        "WHERE tenant_id = :tid AND workflow_type = 'invoice'"
    ), {'tid': DEMO_TENANT_ID})
