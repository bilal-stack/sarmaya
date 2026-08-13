# Project README — Sarmaya OS (mini)

## Quick status (current)
- **Alembic migrations recreated (clean state)**:
  - Initial schema migration (all tables)
  - Seed data migration (demo tenant, users, workflow states, vendor)
  - RLS migration (applied AFTER tables exist)
- Models: tenants, users, vendors, workflow_states, policies, files, invoices (with line_items), audit_logs, ai_conversations, ai_messages
- Auth endpoints implemented (registration, login, me, logout, profile update, password change)
- Invoice endpoints with OCR (Google Document AI) + AI enhancement
- **AI-powered OCR fixes**: Merges fragmented line items, cleans descriptions
- AI chat with conversation persistence
- **AI-powered function calling** for real-time invoice queries
- Audit logging system
- **CORS configured** for cross-origin requests (handles OPTIONS preflight)
- Issues resolved:
  - RLS now applied AFTER table creation (correct order)
  - Password hashing: `bcrypt_sha256` to avoid 72-byte limit
  - Migration chain: linear and verified
  - CORS preflight OPTIONS requests handled
  - **Line item fragmentation fixed** (AI merges "Proof Torch" + "Color" correctly)

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

**Note:** CORS is configured to allow requests from common frontend dev servers (localhost:3000, localhost:5173). Update `CORS_ORIGINS` in `.env` for production.

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
  - Response: Created user (UserOut).  
  - Notes: Only creates user in specified tenant; role assignment validation recommended.

- POST `/api/v1/auth/login`  
  - Purpose: Authenticate user and return JWT.  
  - Query param: `tenant` (optional, defaults to `demo`).  
  - Body: JSON `{"email": "...", "password": "..."}` (LoginIn).  
  - Response: `{"access_token": "...", "token_type":"bearer"}`.  
  - Notes: Token payload contains `sub` (user id) and `tenant_id`.

- GET `/api/v1/auth/me`  
  - Purpose: Return current user info (requires Bearer token).  
  - Response: UserOut.

- POST `/api/v1/auth/logout`  
  - Purpose: Invalidate current session (logout).  
  - Response: `{"detail": "Successfully logged out"}`.

- PUT `/api/v1/auth/update-profile`  
  - Purpose: Update current user profile.  
  - Body: JSON matching `UserUpdate` (email, full_name, optional role).  
  - Response: Updated user (UserOut).

- POST `/api/v1/auth/change-password`  
  - Purpose: Change user password.  
  - Body: JSON `{"old_password": "...", "new_password": "..."}`.  
  - Response: `{"detail": "Password updated successfully"}`.

---

## Other routers (planned / included)
Routers are mounted under `/api/v1`:
- `invoices` — invoice CRUD, approval flow (planned/partial).
- `files` — file upload/download (planned/partial).
- `vendors` — vendor management (planned).
- `dashboard` — app-specific endpoints (planned).

Check `app/api/` for implementation details.

---

## Row Level Security (RLS)
- Migration file: `alembic/versions/003_enable_rls_policies.py`.
- To enable RLS via Alembic:
  ```
  alembic upgrade head
  ```
  (This will run the RLS migration if it is next in chain.)
- Verify in PostgreSQL (Query Tool / pgAdmin):
  - Which tables have RLS enabled:
    ```sql
    SELECT n.nspname AS schema, c.relname AS table, c.relrowsecurity
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'public' AND c.relrowsecurity = true;
    ```
  - List policies:
    ```sql
    SELECT * FROM pg_policies WHERE schemaname = 'public';
    ```
- To test RLS behavior in a session:
  ```sql
  SET app.current_tenant_id = '00000000-0000-0000-0000-000000000001';
  SELECT * FROM users;
  ```
- Note: RLS must be applied after tables exist. If migrations fail due to migration chain issues, fix down_revision values in `alembic/versions/` so they form a linear chain, then run `alembic upgrade head`.

---

## Debug & troubleshooting
- Alembic revision chain problems:
  - List files:
    ```
    dir alembic\versions\
    ```
  - Show revision lines:
    ```
    type alembic\versions\*.py | findstr "revision ="
    ```
  - Ensure each migration's `down_revision` references the actual prior revision id.

- Bcrypt / passlib errors:
  - Install/upgrade bcrypt:
    ```
    pip install --upgrade bcrypt
    ```
  - The code uses `bcrypt_sha256` to avoid 72-byte limit issues.

- Verify applied migrations:
  ```
  psql -U postgres -d sarmaya_os -c "SELECT * FROM alembic_version;"
  psql -U postgres -d sarmaya_os -c "\dt"
  ```

---

## Next recommended tasks
- Harden auth:
  - Validate role assignment and restrict admin creation.
  - Re-hash legacy bcrypt hashes to `bcrypt_sha256` on login.
- Implement invoice, vendor, file endpoints and tests.
- Add API docs examples (OpenAPI) and auth examples in README.
- Confirm migrations applied and enable RLS in environment.

---

## Database migrations (Alembic)
- Apply all pending migrations:
  ```
  alembic upgrade head
  ```
- If the DB is out of sync and you want to align to current revision without changing schema:
  ```
  alembic stamp head
  ```
  then run:
  ```
  alembic upgrade head
  ```
- To view current revision:
  ```
  alembic current
  ```
- To see history:
  ```
  alembic history
  ```

Notes
- Head is `026_backfill_new_workflows`. Run `alembic upgrade head` after adding migrations.
- **Every developer and test database here is built with `create_all`, not
  migrations.** They diverge, and only running the migrations proves them —
  see the deployment section below for how to check.

---

## Deploying to a server

A production database starts **empty on purpose**. Migration 002 seeds a demo
tenant whose five accounts, one of them an administrator, share a password
written in this repository; running it on a server would hand an admin account
to anyone who can reach the login page. It is therefore skipped unless
`SEED_DEMO_DATA` is set.

```bash
alembic upgrade head
```

Then create the first tenant and administrator. `/auth/register` cannot do this
— it requires a tenant to already exist and only ever grants the default clerk
role — so credentials come from the environment, never from source:

```bash
BOOTSTRAP_TENANT_NAME="Acme Holdings" BOOTSTRAP_ADMIN_EMAIL="ops@acme.com" BOOTSTRAP_ADMIN_PASSWORD="..." python -m scripts.bootstrap_tenant
```

It refuses to run against a database that already has users, and refuses a
password under 12 characters. Sign in and change the password immediately; every
later account is created through the application, where permission checks and
the audit trail apply.

For a local database with the demo data:

```bash
SEED_DEMO_DATA=true alembic upgrade head
```

### Where to host it (all free)

Three providers, because no single free tier does all three parts well:

| Part | Provider | Why this one |
|---|---|---|
| Postgres | **Neon** | Real Postgres 17, so the row-level security this app's isolation depends on works. Free tier does not expire — Render's free Postgres is deleted after 30 days, which is a bad surprise to discover with data in it. |
| API | **Render** | Runs the Dockerfile as-is, has a pre-deploy hook for migrations, and reads the blueprint in `render.yaml`. Free instances sleep after ~15 minutes idle and take about a minute to wake — fine for a demo, not for a customer. |
| Frontend | **Vercel** | Next.js is theirs; a static export of this app costs nothing and never sleeps. |

Nothing here is a commitment. The Dockerfile is plain and every setting comes
from the environment, so Fly, Railway or a plain VM take the same values.

### Deploying

**1. Database (Neon).** Create a project, then create the least-privilege role
the app runs as — the connection string Neon gives you by default is the owner,
and an owner is the wrong thing to hand a web process:

```sql
CREATE ROLE os_app LOGIN PASSWORD 'generate-something-long';
GRANT CONNECT ON DATABASE neondb TO os_app;
GRANT USAGE ON SCHEMA public TO os_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO os_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO os_app;
```

Run this **after** the first migration, so the tables exist to grant on. The
migrations set `FORCE ROW LEVEL SECURITY`, so policies apply to the owner too —
but `os_app` is still the right role for the app, because a future superuser
connection would bypass them silently.

**2. API (Render).** Point a new Blueprint at this repo; `render.yaml` defines
the service. Set the three variables it marks `sync: false`:

- `DATABASE_URL` — the **os_app** connection string
- `ADMIN_DATABASE_URL` — the owner connection string, used only by Alembic
- `CORS_ORIGINS` — your Vercel URL, e.g. `https://sarmaya.vercel.app`

`SECRET_KEY` is generated by Render. The app refuses to start without a real
one, and refuses to start if `CORS_ORIGINS` still says localhost — both are
misconfigurations that otherwise look like a working deployment right up until
they don't.

**3. First tenant.** Migrations create an empty database on purpose: the demo
seed's accounts share a password published in this repository. From a Render
shell:

```bash
BOOTSTRAP_TENANT_NAME="Acme" BOOTSTRAP_ADMIN_EMAIL="you@acme.com" BOOTSTRAP_ADMIN_PASSWORD="..." python -m scripts.bootstrap_tenant
```

**4. Frontend (Vercel).** Import the frontend repo and set one variable:

```bash
NEXT_PUBLIC_API_BASE_URL=https://sarmaya-api.onrender.com/api/v1
```

It is inlined at build time, so changing it needs a redeploy — and it is public
by definition, so never put a secret in a `NEXT_PUBLIC_` variable.

### Checking the deployment actually worked

A deployment can answer 200 on every route and still be wrong in ways nothing
surfaces — most importantly if `DATABASE_URL` connects as the database owner,
which bypasses every RLS policy silently. The connection string a provider
hands you *is* the owner's, so this is the easy one to get wrong.

```bash
python -m scripts.verify_deployment https://sarmaya-api.onrender.com --database-url "postgresql://os_app:...@...neon.tech/neondb" --origin https://sarmaya.vercel.app
```

It checks that the app role cannot bypass RLS, that every tenant-owned table
has policies and that a bound tenant still sees its own rows, that migrations
are at head and the newest module is deployed, that self-registration is
closed and the published demo credentials do not work, that CORS admits your
frontend, and that somebody can actually sign in. Exits non-zero on any
failure, so it can gate a release.

Anything it could not test is reported as unchecked rather than passed — an
unchecked control is not a passing one.

### Configuration that will stop a deploy

Deliberately, because each of these looks like a working system until it is far
too late:

- **`DEBUG` defaults to `false`.** When on, the 500 handler returns the
  exception type and message to the caller.
- **`SECRET_KEY`** must not be the repository's placeholder and must be at
  least 32 characters. Every access token is signed with it; a public default
  means anyone who can read this repo can mint one claiming an admin's id.
- **`CORS_ORIGINS`** must not contain localhost. It accepts a comma-separated
  list or a JSON array.
- **`ALLOW_SELF_REGISTRATION` defaults to `false`.** Even at the default clerk
  role a self-registered stranger can create vendors, raise invoices, prepare
  payment runs and import bank statements.

### Proving the migrations before you deploy

The suite normally runs against a `create_all` database, which cannot catch a
migration that produces a different schema. Point it at a migrated one:

```bash
createdb os_deploycheck && ADMIN_DATABASE_URL=postgresql://.../os_deploycheck alembic upgrade head
```

```bash
TEST_DATABASE_URL=postgresql://.../os_deploycheck pytest -q
```

Known and accepted differences between the migrated schema and the models, none
of which affect behaviour (the suite passes identically against both):

- Some `VARCHAR` columns are wider than the model needs (`String(50)` in a
  migration against an enum that renders as `VARCHAR(15)`).
- `workflow_states.guards` and `.sla` are `NOT NULL` with a server default in
  the database and nullable in the model — the database is the stricter of the
  two.
- The migrations create composite indexes (`ix_audit_logs_object_chain` and
  similar) where the models declare single-column ones. A performance
  difference, not a correctness one; the composite indexes lead with
  `tenant_id`, which is what every scoped query filters on.

---

## Google Document AI Setup

1. **Create service account:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/iam-admin/serviceaccounts)
   - Create service account with "Document AI API User" role
   - Download JSON key file → save as `os-google.json`

2. **Enable Document AI API:**
   - Visit [Document AI API](https://console.cloud.google.com/apis/library/documentai.googleapis.com)
   - Click "Enable"

3. **Create processor:**
   - Go to [Document AI Processors](https://console.cloud.google.com/ai/document-ai/processors)
   - Create new "Invoice Parser" processor
   - Copy processor ID

4. **Set environment variable:**
   - Windows PowerShell:
     ```powershell
     $env:GOOGLE_APPLICATION_CREDENTIALS="C:\python\os\os-google.json"
     ```
   - Linux/Mac:
     ```bash
     export GOOGLE_APPLICATION_CREDENTIALS="/path/to/os-google.json"
     ```

5. **Update `.env`:**
   ```dotenv
   GOOGLE_CLOUD_PROJECT_ID=sarmayaos
   GOOGLE_DOCUMENT_AI_PROCESSOR_ID=your-processor-id
   GOOGLE_APPLICATION_CREDENTIALS=C:\python\os\os-google.json
   ```

---

Contact / dev notes
- API base: `http://127.0.0.1:8000/api/v1`  
- For help fixing Alembic chain, paste `dir alembic\versions\` output and `type alembic\versions\*.py | findstr "revision ="`.

---

## AI Features
- **AI Agents Architecture**:
  - Query Agent (natural language → SQL with function calling)
  - Duplicate Detection Agent (multi-strategy: exact/fuzzy/line-item)
  - SQL Tools for structured database queries