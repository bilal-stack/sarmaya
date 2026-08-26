"""Let an evidence pack cover a period or a control, not only one chain.

Build Book, Audit/Compliance: "One-click audit pack export per period and per
control." Until now every pack was assembled from a single `correlation_id` —
one invoice's story, end to end. That is the right shape for answering "show me
what happened to this invoice" and the wrong one for the question an auditor
actually opens with, which is "show me that this control operated across the
quarter."

Same table rather than a new one: both are sealed, hash-stamped bundles that
should appear together in one list of "packs that were produced". What differs
is the scope, so the scope becomes a column and `correlation_id` becomes
nullable — it is now meaningful for chain packs only.

Revision ID: 044_audit_pack_scope
Revises: 043_integration_default_accounts
"""
from alembic import op
import sqlalchemy as sa

revision = "044_audit_pack_scope"
down_revision = "043_integration_default_accounts"
branch_labels = None
depends_on = None


def upgrade():
    # Backfilled to "chain" for every existing row before the NOT NULL lands,
    # because every pack that exists today is a chain pack by construction.
    op.add_column(
        "evidence_packs",
        sa.Column("scope", sa.String(length=20), nullable=False,
                  server_default="chain"),
    )
    op.add_column(
        "evidence_packs",
        sa.Column("control", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "evidence_packs", sa.Column("period_start", sa.Date(), nullable=True),
    )
    op.add_column(
        "evidence_packs", sa.Column("period_end", sa.Date(), nullable=True),
    )

    op.alter_column(
        "evidence_packs", "correlation_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    op.create_index(
        "ix_evidence_packs_scope_period",
        "evidence_packs", ["scope", "period_start", "period_end"],
    )

    # A pack has to be coherent about what it covers. Without this, a period
    # pack carrying a stray correlation_id — or a chain pack with no chain —
    # is storable, and the thing being stored is a sealed document that lies
    # about its own scope. That is worth refusing at the database rather than
    # trusting every future caller to get right.
    op.create_check_constraint(
        "ck_evidence_packs_scope_coherent",
        "evidence_packs",
        "(scope = 'chain' AND correlation_id IS NOT NULL "
        "  AND period_start IS NULL AND period_end IS NULL AND control IS NULL)"
        " OR (scope = 'period' AND correlation_id IS NULL "
        "  AND period_start IS NOT NULL AND period_end IS NOT NULL "
        "  AND control IS NULL)"
        " OR (scope = 'control' AND correlation_id IS NULL "
        "  AND period_start IS NOT NULL AND period_end IS NOT NULL "
        "  AND control IS NOT NULL)",
    )


def downgrade():
    op.drop_constraint(
        "ck_evidence_packs_scope_coherent", "evidence_packs", type_="check",
    )
    op.drop_index("ix_evidence_packs_scope_period", table_name="evidence_packs")
    # Rows that are not chain packs have no correlation_id to restore, so they
    # cannot survive a downgrade. Removed rather than left to fail the NOT NULL.
    op.execute("DELETE FROM evidence_packs WHERE scope <> 'chain'")
    op.alter_column(
        "evidence_packs", "correlation_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("evidence_packs", "period_end")
    op.drop_column("evidence_packs", "period_start")
    op.drop_column("evidence_packs", "control")
    op.drop_column("evidence_packs", "scope")
