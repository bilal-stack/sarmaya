# Project README — Sarmaya OS (mini)

## Quick status (current)
- Alembic migrations generated (initial schema: `b105d820d4a4`).
- Seed migration present: `alembic/versions/002_seed_demo.py`.
- No-op placeholder migration: `002_seed_demo_data`.
- RLS migration present: `003_enable_rls_policies.py` (not applied until you run migrations).
- Models detected by Alembic: tenants, users, vendors, workflow_states, policies, files, invoices, audit_logs.
- Auth endpoints implemented (registration, login, me).
- Issues handled:
  - Password hashing: switched to `bcrypt_sha256` in `app/core/security.py` to avoid bcrypt 72-byte limit.
  - Pylance typing: ORM column -> str cast used in `app/api/auth.py` for static typing.

---

## How to run (development)
1. Activate venv:
   - Windows PowerShell:
     ```
     .\env\Scripts\Activate
     ```
2. Install requirements:
   ```
   pip install -r requirements.txt
   ```
3. Set env vars (example):
   - Windows (PowerShell):
     ```
     $env:DATABASE_URL="postgresql://user:pass@localhost:5432/sarmaya_os"
     $env:SECRET_KEY="your-secret"
     ```
4. Run migrations:
   ```
   cd c:\python\os
   alembic upgrade head
   ```
5. Start server:
   ```
   uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
   ```

---

## How to run the FastAPI app

To start the server locally (development):

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- `--reload` enables auto-reload on code changes (development only).
- `--host 127.0.0.1` binds to localhost.
- `--port 8000` sets the port (change as needed).

The API will be available at:  
`http://127.0.0.1:8000/api/v1`

---

## Available routes (base)
Base prefix: `/api/v1`

- GET `/`  
  - Behavior: Redirects to `/api/v1`.

- GET `/api/v1/ping`  
  - Behavior: Health check. Returns `{"pong": true}`.

### Auth (implemented)
Prefix: `/api/v1/auth`

- POST `/api/v1/auth/register`  
  - Purpose: Create a new user for a tenant.  
  - Query param: `tenant` (optional, defaults to `demo`).  
  - Body: JSON matching `UserCreate` (email, password, full_name, optional role).  
  - Response: `TokenWithUser` (access_token, token_type, user details).  
  - Notes: User is automatically logged in upon registration.

- POST `/api/v1/auth/login`  
  - Purpose: Authenticate user and return JWT with user details.  
  - Query param: `tenant` (optional, defaults to `demo`).  
  - Body: JSON `{"email": "...", "password": "..."}` (LoginIn).  
  - Response: `TokenWithUser` (access_token, token_type, user details).  
  - Notes: Token payload contains `sub` (user id) and `tenant_id`.

**Response Format (Login & Register):**
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "user": {
    "id": "uuid",
    "tenant_id": "uuid",
    "email": "user@example.com",
    "full_name": "User Name",
    "role": "user",
    "is_active": true,
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
}
```

- GET `/api/v1/auth/me`  
  - Purpose: Return current user info (requires Bearer token).  
  - Response: UserOut.

- POST `/api/v1/auth/logout`  
  - Purpose: Logout (stateless).  
  - Response: `{"success": true}`.

- PUT `/api/v1/auth/me`  
  - Purpose: Update profile.  
  - Body: JSON `{full_name?, role?}`.  
  - Response: Updated user (UserOut).

- POST `/api/v1/auth/change-password`  
  - Purpose: Change password.  
  - Body: JSON `{current_password, new_password}`.  
  - Response: `{"success": true}`.

- POST `/api/v1/auth/refresh`  
  - Purpose: Refresh JWT token.  
  - Response: New access token.  

**Query Params:**
- `tenant` (default: `demo`) — Tenant slug for register/login

---

## Invoices (`/invoices`)

### CRUD Operations

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/invoices/` | List invoices (filtered) | ✅ Bearer | - |
| GET | `/invoices/{invoice_id}` | Get invoice details | ✅ Bearer | - |
| POST | `/invoices/` | Create invoice manually | ✅ Bearer | `{invoice_number, vendor_name, invoice_date, total_amount, tax_amount?, due_date?, description?, currency?}` |
| PUT | `/invoices/{invoice_id}` | Update invoice | ✅ Bearer | `{invoice_number?, vendor_name?, invoice_date?, total_amount?, tax_amount?, due_date?, description?}` |
| DELETE | `/invoices/{invoice_id}` | Delete invoice (draft only) | ✅ Bearer | - |

**Query Params (List):**
- `status_filter` — Filter by state (draft, pending_approval, approved, rejected, paid, cancelled)
- `vendor_name` — Search by vendor name
- `start_date` — Filter from date
- `end_date` — Filter to date
- `limit` (default: 50, max: 100)
- `offset` (default: 0)

### OCR & Upload

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| POST | `/invoices/upload` | Upload PDF + OCR + create invoice | ✅ Bearer | `multipart/form-data: file` |

**Response:**
```json
{
  "success": true,
  "invoice_id": "uuid",
  "invoice_number": "INV-123",
  "vendor_name": "ABC Corp",
  "invoice_date": "2024-01-15",
  "total_amount": 50000.0,
  "tax_amount": 5000.0,
  "currency": "PKR",
  "current_state": "draft",
  "ocr_confidence": 85,
  "ocr_data": {...},
  "line_items": [
    {
      "description": "Umbrella Upper Color: Black with Inner Color: Blue-Good Quality",
      "quantity": 15.0,
      "unit_price": 1700.0,
      "amount": 30090.0,
      "product_code": "01.02.10.001.005"
    }
  ],
  "duplicate_warning": null,
  "file_id": "uuid"
}
```

### Workflow Transitions

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| POST | `/invoices/{invoice_id}/submit` | Submit for approval | ✅ Bearer | - |
| POST | `/invoices/{invoice_id}/approve` | Approve invoice | ✅ Bearer (Manager+) | - |
| POST | `/invoices/{invoice_id}/reject` | Reject invoice | ✅ Bearer | `{reason}` |
| POST | `/invoices/{invoice_id}/mark-paid` | Mark as paid | ✅ Bearer | - |

### Analytics

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/invoices/stats/summary` | Dashboard stats | ✅ Bearer | - |
| GET | `/invoices/pending` | Get pending approvals | ✅ Bearer | - |

**Stats Response:**
```json
{
  "by_status": {
    "draft": {"count": 5, "amount": 100000.0},
    "pending_approval": {"count": 2, "amount": 50000.0},
    "approved": {"count": 10, "amount": 500000.0}
  },
  "total_invoices": 17,
  "total_amount": 650000.0,
  "this_month": {
    "count": 5,
    "amount": 150000.0
  }
}
```

---

## Vendors (`/vendors`)

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/vendors/` | List vendors | ✅ Bearer | - |
| GET | `/vendors/{vendor_id}` | Get vendor details | ✅ Bearer | - |
| POST | `/vendors/` | Create vendor | ✅ Bearer | `{legal_name, email?, phone?, bank_account_name?, bank_account_number?, status?}` |
| PATCH | `/vendors/{vendor_id}/status` | Update vendor status | ✅ Bearer | `{status}` |
| DELETE | `/vendors/{vendor_id}` | Delete vendor | ✅ Bearer | - |

**Query Params (List):**
- `status_filter` — Filter by status (active, inactive, blocked, pending_verification)

---

## Files (`/files`)

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| POST | `/files/upload` | Upload file | ✅ Bearer | `multipart/form-data: file` |

**Response:**
```json
{
  "filename": "invoice.pdf",
  "size": 204800
}
```

---

## Conversation (`/conversation`)

### AI Agents

The system uses specialized AI agents for complex tasks:

**Query Agent:**
- Converts natural language to SQL queries
- Uses function calling to execute database queries
- Examples:
  - "pending invoices" → `query_invoices(status="pending_approval")`
  - "invoices below 25000" → `query_invoices(max_amount=25000)`
  - "top 5 vendors" → `get_top_vendors(limit=5)`

**Duplicate Detection Agent:**
- Multi-strategy approach:
  1. **Exact match** - Invoice number + vendor
  2. **Fuzzy match** - Amount ±5% + date ±30 days
  3. **Line item comparison** - Semantic similarity with AI

### Conversations

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/conversation/list` | List user's conversations | ✅ Bearer | - |
| GET | `/conversation/messages/{id}` | Get conversation with history | ✅ Bearer | - |
| DELETE | `/conversation/delete/{id}` | Delete conversation | ✅ Bearer | - |

**Query Params (List):**
- `limit` (default: 50)
- `offset` (default: 0)

### Chat & Query

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| POST | `/conversation/chat` | Chat with AI (persistent) | ✅ Bearer | `{message, conversation_id?}` |
| POST | `/conversation/query` | Natural language query | ✅ Bearer | `{query}` |
| POST | `/conversation/detect-duplicate` | AI duplicate detection | ✅ Bearer | `{vendor_name, invoice_number, invoice_date, total_amount}` |

**Chat Request:**
```json
{
  "message": "Show me pending invoices",
  "conversation_id": "uuid-optional"
}
```

**Chat Response:**
```json
{
  "conversation_id": "uuid",
  "message": "Here are your pending invoices...",
  "role": "assistant"
}
```

**Query Response (with Agent):**
```json
{
  "query": "Show me pending invoices",
  "ai_response": "Here are 3 pending invoices...",
  "data": [...],
  "function_called": "query_invoices",
  "sql_executed": true
}
```

**Duplicate Detection Response (Multi-Strategy):**
```json
{
  "is_duplicate": true,
  "confidence": 0.95,
  "strategy": "line_item",
  "matched_invoice_id": "uuid",
  "reasoning": "Line items match exactly, same vendor, date within 7 days"
}
```

---

## Audit (`/audit`)

**Role:** Auditor/Admin only

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/audit/logs` | Query audit logs | ✅ Bearer (Auditor+) | - |
| GET | `/audit/trail/{object_type}/{object_id}` | Get audit trail | ✅ Bearer (Auditor+) | - |
| GET | `/audit/stats/summary` | Audit statistics | ✅ Bearer (Auditor+) | - |

**Query Params (Logs):**
- `object_type` — Filter by object type (invoice, vendor, user, policy, etc.)
- `object_id` — Filter by object UUID
- `user_id` — Filter by user UUID
- `action` — Filter by action (created, updated, deleted, approved, rejected, etc.)
- `start_date` — From date
- `end_date` — To date
- `workflow_type` — Filter by workflow (invoice, purchase_order, etc.)
- `ai_assisted` — Filter by AI assistance (true/false)
- `limit` (default: 100, max: 1000)
- `offset` (default: 0)

**Logs Response:**
```json
{
  "total": 250,
  "limit": 100,
  "offset": 0,
  "logs": [
    {
      "id": "uuid",
      "timestamp": "2024-01-15T10:30:00Z",
      "user_email": "user@example.com",
      "user_role": "manager",
      "action": "approved",
      "object_type": "invoice",
      "object_id": "uuid",
      "workflow_step": "approved",
      "workflow_type": "invoice",
      "before_value": {"state": "pending_approval"},
      "after_value": {"state": "approved"},
      "changes": null,
      "file_path": "/uploads/...",
      "document_hash": "abc123...",
      "ai_assisted": false,
      "ai_provider": null,
      "ai_confidence": null,
      "ip_address": "192.168.1.1",
      "comment": null
    }
  ]
}
```

**Trail Response:**
```json
{
  "object_type": "invoice",
  "object_id": "uuid",
  "total_events": 5,
  "trail": [
    {
      "timestamp": "2024-01-15T10:00:00Z",
      "action": "uploaded",
      "user": "user@example.com",
      "role": "ap_clerk",
      "workflow_step": "draft",
      "before": null,
      "after": {"invoice_number": "INV-123", "amount": 50000.0},
      "ai_assisted": true,
      "file_hash": "abc123..."
    },
    {
      "timestamp": "2024-01-15T10:30:00Z",
      "action": "approved",
      "user": "manager@example.com",
      "role": "manager",
      "workflow_step": "approved",
      "before": {"state": "pending_approval"},
      "after": {"state": "approved"},
      "ai_assisted": false,
      "file_hash": "abc123..."
    }
  ]
}
```

**Stats Response:**
```json
{
  "total_events": 1250,
  "by_action": {
    "created": 150,
    "updated": 300,
    "approved": 400,
    "rejected": 100,
    "deleted": 50
  },
  "by_role": {
    "ap_clerk": 200,
    "manager": 600,
    "cfo": 300,
    "admin": 150
  },
  "ai_assisted_actions": 450
}
```

---

## Dashboard (`/dashboard`)

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/dashboard/stats` | Dashboard metrics | ✅ Bearer | - |

**Response:**
```json
{
  "pending_approvals": 5,
  "invoices_this_month": {
    "count": 15,
    "total_amount": 500000.0
  },
  "top_vendors": [
    {
      "vendor_name": "ABC Corp",
      "total_amount": 150000.0
    }
  ]
}
```

---

## Health Check

| Method | Endpoint | Purpose | Auth | Body |
|--------|----------|---------|------|------|
| GET | `/ping` | Health check | ❌ | - |

**Response:**
```json
{
  "pong": true
}
```

---

## Error Responses

All errors follow this format:

```json
{
  "success": false,
  "error": {
    "code": "error_code",
    "message": "Human-readable error message",
    "timestamp": "2024-01-15T10:30:00Z",
    "correlation_id": "uuid"
  }
}
```

**Common Status Codes:**
- `200` — OK
- `201` — Created
- `204` — No Content (delete)
- `400` — Bad Request
- `401` — Unauthorized
- `403` — Forbidden
- `404` — Not Found
- `500` — Internal Server Error (includes correlation_id for debugging)

---

## Authentication

All endpoints marked with `✅ Bearer` require:

```
Authorization: Bearer <jwt_token>
```

Get token from `/auth/login`:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "password123"}' \
  -G --data-urlencode "tenant=demo"
```

Response:
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer"
}
```

---

## Example Workflows

### 1. Upload Invoice & Get Approval
```bash
# 1. Login
TOKEN=$(curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -d '{"email":"user@example.com","password":"pass"}' | jq -r '.access_token')

# 2. Upload PDF
RESPONSE=$(curl -X POST http://127.0.0.1:8000/api/v1/invoices/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@invoice.pdf")

INVOICE_ID=$(echo $RESPONSE | jq -r '.invoice_id')

# 3. Submit for approval
curl -X POST http://127.0.0.1:8000/api/v1/invoices/$INVOICE_ID/submit \
  -H "Authorization: Bearer $TOKEN"

# 4. Approve (as manager)
curl -X POST http://127.0.0.1:8000/api/v1/invoices/$INVOICE_ID/approve \
  -H "Authorization: Bearer $TOKEN"
```

### 2. Query Pending Invoices with AI
```bash
curl -X POST http://127.0.0.1:8000/api/v1/conversation/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me all pending invoices over 100k"}'
```

### 3. Get Audit Trail
```bash
curl -X GET "http://127.0.0.1:8000/api/v1/audit/trail/invoice/uuid" \
  -H "Authorization: Bearer $TOKEN"
```

### 4. Chat with Context Retention
```bash
# Start new conversation
RESPONSE=$(curl -X POST http://127.0.0.1:8000/api/v1/conversation/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Show me pending invoices"}')

CONV_ID=$(echo $RESPONSE | jq -r '.conversation_id')

# Continue conversation
curl -X POST http://127.0.0.1:8000/api/v1/conversation/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"Which one is the highest amount?\", \"conversation_id\": \"$CONV_ID\"}"

# List all conversations
curl -X GET http://127.0.0.1:8000/api/v1/conversation/conversations \
  -H "Authorization: Bearer $TOKEN"
```

---

**Last Updated:** 2025-11-28
---

## Post-MVP Governance & AI Endpoints (added 2026-06)

All endpoints below require `Authorization: Bearer $TOKEN` and are tenant-scoped.

### ⚠️ Invoice Next-Action Suggestion — FRONTEND INTEGRATION PENDING

> **Integration note:** this endpoint is designed for the invoice detail view
> (a "Suggested next step" card). Integrate carefully:
> - It **suggests only** — the UI must never auto-execute the action; render it
>   as a recommendation with a button for the *existing* action endpoint
>   (validate / submit / approve / resolve-duplicate / vendor status).
> - `action` is a closed enum — handle every value or fall back to hiding the
>   card: `review_extraction | fix_missing_fields | validate |
>   submit_for_approval | resolve_duplicate | verify_vendor | approve |
>   mark_paid | revise | none`.
> - `signals` is the explainability trace — show it (e.g. collapsible "Why?")
>   per the Build Book's explainability requirement.
> - With `use_ai=true` (default) the call invokes the LLM: latency is seconds,
>   and it costs tokens — call it on demand (button / detail-view open), never
>   in a list loop. `?use_ai=false` returns instantly (rules only, no AI cost).
> - `required_role` (on `approve`) and a `sod=creator_cannot_approve` signal
>   tell the UI to disable the approve button for ineligible users.

```bash
curl -X GET "http://127.0.0.1:8000/api/v1/invoices/{invoice_id}/next-action?use_ai=true" \
  -H "Authorization: Bearer $TOKEN"
# -> { "invoice_id", "action", "confidence", "reasoning", "signals": [...],
#      "required_role", "source": "rules"|"ai", "ai_provider", "ai_model", "prompt_version" }
```

### Live Audit Mode & integrity
```bash
GET /api/v1/audit/timeline/{object_type}/{object_id}   # per-object timeline w/ plain-English reasons
GET /api/v1/audit/verify/{object_type}/{object_id}     # tamper-evidence check of the audit hash chain
GET /api/v1/audit/ai-actions?action=&status_filter=    # AI invocation trail (auditor/admin)
GET /api/v1/audit/policy-evals?object_type=&object_id= # policy evaluation snapshots (rule version + inputs)
GET /api/v1/audit/chain/{correlation_id}               # whole transaction story across record types
POST /api/v1/audit/evidence-pack/{correlation_id}      # generate sealed audit bundle (SHA-256 pack_hash)
GET  /api/v1/audit/evidence-pack/{correlation_id}      # preview the bundle without recording
GET  /api/v1/audit/evidence-packs?correlation_id=      # packs generated: when, by whom, with what seal
```

### Decision Inbox
```bash
GET  /api/v1/inbox                       # prioritized worklist; items carry sla_due_at/overdue/escalated,
                                         # breached items sort first, overdue_count in the envelope
GET  /api/v1/inbox?overdue_only=true     # the "Overdue" view (SLA-breached items only)
POST /api/v1/inbox/escalate-overdue      # escalate breached items once per state entry (audited +
                                         # notifies the escalation role); idempotent — button or cron
```

### Configuration (admin)
```bash
POST /api/v1/config/initialize-defaults                # seed default workflow + approval matrix
GET/POST/PUT/DELETE /api/v1/config/approval-policies   # approval routing matrix CRUD
POST /api/v1/config/approval-policies/simulate         # what-if a rule change (read-only)
GET  /api/v1/config/workflow/{type}/states             # workflow states + transitions + guards + sla
PUT  /api/v1/config/workflow/{type}/states/{state}/transitions
PUT  /api/v1/config/workflow/{type}/states/{state}/sla # {"hours": 48, "escalate_to": "cfo"}; versioned
GET/PUT /api/v1/config/autopilot                       # Restricted Autopilot settings
GET  /api/v1/config/versions/{config_type}/{config_key}            # config history (newest first)
GET  /api/v1/config/versions/{config_type}/{config_key}/{version}  # one snapshot
POST /api/v1/config/versions/{config_type}/{config_key}/{version}/restore  # rollback
# config_type: approval_policy (key=policy id) | workflow (key=workflow type) | autopilot (key=autopilot)
```

### Delegation (temporary approval authority)
```bash
GET  /api/v1/delegations?include_inactive=   # delegations you granted or received
POST /api/v1/delegations                     # {to_user_id, starts_at, ends_at, reason?}
POST /api/v1/delegations/{id}/revoke         # withdraw early
# The delegate borrows the delegator's role for the window. SoD still applies to the
# acting user, and approvals record acted_under_delegation + delegated_authority_of.
```

### Restricted Autopilot
```bash
GET  /api/v1/autopilot/preview          # dry run: what would be auto-approved and why
POST /api/v1/autopilot/run              # execute within configured bounds
POST /api/v1/autopilot/{invoice_id}/revert
```

### Vendors (governance gate)
```bash
GET   /api/v1/vendors/review-queue      # vendors blocking invoices, highest impact first
PATCH /api/v1/vendors/{id}/status       # activate/block (SoD: creator cannot activate own vendor)
GET   /api/v1/invoices/blocked-on-vendor
```

### Auth (revocation-aware)
```bash
POST /api/v1/auth/logout           # revokes ALL of the user's tokens (token_version bump)
POST /api/v1/auth/change-password  # revokes other sessions; returns a fresh token for the caller
POST /api/v1/auth/refresh          # rejected for revoked tokens
```

## Procure-to-Pay (added 2026-08)

The full chain: order → receive → match → approve → pay → reconcile. Each step
is a separate authority, so no single person can carry a spend from request to
settlement.

### Purchase orders (`/purchase-orders`)
```bash
GET    /api/v1/purchase-orders?state=       # list
POST   /api/v1/purchase-orders              # raise a draft (purchase_orders.create)
GET    /api/v1/purchase-orders/{id}
PUT    /api/v1/purchase-orders/{id}         # draft only
POST   /api/v1/purchase-orders/{id}/submit  # hand to an approver
POST   /api/v1/purchase-orders/{id}/approve # purchase_orders.approve; SoD: creator cannot approve own
POST   /api/v1/purchase-orders/{id}/reject  # {"reason": "..."}
POST   /api/v1/purchase-orders/{id}/issue   # send to the vendor; only then can goods be received
POST   /api/v1/purchase-orders/{id}/close
```

### Goods receipts (the delivery leg)
```bash
GET  /api/v1/purchase-orders/{id}/receipts
POST /api/v1/purchase-orders/{id}/receipts  # purchase_orders.receive — deliberately NOT held by
                                            # the roles that approve orders, or the three-way
                                            # match verifies nothing
```

### Three-way matching
```bash
GET /api/v1/invoices/{invoice_id}/match     # PO vs receipts vs invoice, per line
# line status: matched | within_tolerance | mismatched | unmatched
# A mismatch blocks approval via the three_way_match_failed transition guard.
```

### Payments (`/payments`)
```bash
GET  /api/v1/payments?state=            # list (payments.view)
GET  /api/v1/payments/payable           # approved invoices not already on an open or released run
POST /api/v1/payments                   # prepare a run: {invoice_ids[], payment_date?, currency?}
                                        # amounts come from the invoices — a run cannot pay a
                                        # different figure from the one approved
GET  /api/v1/payments/{id}
POST /api/v1/payments/{id}/submit       # hand to whoever releases
POST /api/v1/payments/{id}/release      # payments.release; SoD refuses the preparer WITH NO ADMIN
                                        # EXEMPTION (DR-017). Re-checks every line first.
POST /api/v1/payments/{id}/reject       # {"reason": "..."}
GET  /api/v1/payments/{id}/bank-file    # CSV instruction for a RELEASED run only; SHA-256 in the
                                        # X-Content-SHA256 header and recorded on first export
# The system never moves money. A treasury user uploads the file to their own bank.
```

### Bank reconciliation (`/bank-statements`)
```bash
GET  /api/v1/bank-statements                    # imported statements (bank_statements.view)
POST /api/v1/bank-statements                    # {content, source_format?, filename?}
POST /api/v1/bank-statements/upload             # multipart file; format detected from content
                                                # (camt053 | mt940 | csv) — banks name files anything
GET  /api/v1/bank-statements/{id}               # statement with its lines
GET  /api/v1/bank-statements/reconciliation     # BOTH directions, in one answer:
                                                #   instructed_not_cleared — released runs the bank
                                                #     never confirmed (vendor unpaid, nobody knows)
                                                #   cleared_not_instructed — debits no instruction
                                                #     explains; an empty `candidates` list on one of
                                                #     these is the strongest fraud signal here
GET  /api/v1/bank-statements/lines/{id}/suggestions   # ranked payments + the reasons for each
POST /api/v1/bank-statements/lines/{id}/match         # {payment_id} — a HUMAN confirms; nothing
                                                      # matches automatically. SoD refuses the person
                                                      # who released the run. The suggestion shown at
                                                      # the time is stored in the audit entry.
POST /api/v1/bank-statements/lines/{id}/unmatch       # {reason} — a wrong match is a finding
```

> **Frontend note:** a suggestion must never be rendered as a completed match.
> Show `score`, `confidence` and `reasons` next to a confirm button — an
> automatic match that is wrong marks a payment cleared that did not clear, and
> hides the unexplained debit beside it by consuming it.

**Last Updated:** 2026-08-11
