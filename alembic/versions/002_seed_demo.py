"""Seed demo tenant and user

Revision ID: 002_seed_demo
Revises: b105d820d4a4
Create Date: 2025-11-28 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '002_seed_demo'
down_revision: str = 'b105d820d4a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Insert demo tenant
    op.execute("""
        INSERT INTO tenants (id, name, slug, isolation_level, is_active, created_at, updated_at)
        VALUES (
            '00000000-0000-0000-0000-000000000001'::uuid,
            'Demo Company',
            'demo',
            'rls',
            true,
            NOW(),
            NOW()
        ) ON CONFLICT DO NOTHING;
    """)
    
    # Insert demo user (password: demo123)
    op.execute("""
        INSERT INTO users (id, tenant_id, email, password_hash, full_name, role, is_active, created_at, updated_at)
        VALUES (
            '00000000-0000-0000-0000-000000000002'::uuid,
            '00000000-0000-0000-0000-000000000001'::uuid,
            'demo@sarmaya.com',
            '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5NU2c4.gQ3YXm',
            'Demo User',
            'admin',
            true,
            NOW(),
            NOW()
        ) ON CONFLICT DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DELETE FROM users WHERE email = 'demo@sarmaya.com';")
    op.execute("DELETE FROM tenants WHERE slug = 'demo';")
