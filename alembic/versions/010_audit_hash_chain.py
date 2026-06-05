"""add hash chain to audit_logs (tamper-evident audit trail)

Revision ID: 010_audit_hash_chain
Revises: 009_user_token_version
Create Date: 2026-06-05 00:00:00.000000

The audit trail was append-only by convention only — a direct UPDATE/DELETE on
audit_logs left no trace. This adds per-object hash chaining: each entry stores
prev_hash + entry_hash, where entry_hash = SHA-256(prev_hash + canonical(row)).
Tampering with, deleting, or reordering an object's events now breaks the chain
and is reported by /audit/verify/{object_type}/{object_id}.

Existing rows are back-filled into valid chains here, ordered per object by
(timestamp, id), using the exact same hashing function the application uses at
verify time, so the entire historical trail becomes verifiable.
"""
from types import SimpleNamespace

from alembic import op
import sqlalchemy as sa

# Reuse the application's hashing so back-filled hashes match runtime verification.
from app.services.audit_integrity import compute_entry_hash, HASHED_FIELDS

revision = '010_audit_hash_chain'
down_revision = '009_user_token_version'
branch_labels = None
depends_on = None

TABLE = 'audit_logs'


def upgrade() -> None:
    op.add_column(TABLE, sa.Column('prev_hash', sa.String(64), nullable=True))
    op.add_column(TABLE, sa.Column('entry_hash', sa.String(64), nullable=True))
    op.create_index(
        'ix_audit_logs_object_chain',
        TABLE,
        ['tenant_id', 'object_type', 'object_id', 'timestamp'],
    )

    # Back-fill existing rows into per-object chains.
    conn = op.get_bind()
    columns = ", ".join(HASHED_FIELDS)
    rows = conn.execute(
        sa.text(
            f"SELECT {columns} FROM {TABLE} "
            "ORDER BY tenant_id, object_type, object_id, timestamp, id"
        )
    ).mappings().all()

    prev_by_object: dict = {}
    for row in rows:
        key = (row["tenant_id"], row["object_type"], row["object_id"])
        prev_hash = prev_by_object.get(key)
        entry_hash = compute_entry_hash(SimpleNamespace(**dict(row)), prev_hash)
        conn.execute(
            sa.text(
                f"UPDATE {TABLE} SET prev_hash = :prev, entry_hash = :entry WHERE id = :id"
            ),
            {"prev": prev_hash, "entry": entry_hash, "id": row["id"]},
        )
        prev_by_object[key] = entry_hash


def downgrade() -> None:
    op.drop_index('ix_audit_logs_object_chain', table_name=TABLE)
    op.drop_column(TABLE, 'entry_hash')
    op.drop_column(TABLE, 'prev_hash')
