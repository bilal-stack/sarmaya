"""seed demo data — opt-in only

Revision ID: 002_seed_demo_data
Revises: 9ee83d7f931f
Create Date: 2024-01-15 10:00:00.000000

This migration creates five active accounts, one of them an administrator, all
sharing a password written in this file. Running it unconditionally means
`alembic upgrade head` on a server hands anyone who can reach the login page a
working admin account — which is what it did until this gate was added, found by
running the migrations against an empty database for the first time.

So it now does nothing unless SEED_DEMO_DATA is set. A deployment gets an empty
database and bootstraps its first tenant and administrator with
`python -m scripts.bootstrap_tenant`, which takes its credentials from the
environment rather than from source control.

Gated rather than deleted: the seed is genuinely useful for local work, and no
database has ever applied this migration (every developer and test database in
this project is built with create_all), so nothing depends on it having run.
"""
from alembic import op
from sqlalchemy import text
from datetime import datetime
import os
import uuid
from passlib.context import CryptContext

# revision identifiers, used by Alembic.
revision = '002_seed_demo_data'
down_revision = '9ee83d7f931f'
branch_labels = None
depends_on = None

# Password hashing context
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

#: Deliberately opt-in, and deliberately not read from app settings: this must
#: be an explicit act by whoever runs the migration, not something a stray value
#: in a .env file can switch on.
SEED_FLAG = "SEED_DEMO_DATA"


def _seeding_requested() -> bool:
    return os.getenv(SEED_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def upgrade() -> None:
    """Seed demo tenant, users, and workflow states — only when asked."""
    if not _seeding_requested():
        print(
            f"  [002] Skipping demo seed data. These accounts share a password "
            f"published in source, so they must never exist on a server. "
            f"Set {SEED_FLAG}=true to seed a local database."
        )
        return

    conn = op.get_bind()

    # 1. Insert demo tenant
    demo_tenant_id = '00000000-0000-0000-0000-000000000001'
    conn.execute(text("""
        INSERT INTO tenants (
            id, 
            name, 
            slug, 
            isolation_level,
            subscription_tier,
            is_active,
            created_at, 
            updated_at
        )
        VALUES (
            :id, 
            :name, 
            :slug, 
            :isolation_level,
            :subscription_tier,
            :is_active,
            :created_at, 
            :updated_at
        )
        ON CONFLICT (id) DO NOTHING
    """), {
        'id': demo_tenant_id,
        'name': 'Demo Tenant',
        'slug': 'demo',
        'isolation_level': 'rls',
        'subscription_tier': 'free',
        'is_active': True,
        'status': 'ACTIVE',  # Use enum value
        'created_at': datetime.utcnow(),
        'updated_at': datetime.utcnow()
    })
    
    # 2. Insert demo users with properly hashed passwords
    hashed_password = pwd_context.hash('password123')
    
    users_data = [
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'email': 'admin@demo.com',
            'password': hashed_password,
            'full_name': 'Demo Admin',
            'role': 'ADMIN',  # Use enum value
            'is_active': True
        },
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'email': 'cfo@demo.com',
            'password': hashed_password,
            'full_name': 'Demo CFO',
            'role': 'CFO',  # Use enum value
            'is_active': True
        },
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'email': 'manager@demo.com',
            'password': hashed_password,
            'full_name': 'Demo Manager',
            'role': 'MANAGER',  # Use enum value
            'is_active': True
        },
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'email': 'clerk@demo.com',
            'password': hashed_password,
            'full_name': 'Demo AP Clerk',
            'role': 'AP_CLERK',  # Use enum value
            'is_active': True
        },
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'email': 'auditor@demo.com',
            'password': hashed_password,
            'full_name': 'Demo Auditor',
            'role': 'AUDITOR',  # Use enum value
            'is_active': True
        }
    ]
    
    for user in users_data:
        conn.execute(text("""
            INSERT INTO users (id, tenant_id, email, password, full_name, role, is_active, created_at, updated_at)
            VALUES (:id, :tenant_id, :email, :password, :full_name, :role, :is_active, :created_at, :updated_at)
        """), {
            **user,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        })
    
    # 3. Insert workflow states for invoice workflow
    workflow_states = [
        {
            'state_name': 'DRAFT',
            'display_name': 'Draft',
            'workflow_type': 'invoice',
            'state_order': 1,
            'is_initial': True,
            'is_final': False,
            'color': '#gray'
        },
        {
            'state_name': 'VALIDATED',
            'display_name': 'Validated',
            'workflow_type': 'invoice',
            'state_order': 2,
            'is_initial': False,
            'is_final': False,
            'color': '#blue'
        },
        {
            'state_name': 'PENDING_APPROVAL',
            'display_name': 'Pending Approval',
            'workflow_type': 'invoice',
            'state_order': 3,
            'is_initial': False,
            'is_final': False,
            'color': '#yellow'
        },
        {
            'state_name': 'APPROVED',
            'display_name': 'Approved',
            'workflow_type': 'invoice',
            'state_order': 4,
            'is_initial': False,
            'is_final': False,
            'color': '#green'
        },
        {
            'state_name': 'REJECTED',
            'display_name': 'Rejected',
            'workflow_type': 'invoice',
            'state_order': 5,
            'is_initial': False,
            'is_final': True,
            'color': '#red'
        },
        {
            'state_name': 'PAID',
            'display_name': 'Paid',
            'workflow_type': 'invoice',
            'state_order': 6,
            'is_initial': False,
            'is_final': True,
            'color': '#purple'
        },
        {
            'state_name': 'CANCELLED',
            'display_name': 'Cancelled',
            'workflow_type': 'invoice',
            'state_order': 7,
            'is_initial': False,
            'is_final': True,
            'color': '#orange'
        }
    ]
    
    for state in workflow_states:
        conn.execute(text("""
            INSERT INTO workflow_states (
                id, 
                tenant_id, 
                workflow_type, 
                state_name, 
                display_name,
                state_order, 
                is_initial, 
                is_final,
                color,
                created_at, 
                updated_at
            )
            VALUES (
                :id, 
                :tenant_id, 
                :workflow_type, 
                :state_name, 
                :display_name,
                :state_order, 
                :is_initial, 
                :is_final,
                :color,
                :created_at, 
                :updated_at
            )
        """), {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            **state,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        })
    
    # 4. Insert demo vendors
    vendors_data = [
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'legal_name': 'ABC Suppliers Ltd',
            'display_name': 'ABC Suppliers',
            'vendor_code': 'VND-001',
            'email': 'accounts@abcsuppliers.com',
            'phone': '+92-300-1234567',
            'address': '123 Main Street, Karachi, Pakistan',
            'bank_account_name': 'ABC Suppliers Ltd',
            'bank_account_number': 'PK12345678901234567890',
            'bank_name': 'MCB Bank',
            'iban': 'PK36SCBL0000001123456702',
            'tax_id': 'NTN-1234567',
            'status': 'ACTIVE',  # Use enum value
            'risk_score': 0
        },
        {
            'id': str(uuid.uuid4()),
            'tenant_id': demo_tenant_id,
            'legal_name': 'XYZ Services (Pvt) Ltd',
            'display_name': 'XYZ Services',
            'vendor_code': 'VND-002',
            'email': 'billing@xyzservices.com',
            'phone': '+92-321-9876543',
            'address': '456 Commercial Avenue, Lahore, Pakistan',
            'bank_account_name': 'XYZ Services',
            'bank_account_number': 'PK98765432109876543210',
            'bank_name': 'HBL',
            'iban': 'PK70HABB0000091234567890',
            'tax_id': 'NTN-7654321',
            'status': 'ACTIVE',  # Use enum value
            'risk_score': 0
        }
    ]
    
    for vendor in vendors_data:
        conn.execute(text("""
            INSERT INTO vendors (
                id, 
                tenant_id, 
                legal_name, 
                display_name,
                vendor_code,
                email, 
                phone, 
                address,
                bank_account_name,
                bank_account_number,
                bank_name,
                iban,
                tax_id,
                status,
                risk_score,
                created_at, 
                updated_at
            )
            VALUES (
                :id, 
                :tenant_id, 
                :legal_name, 
                :display_name,
                :vendor_code,
                :email, 
                :phone, 
                :address,
                :bank_account_name,
                :bank_account_number,
                :bank_name,
                :iban,
                :tax_id,
                :status,
                :risk_score,
                :created_at, 
                :updated_at
            )
        """), {
            **vendor,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        })


def downgrade() -> None:
    """Remove seed data."""
    conn = op.get_bind()
    demo_tenant_id = '00000000-0000-0000-0000-000000000001'
    
    # Delete in reverse order due to foreign keys
    conn.execute(text("DELETE FROM conversation_messages WHERE conversation_id IN (SELECT id FROM conversations WHERE tenant_id = :tenant_id)"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM conversations WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM audit_logs WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM invoices WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM files WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM vendors WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM workflow_states WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM policies WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM users WHERE tenant_id = :tenant_id"), {'tenant_id': demo_tenant_id})
    conn.execute(text("DELETE FROM tenants WHERE id = :tenant_id"), {'tenant_id': demo_tenant_id})