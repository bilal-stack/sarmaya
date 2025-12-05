"""Enhance audit logs with workflow, file, and AI tracking

Revision ID: 004_enhance_audit
Revises: 003_enable_rls
Create Date: 2025-11-28 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '004_enhance_audit'
down_revision: str = '003_enable_rls'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add workflow context
    op.add_column('audit_logs', sa.Column('workflow_step', sa.String(100), nullable=True))
    op.add_column('audit_logs', sa.Column('workflow_type', sa.String(50), nullable=True))
    
    # Add file/document linkage
    op.add_column('audit_logs', sa.Column('file_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('audit_logs', sa.Column('document_hash', sa.String(64), nullable=True))
    op.add_column('audit_logs', sa.Column('file_path', sa.String(500), nullable=True))
    
    # Add AI assistance tracking
    op.add_column('audit_logs', sa.Column('ai_assisted', sa.Boolean(), default=False))
    op.add_column('audit_logs', sa.Column('ai_provider', sa.String(50), nullable=True))
    op.add_column('audit_logs', sa.Column('ai_confidence', sa.Integer(), nullable=True))
    
    # Create foreign key to files
    op.create_foreign_key(
        'fk_audit_logs_file_id',
        'audit_logs',
        'files',
        ['file_id'],
        ['id'],
        ondelete='SET NULL'
    )
    
    # Create indexes for common queries
    op.create_index('idx_audit_workflow', 'audit_logs', ['tenant_id', 'workflow_type', 'workflow_step'])
    op.create_index('idx_audit_ai_assisted', 'audit_logs', ['tenant_id', 'ai_assisted'])
    op.create_index('idx_audit_file', 'audit_logs', ['file_id'])


def downgrade() -> None:
    op.drop_index('idx_audit_file', table_name='audit_logs')
    op.drop_index('idx_audit_ai_assisted', table_name='audit_logs')
    op.drop_index('idx_audit_workflow', table_name='audit_logs')
    
    op.drop_constraint('fk_audit_logs_file_id', 'audit_logs', type_='foreignkey')
    
    op.drop_column('audit_logs', 'ai_confidence')
    op.drop_column('audit_logs', 'ai_provider')
    op.drop_column('audit_logs', 'ai_assisted')
    op.drop_column('audit_logs', 'file_path')
    op.drop_column('audit_logs', 'document_hash')
    op.drop_column('audit_logs', 'file_id')
    op.drop_column('audit_logs', 'workflow_type')
    op.drop_column('audit_logs', 'workflow_step')
