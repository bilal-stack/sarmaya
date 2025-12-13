-- ============================================
-- SARMAYA OS - COMPLETE DATABASE SCHEMA
-- Multi-Tenant with PostgreSQL RLS
-- Hybrid-Ready (RLS → Schema → Database)
-- ============================================

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================
-- 1. TENANTS TABLE (Core)
-- ============================================
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    
    -- HYBRID SUPPORT
    isolation_level VARCHAR(50) DEFAULT 'rls',
    -- Values: 'rls', 'schema', 'database'
    CONSTRAINT tenants_isolation_level_chk CHECK (isolation_level IN ('rls','schema','database')),

    schema_name VARCHAR(100),          -- For schema-per-tenant
    database_name VARCHAR(100),        -- For db-per-tenant
    connection_string TEXT,            -- For db-per-tenant
    
    -- Configuration
    logo_url VARCHAR(500),
    settings JSONB DEFAULT '{}',
    
    -- Billing
    subscription_tier VARCHAR(50) DEFAULT 'free',
    -- 'free', 'starter', 'business', 'enterprise'
    
    -- Status
    is_active BOOLEAN DEFAULT true,
    trial_ends_at TIMESTAMPTZ,
    
    -- Audit
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed demo tenant
INSERT INTO tenants (id, name, slug, isolation_level)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'Demo Company',
    'demo',
    'rls'
);


-- ============================================
-- 2. USERS TABLE
-- ============================================
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    
    role VARCHAR(50) DEFAULT 'user',
    -- 'admin', 'manager', 'approver', 'user', 'auditor'
    
    permissions JSONB DEFAULT '[]',
    -- ['invoices.approve', 'vendors.edit', etc.]
    
    is_active BOOLEAN DEFAULT true,
    last_login TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT unique_user_email_per_tenant UNIQUE(tenant_id, email)
);

-- RLS Policy
ALTER TABLE users ENABLE ROW LEVEL SECURITY;

CREATE POLICY users_tenant_isolation ON users
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_users_tenant ON users(tenant_id);
CREATE INDEX idx_users_email ON users(tenant_id, email);

-- Seed demo user (password: demo123)
INSERT INTO users (tenant_id, email, password_hash, full_name, role)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'demo@sarmaya.com',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5NU2c4.gQ3YXm',
    'Demo User',
    'admin'
);


-- ============================================
-- 3. WORKFLOW_STATES TABLE
-- ============================================
CREATE TABLE workflow_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    workflow_type VARCHAR(100) NOT NULL,
    -- 'invoice', 'purchase_order', 'grn', 'timesheet', 'leave_request', etc.
    
    state_name VARCHAR(100) NOT NULL,
    display_name VARCHAR(100),
    state_order INTEGER NOT NULL,
    
    is_initial BOOLEAN DEFAULT false,
    is_final BOOLEAN DEFAULT false,
    
    allowed_transitions JSONB DEFAULT '[]',
    -- ['approved', 'rejected', 'cancelled']
    
    color VARCHAR(20), -- For UI badges
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT unique_workflow_state UNIQUE(tenant_id, workflow_type, state_name)
);

ALTER TABLE workflow_states ENABLE ROW LEVEL SECURITY;

CREATE POLICY workflow_states_tenant_isolation ON workflow_states
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

-- Seed invoice workflow
INSERT INTO workflow_states (tenant_id, workflow_type, state_name, display_name, state_order, is_initial, allowed_transitions, color)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'draft', 'Draft', 1, true, '["pending_approval", "cancelled"]', 'gray'),
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'pending_approval', 'Pending Approval', 2, false, '["approved", "rejected"]', 'yellow'),
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'approved', 'Approved', 3, false, '["paid", "cancelled"]', 'green'),
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'rejected', 'Rejected', 4, false, '["draft"]', 'red'),
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'paid', 'Paid', 5, false, '[]', 'blue'),
    ('00000000-0000-0000-0000-000000000001', 'invoice', 'cancelled', 'Cancelled', 6, true, '[]', 'gray');


-- ============================================
-- 4. POLICIES TABLE
-- ============================================
CREATE TABLE policies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    policy_type VARCHAR(100) NOT NULL,
    -- 'approval_limit', 'document_required', 'segregation_duty', 
    -- 'validation_rule', 'budget_control', etc.
    
    policy_name VARCHAR(255) NOT NULL,
    description TEXT,
    
    rule_config JSONB NOT NULL,
    -- Flexible JSON structure for different rule types
    
    applies_to VARCHAR(100), -- 'invoice', 'purchase_order', 'all', etc.
    
    is_active BOOLEAN DEFAULT true,
    priority INTEGER DEFAULT 0, -- Higher = evaluated first
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE policies ENABLE ROW LEVEL SECURITY;

CREATE POLICY policies_tenant_isolation ON policies
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_policies_tenant_type ON policies(tenant_id, policy_type, is_active);

-- Seed approval policies
INSERT INTO policies (tenant_id, policy_type, policy_name, rule_config, applies_to)
VALUES
    (
        '00000000-0000-0000-0000-000000000001',
        'approval_limit',
        'Manager Approval Under 250k',
        '{
            "amount_threshold": 250000,
            "operator": "less_than",
            "required_role": "manager",
            "currency": "PKR"
        }'::JSONB,
        'invoice'
    ),
    (
        '00000000-0000-0000-0000-000000000001',
        'approval_limit',
        'CFO Approval 250k and Above',
        '{
            "amount_threshold": 250000,
            "operator": "greater_equal",
            "required_role": "cfo",
            "currency": "PKR"
        }'::JSONB,
        'invoice'
    );


-- ============================================
-- 5. VENDORS TABLE
-- ============================================
CREATE TABLE vendors (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    legal_name VARCHAR(255) NOT NULL,
    display_name VARCHAR(255),
    vendor_code VARCHAR(100),
    
    email VARCHAR(255),
    phone VARCHAR(50),
    address TEXT,
    
    -- Banking
    bank_account_name VARCHAR(255),
    bank_account_number VARCHAR(100),
    bank_name VARCHAR(255),
    iban VARCHAR(50),
    swift_code VARCHAR(20),
    
    -- Tax
    tax_id VARCHAR(100),
    tax_certificate_url VARCHAR(500),
    
    -- Status & Risk
    status VARCHAR(50) DEFAULT 'active',
    -- 'active', 'inactive', 'blocked', 'pending_verification'
    
    risk_score INTEGER DEFAULT 0, -- 0-100
    risk_flags JSONB DEFAULT '[]',
    
    -- Metadata
    metadata JSONB DEFAULT '{}',
    notes TEXT,
    
    -- Audit
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT unique_vendor_name UNIQUE(tenant_id, legal_name)
);

ALTER TABLE vendors ENABLE ROW LEVEL SECURITY;

CREATE POLICY vendors_tenant_isolation ON vendors
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_vendors_tenant ON vendors(tenant_id, status);
CREATE INDEX idx_vendors_name ON vendors(tenant_id, legal_name);


-- ============================================
-- 6. FILES TABLE
-- ============================================
CREATE TABLE files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    original_filename VARCHAR(255) NOT NULL,
    stored_filename VARCHAR(255) NOT NULL, -- UUID-based
    file_path VARCHAR(500) NOT NULL,
    
    mime_type VARCHAR(100),
    file_size BIGINT, -- bytes
    file_hash VARCHAR(64), -- SHA-256
    
    -- Link to any object
    object_type VARCHAR(50),
    -- 'invoice', 'vendor', 'contract', 'resume', 'po', etc.
    object_id UUID,
    
    -- Storage location
    storage_type VARCHAR(50) DEFAULT 'local',
    -- 'local', 's3', 'azure', 'gcs'
    
    -- Metadata
    metadata JSONB DEFAULT '{}',
    
    -- Audit
    uploaded_by UUID REFERENCES users(id),
    uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE files ENABLE ROW LEVEL SECURITY;

CREATE POLICY files_tenant_isolation ON files
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_files_tenant ON files(tenant_id);
CREATE INDEX idx_files_object ON files(tenant_id, object_type, object_id);


-- ============================================
-- 7. INVOICES TABLE (MVP Core)
-- ============================================
CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    -- Basic Info
    invoice_number VARCHAR(100) NOT NULL,
    
    -- Vendor (flexible for MVP)
    vendor_id UUID REFERENCES vendors(id),
    vendor_name VARCHAR(255) NOT NULL, -- Direct field for MVP
    
    -- Dates
    invoice_date DATE NOT NULL,
    due_date DATE,
    
    -- Amounts
    currency VARCHAR(3) DEFAULT 'PKR',
    subtotal_amount DECIMAL(15,2),
    tax_amount DECIMAL(15,2),
    total_amount DECIMAL(15,2) NOT NULL,
    
    description TEXT,
    
    -- Workflow
    current_state VARCHAR(50) DEFAULT 'draft',
    
    -- OCR
    ocr_confidence INTEGER, -- 0-100
    ocr_extracted_data JSONB,
    
    -- ✅ ADD: Line items
    line_items JSONB DEFAULT '[]',
    -- Structure: [{"description": "...", "quantity": 10, "unit_price": 100, "amount": 1000, "product_code": "..."}]
    
    -- File
    pdf_file_id UUID REFERENCES files(id),
    
    -- Approval
    approved_by UUID REFERENCES users(id),
    approved_at TIMESTAMPTZ,
    rejection_reason TEXT,
    
    -- Future links (NULL for MVP)
    purchase_order_id UUID, -- For post-MVP
    grn_id UUID, -- For post-MVP
    payment_batch_id UUID, -- For post-MVP
    
    -- GL Posting (future)
    gl_account_code VARCHAR(50),
    cost_center VARCHAR(50),
    
    -- Metadata
    metadata JSONB DEFAULT '{}',
    tags JSONB DEFAULT '[]',
    
    -- Audit
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT unique_invoice_number UNIQUE(tenant_id, vendor_name, invoice_number)
);

ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;

CREATE POLICY invoices_tenant_isolation ON invoices
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_invoices_tenant_state ON invoices(tenant_id, current_state);
CREATE INDEX idx_invoices_tenant_date ON invoices(tenant_id, invoice_date DESC);
CREATE INDEX idx_invoices_vendor ON invoices(tenant_id, vendor_name);
CREATE INDEX idx_invoices_number ON invoices(tenant_id, invoice_number);


-- ============================================
-- 8. AUDIT_LOGS TABLE
-- ============================================
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    -- Who
    user_id UUID REFERENCES users(id),
    user_email VARCHAR(255),
    user_role VARCHAR(50),
    
    -- What
    object_type VARCHAR(50) NOT NULL,
    -- 'invoice', 'vendor', 'user', 'policy', etc.
    object_id UUID NOT NULL,
    
    action VARCHAR(100) NOT NULL,
    -- 'created', 'updated', 'deleted', 'approved', 'rejected',
    -- 'state_changed', 'uploaded', etc.
    
    -- Details
    before_value JSONB,
    after_value JSONB,
    changes JSONB, -- Specific field changes
    
    comment TEXT,
    
    -- Context
    metadata JSONB DEFAULT '{}',
    ip_address INET,
    user_agent TEXT,
    
    -- When
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY audit_logs_tenant_isolation ON audit_logs
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));

CREATE INDEX idx_audit_logs_tenant ON audit_logs(tenant_id);
CREATE INDEX idx_audit_logs_object ON audit_logs(tenant_id, object_type, object_id, timestamp DESC);
CREATE INDEX idx_audit_logs_user ON audit_logs(tenant_id, user_id, timestamp DESC);
CREATE INDEX idx_audit_logs_timestamp ON audit_logs(tenant_id, timestamp DESC);


-- ============================================
-- 9. PURCHASE_ORDERS TABLE (Future)
-- ============================================
CREATE TABLE purchase_orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    po_number VARCHAR(100) NOT NULL,
    vendor_id UUID REFERENCES vendors(id),
    
    po_date DATE NOT NULL,
    expected_delivery_date DATE,
    
    currency VARCHAR(3) DEFAULT 'PKR',
    total_amount DECIMAL(15,2) NOT NULL,
    
    current_state VARCHAR(50) DEFAULT 'draft',
    
    -- Links
    pr_id UUID, -- Purchase requisition (future)
    rfq_id UUID, -- RFQ (future)
    
    metadata JSONB DEFAULT '{}',
    
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(tenant_id, po_number)
);

ALTER TABLE purchase_orders ENABLE ROW LEVEL SECURITY;

CREATE POLICY purchase_orders_tenant_isolation ON purchase_orders
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));


-- ============================================
-- 10. ITEMS TABLE (Future Supply Chain)
-- ============================================
CREATE TABLE items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    
    sku VARCHAR(100) NOT NULL,
    item_name VARCHAR(255) NOT NULL,
    description TEXT,
    
    category VARCHAR(100),
    unit_of_measure VARCHAR(50),
    
    is_active BOOLEAN DEFAULT true,
    
    metadata JSONB DEFAULT '{}',
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(tenant_id, sku)
);

ALTER TABLE items ENABLE ROW LEVEL SECURITY;

CREATE POLICY items_tenant_isolation ON items
    USING (tenant_id::TEXT = current_setting('app.current_tenant_id', true));


-- ============================================
-- TRIGGERS FOR UPDATED_AT
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_tenants_updated_at BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_vendors_updated_at BEFORE UPDATE ON vendors
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_invoices_updated_at BEFORE UPDATE ON invoices
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_policies_updated_at BEFORE UPDATE ON policies
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- ============================================
-- VIEWS FOR COMMON QUERIES
-- ============================================

-- Invoice summary view
CREATE OR REPLACE VIEW invoice_summary AS
SELECT 
    i.id,
    i.tenant_id,
    i.invoice_number,
    i.vendor_name,
    i.invoice_date,
    i.total_amount,
    i.current_state,
    i.created_at,
    u.full_name as created_by_name,
    a.full_name as approved_by_name,
    f.file_path as pdf_path
FROM invoices i
LEFT JOIN users u ON i.created_by = u.id
LEFT JOIN users a ON i.approved_by = a.id
LEFT JOIN files f ON i.pdf_file_id = f.id;


-- ============================================
-- GRANT PERMISSIONS
-- ============================================
-- (Adjust based on your PostgreSQL user)

-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO sarmaya_user;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO sarmaya_user;


-- ============================================
-- VERIFICATION QUERIES
-- ============================================

-- Check RLS is enabled
SELECT schemaname, tablename, rowsecurity 
FROM pg_tables 
WHERE schemaname = 'public' AND rowsecurity = true;

-- Check policies
SELECT schemaname, tablename, policyname 
FROM pg_policies 
WHERE schemaname = 'public';

-- Test RLS
-- SET app.current_tenant_id = '00000000-0000-0000-0000-000000000001';
-- SELECT * FROM users; -- Should return only demo company users

-- Reset
-- RESET app.current_tenant_id;

/* Added: safety CHECK for tenants.isolation_level and index on slug.
   Using ALTER TABLE / CREATE INDEX so statements are non-destructive if you already created the table. */
ALTER TABLE IF EXISTS tenants
    ADD CONSTRAINT IF NOT EXISTS tenants_isolation_level_chk
    CHECK (isolation_level IN ('rls','schema','database'));

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants (slug);
