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
  - Purpose: Self-service signup into an existing tenant.  
  - **Disabled unless `ALLOW_SELF_REGISTRATION` is set** — otherwise 403. Even at
    the default clerk role a stranger can create vendors, raise invoices, prepare
    payment runs and import bank statements. Administrators create accounts with
    `POST /api/v1/users` instead.  
  - Query param: `tenant` (optional, defaults to `demo`).  
  - Body: `{email, password, full_name?}`. **No role** — the new account is
    always the default clerk. The body used to accept one, so an unauthenticated
    `{"role": "admin"}` against any tenant slug returned an administrator's
    token.  
  - Response: `TokenWithUser` (access_token, token_type, user details).

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

Items come from every module, not just invoices. Seven collectors report: pending
invoices, requisitions awaiting approval, closed tenders awaiting award, POs
awaiting approval, payment runs awaiting release, open vendor bank changes, and
unmatched bank debits. Each filters by permission *and* by segregation of duties,
so nothing appears that the caller would be refused on.

**Item shape** (neutral — it is no longer invoice-specific):

| field | meaning |
|---|---|
| `object_type` / `object_id` | what it is and which one — `invoice`, `requisition`, `rfq`, `purchase_order`, `payment`, `vendor_bank_change`, `bank_statement_line` |
| `reference` / `subtitle` | the number a person would quote, and whatever identifies it (vendor, title, counterparty) |
| `category` | one of nine, drives the badge |
| `work_item_type` | the Build Book's grouping: `approval`, `exception`, `review`, `reconciliation`, `admin` |
| `detail_url` / `timeline_url` | where to act, and the Live Audit Mode link — **build neither client-side**, they differ per module |
| `priority` | lower sorts first; `0` = a debit nothing explains, `1` = an open bank change |

The envelope adds `by_work_item_type` alongside `counts`.

> Frontend note: items were once `{invoice_id, invoice_number, vendor_name}` and
> the client built `/ai-tools/invoices/{id}` itself. Any client still doing that
> sends the reader to an invoice page for a payment run.

### Notifications and SLAs

Every module tells whoever can act when work arrives — requisition submitted, PO
submitted, tender closed, payment awaiting release — resolved by *permission*,
excluding whoever raised it. Email delivery is opt-in (`SMTP_ENABLED`).

Every workflow's waiting state carries an SLA. RFQ `closed` was the exception
and now escalates to manager after 48h: quoting has ended, the vendors are
waiting, and nothing else chases it.

### Notification queue
```bash
POST /api/v1/notifications/dispatch?limit=       # deliver everything due
GET  /api/v1/notifications/queue?status=         # pending | sent | failed
GET  /api/v1/notifications/queue/summary         # counts by status
POST /api/v1/notifications/queue/retry-failed    # requeue after fixing the cause
```

Notifications are **queued in the same transaction as the action** that produced
them and delivered afterwards, so no request waits on a mail server and a
rolled-back action sends nothing. Needs `workflow.manage`.

Run the drain on a schedule — every minute is fine, and it is cheap when empty:

```bash
python -m scripts.dispatch_notifications
```

Delivery is opt-in (`SMTP_ENABLED`). While it is off, messages are **held**
untouched rather than attempted, so switching it on later sends the backlog
instead of finding it expired.

### Change Watchlist
```bash
GET  /api/v1/watchlist?open_only=&category=   # alerts newest first + open_count
POST /api/v1/watchlist/{alert_id}/acknowledge # {note?} — records that somebody looked
```

Raised by the three changes the Build Book names — vendor bank changes, vendor
master data edits, and approval policy create/update/delete. Each moves money or
moves the rules **without touching an invoice**, so nothing else surfaces them.

Needs `watchlist.view` (admin, CFO, auditor). The clerk and manager who make
these changes deliberately do not hold it — they are the subjects of the
watchlist, not its audience. Whoever caused a change cannot acknowledge its own
alert. Account numbers are masked here as everywhere else.

> Email delivery is opt-in: set `SMTP_ENABLED=true`. Notifications are sent
> synchronously inside the request, so an unconfigured mail server would
> otherwise add a socket timeout to every vendor edit.

### Deleting records

`DELETE /vendors/{id}`, `DELETE /invoices/{id}` and
`DELETE /config/approval-policies/{id}` **withdraw** rather than destroy, and
each takes a body:

```json
{ "reason": "at least 10 characters explaining why" }
```

Withdrawn rows vanish from every query but stay resolvable, so the audit entry
describing the deletion still points at something. A reason is required because
a deletion is the one event nobody can reconstruct from what is left.

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

### Vendor bank changes (`/vendors`) — the AP fraud control

> **Account numbers are masked unless you hold `vendors.view_bank_details`.**
> Held by admin, AP clerk, manager and CFO — the roles that act on payment
> details — and deliberately not by the auditor. Masked values keep the last
> four (`••••6702`) so an account stays identifiable, and the response carries
> `bank_details_visible: false` so a client can say why. This applies to the
> vendor record, both sides of a bank change, and the destination on every
> payment line.

```bash
POST /api/v1/vendors/{id}/bank-change   # {reason, iban?, bank_account_number?, ...}
                                        # Bank fields are REFUSED by PATCH /vendors/{id};
                                        # they only change through here. Records the old
                                        # values beside the new. Payments to this vendor
                                        # are held from now until it is resolved.
GET  /api/v1/vendors/bank-changes?vendor_id=&state=
POST /api/v1/vendors/bank-changes/{id}/approve   # vendors.approve_bank_change, which the
                                                 # clerk who maintains vendors does NOT hold.
                                                 # Refuses the requester, no admin exemption.
                                                 # Starts the cooling period; changes nothing yet.
POST /api/v1/vendors/bank-changes/{id}/apply     # writes it to the vendor, once the clock has run
POST /api/v1/vendors/bank-changes/{id}/reject    # {"reason": "..."}
POST /api/v1/vendors/bank-changes/{id}/cancel    # {"reason": "..."}
```

The control does not end when the change is applied. Build Book line 193: whoever
requested or applied a change cannot **release** the first payment to that vendor
afterwards — the second signature at approval means nothing if it is the same
person again at the money. Measured against releases, so a run that was prepared
and rejected does not discharge it, and the restriction lifts once one payment
has gone out with somebody else's signature. Preparation is unaffected.


> Build Book A1 control. The most common invoice fraud is a real invoice paid
> to a changed account: every downstream control passes because nothing
> downstream is wrong. Cooling period defaults to 24h
> (`VENDOR_BANK_CHANGE_COOLING_HOURS`), and while a change is open payments are
> held to **either** account — during a dispute neither destination is known
> to be right.

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
POST /api/v1/auth/register?tenant= # DISABLED unless ALLOW_SELF_REGISTRATION is set. The body
                                   # cannot carry a role: the new account is always the default
                                   # clerk. It previously accepted one, so an unauthenticated
                                   # {"role":"admin"} against any tenant slug returned an
                                   # administrator's token.
```

### Users (accounts are granted, not claimed)
```bash
GET   /api/v1/users?active_only=   # directory for the delegate picker; requires users.view
POST  /api/v1/users                # create an account in YOUR tenant; requires users.manage.
                                   # {email, password (12+ chars), full_name?, role?} — audited
                                   # with the role granted. This is how accounts are made now
                                   # that self-registration is closed by default.
PATCH /api/v1/users/{id}/role      # requires users.manage; never self-service; revokes the
                                   # target's existing tokens
```

### Org units and scopes (`/org-units`) — what a role may act *on*

A role says what somebody may do. A scope says what they may do it to. Assign a
unit and they see that unit **and everything beneath it**.

Two defaults matter more than the endpoints:

* **No scope means no restriction.** A user with nothing assigned sees the whole
  tenant. So `GET .../scopes` returning `[]` means *unrestricted*, not "nothing" —
  and removing somebody's last scope **widens** their access rather than
  narrowing it. The revoke audit entry says so explicitly.
* **A record with no `org_unit_id` stays visible to everyone**, so existing data
  does not vanish the moment one person is scoped.

Scoping is applied as a global query filter next to the tenant filter, so it
holds for queries written in modules that know nothing about it. Invoices,
requisitions and purchase orders carry a unit. Payments deliberately do not — a
payment run settles invoices across units.

```bash
GET    /api/v1/org-units                              # the org chart; requires users.view
POST   /api/v1/org-units                              # {code, name, unit_type, parent_id?}
                                                      # unit_type: business_unit | location |
                                                      # department | cost_center | project
                                                      # requires users.manage; code unique per tenant
GET    /api/v1/org-units/users/{id}/scopes            # [] means unrestricted, not "none"
POST   /api/v1/org-units/users/{id}/scopes            # {org_unit_id} — grants the unit and its
                                                      # descendants; audited under the administrator
DELETE /api/v1/org-units/users/{id}/scopes/{unit_id}  # removing the last one widens access
```

### Exports - getting it out as a file

Until this existed the only thing that left the system as a file was the bank
payment instruction, which is an odd gap in a product whose argument is
evidence.

Three formats, for three readers. **CSV** carries one table, because a CSV
holding several tables separated by blank lines opens tidily in Excel and is
unparseable by everything else. **HTML** carries the whole report, is
self-contained (nothing is fetched - an archive that phones home is not an
archive), and prints to PDF from any browser; that is how you get a PDF without
putting a rendering library in the path that produces audit evidence. **JSON**
is the canonical bundle.

Two details that matter more than the formats:

* **Spreadsheet safety.** Any cell opening `=`, `+`, `-` or `@` is prefixed
  with an apostrophe. A vendor named `=HYPERLINK("http://...")` is a live
  formula in Excel, in a file the finance team opened precisely because it came
  from their own finance system. Excel strips the apostrophe on display, so the
  cell still reads correctly.
* **The seal covers the JSON, not the page.** An evidence pack's `pack_hash` is
  computed over the canonical bundle. Re-hashing a rendered document gives a
  different number, so the HTML export embeds the exact bundle it was rendered
  from in a `<script type="application/json" id="canonical-bundle">` block.
  Extract it, SHA-256 it, and it equals the printed hash - which is the
  difference between evidence and decoration.

```bash
GET /api/v1/dashboard/{report}/export?format=csv&table=&days=
    # report: control-room | bottlenecks | exceptions | policy-overrides |
    #         evidence | reconciliation-health | autopilot-health
    # format: csv (one table) | html (whole report) | json
    # table:  which table for csv; defaults to the report's largest
    # Gated exactly like the report's own endpoint - the file can never be a
    # way around the permission on the screen.

GET /api/v1/audit/evidence-pack/{correlation_id}/export?format=html|json
    # requires audit.view. Returns X-Pack-SHA256 alongside the file.
    # A GET, and deliberately a *preview* - it does not seal a new pack, so
    # refreshing a download never adds to the register of generated packs.
```

### System health (`/system`) — the admin console's error monitor

Answers "is anything silently not running". A background job that throws is
already in the logs; a background job that **stopped** produces nothing at all,
so every scheduled run writes a heartbeat and this reads the age of the last one.

`status` is `ok | degraded | down`, and the top-level value is the worst of the
components — an average would hide an outage behind three greens. A job that has
never run reads `down` rather than `unknown`, deliberately. Email delivery being
off appears under `notes` as configuration, not as a fault.

```bash
GET /api/v1/system/health   # requires audit.view
                            # { status, checked_at,
                            #   jobs: [{job, status, last_run_at, minutes_since,
                            #           expected_every_minutes, last_error, detail}],
                            #   notifications: {pending, failed, stuck, delivery_enabled, ...},
                            #   ai: {total, errors, schema_rejections, ...},
                            #   notes: [...] }
```

Both jobs need a scheduler in production, and this screen is where a missing one
becomes visible:

```bash
python -m scripts.dispatch_notifications   # every minute
python -m scripts.run_workflow_timers      # hourly
```

## Procure-to-Pay (added 2026-08)

The full chain: order → receive → match → approve → pay → reconcile. Each step
is a separate authority, so no single person can carry a spend from request to
settlement.

### Requisitions (`/requisitions`) — the request that justifies an order

```bash
GET  /api/v1/requisitions?state=       # list (requisitions.view)
POST /api/v1/requisitions              # {title, justification, budget_code?, department?,
                                       #  needed_by?, lines[{description, quantity,
                                       #  estimated_unit_price}]}
                                       # No vendor: naming one here would let the requester
                                       # pre-select the winner before anyone has quoted.
                                       # Mints the correlation id the whole chain inherits.
GET  /api/v1/requisitions/{id}
POST /api/v1/requisitions/{id}/submit  # guards: needs lines and a real justification
POST /api/v1/requisitions/{id}/approve # requisitions.approve; SoD refuses the requester;
                                       # the approval matrix's amount limits apply. The
                                       # approved estimate is the ceiling for any order.
POST /api/v1/requisitions/{id}/reject  # {"reason": "..."}
POST /api/v1/requisitions/{id}/cancel  # {"reason": "..."}
```

### Sourcing (`/rfqs`) — tender, quotes, award

```bash
GET  /api/v1/rfqs?state=               # list
POST /api/v1/rfqs                      # {requisition_id, title?, closes_at?, vendor_ids[]}
                                       # requisition must be APPROVED; inherits its chain
GET  /api/v1/rfqs/{id}
POST /api/v1/rfqs/{id}/vendors         # {vendor_id} — blocked vendors refused
POST /api/v1/rfqs/{id}/issue           # needs >= 2 invited vendors; one quote is not a
                                       # comparison. For a single source, raise the PO direct.
POST /api/v1/rfqs/{id}/quotes          # {vendor_id, lead_time_days?, payment_terms?,
                                       #  is_compliant?, lines[]} — invited vendors only,
                                       #  one quote each, captured_by records who typed it
POST /api/v1/rfqs/{id}/close           # QUOTES LOCK HERE. Nothing may be added or altered
                                       # afterwards, by anyone. Snapshots the field.
GET  /api/v1/rfqs/{id}/comparison      # side by side + lowest COMPLIANT quote, who was
                                       # invited and never answered, and whether the market
                                       # came in over the approved estimate
POST /api/v1/rfqs/{id}/award           # {quote_id, justification?} — sourcing.award, which
                                       # the buyer who ran the tender does NOT hold.
                                       # Anything but the lowest compliant quote requires a
                                       # written reason; stored with the figure it beat.
POST /api/v1/rfqs/{id}/convert         # raise the PO. Refused above the approved estimate;
                                       # marks the requisition converted so one approval
                                       # cannot cover two orders. Carries the chain through.
POST /api/v1/rfqs/{id}/cancel          # {"reason": "..."}
```

> **Roles as shipped:** clerk raises the need and runs the tender; manager/CFO
> approve the need and award. No single person can carry a purchase from "I want
> this" to "this vendor wins".

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
## Supply Chain (Variant D, added 2026-08)

Receiving existed to serve three-way matching. This is the other half: an item
master, locations, and a balance for goods to arrive into.

**Stock is a ledger, not a number.** `stock_movements` is append-only and
signed; the balance is its sum, kept as an aggregate for speed and checkable
against the ledger at any time. That is what lets the system answer "why is it
37 when it should be 40" — a question a stored quantity cannot answer.

Two rules are enforced at the single writer, and both are silent when missing:
**stock cannot go negative** (a shelf cannot hold less than nothing, so this is
always a data error), and the balance row is **locked before it is changed**
(two concurrent receipts would otherwise each read the same number and one
would vanish).

### Items, locations and stock (`/inventory`)

```bash
GET   /api/v1/inventory/items?active_only=&category=   # requires inventory.view
POST  /api/v1/inventory/items                          # requires inventory.manage_items
                                                       # {sku, name, uom?, is_stocked?,
                                                       #  reorder_point?, standard_cost?}
                                                       # A non-stocked item is bought but
                                                       # never held; it can be ordered and
                                                       # received and never touches a balance.
PATCH /api/v1/inventory/items/{id}                     # standard_cost changes are audited
                                                       # with before/after: it decides which
                                                       # adjustments need two approvers
GET   /api/v1/inventory/locations
POST  /api/v1/inventory/locations                      # {code, name, is_receiving_bay?,
                                                       #  is_quarantine?} — never both
GET   /api/v1/inventory/stock?location_id=&include_zero=
                                                       # on hand, valued, with reorder flags
GET   /api/v1/inventory/movements?item_id=&location_id=&limit=
                                                       # the ledger: why a balance is what it is
GET   /api/v1/inventory/reconcile                      # where the stored balance disagrees with
                                                       # the ledger. Should always be empty; a
                                                       # non-empty answer is a bug, so the
                                                       # discrepancies are reported and never
                                                       # silently corrected
```

### Adjustments — the governed record

The only way stock changes with nothing physical behind it, which makes writing
stock off how a theft gets covered up. So it carries the same shape an invoice
does: a workflow, a value threshold, and two signatures above the limit.

`inventory.adjust` and `inventory.approve_adjustment` are **separate
permissions** so no arrangement of roles can collapse the separation. The SoD
rule has **no admin exemption**, unlike the invoice rules — this control exists
precisely for the person with the most access.

```bash
GET  /api/v1/inventory/adjustments?state=&location_id=
GET  /api/v1/inventory/adjustments/{id}                # with lines
POST /api/v1/inventory/adjustments                     # {location_id, reason_code, lines:[
                                                       #  {item_id, quantity_change, note?}]}
                                                       # signed: negative writes off, positive on
POST /api/v1/inventory/adjustments/{id}/submit         # fixes the threshold decision here, and
                                                       # stores it — recomputing at approval
                                                       # would let an edited cost route a large
                                                       # write-off past the second signature
POST /api/v1/inventory/adjustments/{id}/approve        # ONE route for both signatures. The
                                                       # server decides whether a call is the
                                                       # first or the second; a client that
                                                       # could choose would be able to supply
                                                       # the second alone. Posts the ledger once
                                                       # every required signature is present.
                                                       # 409 if it would overdraw stock.
POST /api/v1/inventory/adjustments/{id}/reject         # {reason} — required
POST /api/v1/inventory/adjustments/{id}/cancel         # never once posted: reversing posted
                                                       # stock means an opposing adjustment
```

### Quality checks, putaway and returns

A failed check does **not** undo the receipt — what arrived still arrived. The
rejected quantity moves to quarantine, where it cannot be picked while somebody
decides whether it goes back.

```bash
POST /api/v1/inventory/receipt-lines/{id}/quality-check
     # {quantity_accepted, quantity_rejected, reason_code?, notes?}
     # A rejection REQUIRES a reason code and a note. "27 rejected" with no
     # reason is unusable when somebody later asks which supplier keeps sending
     # damaged goods — which is the whole point of reason codes.
POST /api/v1/inventory/receipt-lines/{id}/putaway       # {destination_id, quantity}
GET  /api/v1/inventory/receipt-lines/{id}/exception     # why a delivery did not match its
                                                        # order. The computed half (short,
                                                        # over, late, rejected) is always
                                                        # present; the AI explanation is added
                                                        # when available, and its absence is
                                                        # stated rather than left blank.
                                                        # A suggested reason code outside the
                                                        # vocabulary is discarded, never stored.
GET  /api/v1/inventory/uninspected                      # delivered but never checked

GET  /api/v1/inventory/returns?state=&vendor_id=
POST /api/v1/inventory/returns                          # {vendor_id, location_id, reason_code,
                                                        #  lines:[{item_id, quantity}]}
                                                        # vendor_attributable is derived from the
                                                        # reason and then FIXED, so a scorecard
                                                        # cannot rewrite last quarter
POST /api/v1/inventory/returns/{id}/{action}
     # submit | approve | dispatch | reject | cancel | credit
     # Stock leaves on DISPATCH, not on approval — the goods are still on the
     # premises until the lorry goes. `credit` requires the vendor's credit note
     # reference, or there is nothing to reconcile against later.
```

### Supply chain reports

```bash
GET /api/v1/dashboard/stock-accuracy?days=          # write-offs and write-ons are NOT netted:
                                                    # two adjustments that cancel out are two
                                                    # discrepancies, not a tidy warehouse.
                                                    # Loss and theft are called out separately.
GET /api/v1/dashboard/supplier-performance?days=    # on time, in full, undamaged — three
                                                    # questions, deliberately not averaged into
                                                    # one score. A delivery with no promised
                                                    # date is UNMEASURABLE, not punctual.
GET /api/v1/dashboard/receipt-to-invoice?days=      # goods received with no invoice yet: a
                                                    # liability the ledger does not show, and
                                                    # the accrual an auditor asks about
```

All three export like every other report (`/export?format=csv|html|json`), and
both new workflows (`inventory_adjustment`, `vendor_return`) reach the Decision
Inbox with SLAs and escalation.

## HR (Variant C, added 2026-08)

**An employee is not a user.** A user is a login; an employee is a person the
company employs. Creating an employee never creates an account, and linking one
is a separate, audited action that grants nothing by itself — the role on the
account still decides what it may do. Most of a real workforce has no login at
all, which is why `user_id` is nullable and expected to stay that way.

**Pay, national IDs and bank details are masked on the way out.** A caller
without `hr.view_compensation` gets `"restricted"` for salary and `••••67-1`
for identifiers. The keys are identical in both shapes, so nothing has to
branch. The values are stored in full — payroll variance is arithmetic on real
numbers — and the masking happens at the service boundary, the same place
vendor bank details already do it. Pay is deliberately kept out of the audit
trail too, because `audit.view` reaches a wider audience than
`hr.view_compensation`.

### People (`/hr/employees`)

```bash
GET  /api/v1/hr/employees?org_unit_id=&include_left=   # requires hr.view
GET  /api/v1/hr/employees/{id}
POST /api/v1/hr/employees                              # requires hr.manage_employees
                                                       # base_salary is refused unless the
                                                       # caller may see compensation
POST /api/v1/hr/employees/{id}/user                    # {user_id} — or null to detach.
                                                       # One account belongs to one employee,
                                                       # or every approval it gives is ambiguous
POST /api/v1/hr/employees/{id}/status                  # {status, end_date?}
                                                       # `left` REQUIRES an end date: without one
                                                       # "how many did we employ in March" has
                                                       # no answer. Leaving is never a deletion
```

### Hiring (`/hr/headcount`)

A hire is agreed once and paid for years, so a request states its annual cost
before anybody approves it. `filled` is terminal and separate from `approved`:
an approved request already hired against must not authorise a second hire.

```bash
GET  /api/v1/hr/headcount?state=&org_unit_id=
POST /api/v1/hr/headcount            # {job_title, annual_cost, positions?, ...}
                                     # requires hr.request_headcount; a cost of zero is refused
POST /api/v1/hr/headcount/{id}/submit    # a justification is required here, not at creation
POST /api/v1/hr/headcount/{id}/approve   # requires hr.approve_headcount — a SEPARATE grant,
                                         # so a manager asks and the CFO decides the cost
POST /api/v1/hr/headcount/{id}/reject    # {reason}
POST /api/v1/hr/headcount/{id}/fill      # {employee_id}. A sensitive role refuses anyone
                                         # whose background check is not cleared — checked
                                         # here because vetting happens to a person, and at
                                         # approval time there was no person yet
POST /api/v1/hr/headcount/{id}/cancel
GET  /api/v1/hr/headcount-plan           # plan vs actual. Approved-and-unfilled is the number
                                         # that matters: committed cost the budget carries and
                                         # the headcount does not show
```

### Onboarding and offboarding

One engine for both, and the offboarding half is the one with teeth: an
unfinished onboarding task is a starter waiting for a laptop, while an
unfinished offboarding task is somebody who left and can still sign in.

```bash
GET  /api/v1/hr/employees/{id}/tasks?flow=
POST /api/v1/hr/employees/{id}/checklist   # {flow: onboarding|offboarding, extra_tasks?}
                                           # refuses a second checklist for the same flow
POST /api/v1/hr/tasks/{task_id}/status     # {status, note?}
                                           # 'not_applicable' and 'blocked' REQUIRE a note —
                                           # without one they cannot be told apart from a task
                                           # somebody quietly dropped
GET  /api/v1/hr/outstanding-access         # who has left and can still sign in
GET  /api/v1/hr/onboarding-completion?flow=   # by owning team, because "IT has four open
                                              # tasks" is actionable and "60% done" is not
```

### Pay changes (`/hr/payroll-changes`)

The clearest SoD surface in the product, and it refuses **three** conflicts:
you cannot raise your own, you cannot approve your own, and you cannot approve
a rise for **your own manager** — because two managers approving each other's
rises pass every check that looks at one record at a time.

```bash
GET  /api/v1/hr/payroll-changes?state=&employee_id=
     # the SIZE of each change is always shown — an approver has to know whether
     # they are signing 2% or 40% — while the salaries need hr.view_compensation
POST /api/v1/hr/payroll-changes          # {employee_id, new_salary, reason_code,
                                         #  effective_date} — requires hr.request_payroll_change
POST /api/v1/hr/payroll-changes/{id}/submit
POST /api/v1/hr/payroll-changes/{id}/approve
     # requires hr.approve_payroll_change, a separate grant: a manager requests
     # and never approves; the CFO approves and never requests. Applies the new
     # salary in the SAME transaction — approved-but-unapplied would be paperwork
     # saying somebody got a rise that never reached their pay
POST /api/v1/hr/payroll-changes/{id}/reject   # {reason}
POST /api/v1/hr/payroll-changes/{id}/cancel   # never once applied: reverse it with another
                                              # change, so the record shows both
```

### Expenses (`/hr/expenses`)

A claim is a payment request with a person attached, so it is controlled like
an invoice. A receipt is required for anything over 1,000 or in travel,
accommodation, entertainment or equipment — checked at **submission**, so the
claimant finds out while they still have the receipt.

```bash
GET  /api/v1/hr/expenses?state=&employee_id=
     # somebody who can only claim sees their own: an expense list is a record of
     # where people went and what they bought
POST /api/v1/hr/expenses               # {employee_id, category, total_amount, incurred_date}
POST /api/v1/hr/expenses/{id}/submit   # refused without a receipt where one is required
POST /api/v1/hr/expenses/{id}/approve  # {override_reason?} — waiving the receipt rule needs a
                                       # written reason, recorded in policy_override_reason and
                                       # reported alongside every other override.
                                       # NOBODY approves their own, admins included, and not a
                                       # claim somebody else keyed in on their behalf
POST /api/v1/hr/expenses/{id}/reject   # {reason}
POST /api/v1/hr/expenses/{id}/pay
POST /api/v1/hr/expenses/{id}/cancel
```

### HR reports

Gated on `hr.view` rather than the dashboard permission: the other dashboards
aggregate records anybody with `invoices.view` could open individually, while
these aggregate people.

```bash
GET /api/v1/dashboard/hiring-pipeline?days=      # time to hire, measured from APPROVAL —
                                                 # the wait before that is a budget decision.
                                                 # Open roles are reported separately, because
                                                 # an average over completed hires flatters
                                                 # every pipeline
GET /api/v1/dashboard/payroll-variance?days=     # movement and reasons, never a payroll total:
                                                 # this is readable with hr.view while salaries
                                                 # are not. Corrections are called out — a rise
                                                 # is a decision, a correction is a mistake
GET /api/v1/dashboard/expense-exceptions?days=   # waived rules, and employees still out of
                                                 # pocket after approval
```

All three export like every other report (`/export?format=csv|html|json`), and
all three HR workflows reach the Decision Inbox with SLAs and escalation.


---

## Integration Hub (added 2026-08)

**Sarmaya pushes facts, and pulls only configuration.** A journal entry is
posted *after* money has already moved — a payment released, an expense claim
paid — so the client's ledger is told what happened, never asked to approve
anything. The only thing pulled back is reference data (chart of accounts,
vendor and customer list), on demand, as a wholesale replace. There is no
continuous two-way sync and nothing to reconcile: Sarmaya never edits a record
in the client's books, so the two copies cannot disagree about who changed what.

**QuickBooks Online is the only provider today.** The interface
(`FinanceConnector`) is provider-neutral and resolved per connection from
`connection.provider`, not from a global setting — two tenants can be on
different providers, or none. Xero and SAP are deliberately unbuilt; see DR-049.

**Posting is opportunistic and can never block a payment.** A tenant with no
connection, a connection that is not `connected`, or one whose posting accounts
have not been chosen yet queues nothing and is not warned. Today that is every
tenant.

```bash
POST /api/v1/integrations/{provider}/connect      # {redirect_uri?}
     # -> { "authorization_url" } — navigate the browser there with
     #    window.location.href, NOT fetch: the browser has to leave the SPA
GET  /api/v1/integrations/{provider}/callback     # ?state=&code=&realmId=
     # NO Authorization header — the caller is Intuit's redirect, not the
     # frontend. The tenant comes entirely from `state`. Answers with a 302
     # back to /ai-tools/system/integrations?connected= or ?error=, because a
     # browser mid-navigation cannot read a JSON body
POST /api/v1/integrations/{provider}/disconnect   # revokes upstream, wipes both tokens
POST /api/v1/integrations/{provider}/refresh      # re-pull accounts + parties (replace)
GET  /api/v1/integrations/{provider}/status       # -> includes ready_to_post
POST /api/v1/integrations/{provider}/default-accounts
     # {liability_account_external_id, bank_account_external_id}
     # Required before anything can post. Validated against the last pull —
     # guessing "Accounts Payable" by name posts to the wrong account in a
     # chart that spells it differently
GET  /api/v1/integrations/{provider}/accounts
GET  /api/v1/integrations/{provider}/parties?type=vendor|customer
POST /api/v1/integrations/{provider}/vendors/{vendor_id}/map   # {external_party_id}
     # Optional. An unmapped vendor still posts, just without an Entity tag
GET  /api/v1/integrations/{provider}/posts?status=pending|posted|failed
POST /api/v1/integrations/{provider}/posts/{post_id}/retry
     # Only a failed post. `attempts` is NOT reset — the failure history
     # survives for whoever asks how long this went unnoticed
```

Status codes beyond the usual: **424** the provider's own auth failed (the
connection needs reconnecting), **502** the provider could not be reached.

`integrations.view` reads; `integrations.manage` connects, disconnects, maps and
retries. Admin holds both; CFO and Auditor view only — connecting touches OAuth
credentials for the tenant's external accounting system.

**The queue drains on a schedule**, not in the request:

```bash
python -m scripts.dispatch_integration_posts   # every 5 minutes
```

It reports on the System Health page like the other scheduled jobs
(`JOB_INTEGRATION_POSTS`). Without it running, entries queue and never reach the
client's books.
