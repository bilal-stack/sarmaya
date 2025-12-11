# Project README — Sarmaya OS (mini)

## Quick status (current)
- **Alembic migrations recreated (clean state)**:
  - Initial schema migration (all tables)
  - Seed data migration (demo tenant, users, workflow states, vendor)
  - RLS migration (applied AFTER tables exist)
- Models: tenants, users, vendors, workflow_states, policies, files, invoices, audit_logs, ai_conversations, ai_messages
- Auth endpoints implemented (registration, login, me, logout, profile update, password change)
- Invoice endpoints with OCR (Google Document AI)
- AI chat with conversation persistence
- Audit logging system
- Issues resolved:
  - RLS now applied AFTER table creation (correct order)
  - Password hashing: `bcrypt_sha256` to avoid 72-byte limit
  - Migration chain: linear and verified

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
- `dashboard`, `chatbot` — app-specific endpoints (planned).

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
- Current chain: b105d820d4a4 -> 002_seed_demo -> 002_seed_demo_data -> 003_enable_rls
- Run upgrade head after adding new migrations (e.g., audit enhancements).

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