# Agent memory & project context

Snapshot of the Claude Code project memory for Sarmaya OS, committed so it
travels with the repo (e.g. when continuing on another machine).

## What these files are

| File | Purpose |
|------|---------|
| `MEMORY.md` | Index of the memory notes below |
| `project_sarmaya_overview.md` | Stack, architecture, MVP stage |
| `project_mvp_spec.md` | Contracted MVP scope + where code diverges |
| `project_build_book.md` | Long-term vision (governance-first Ops OS), non-negotiables |
| `project_erp_blueprint.md` | Most detailed spec (modules/workflows/AI/acceptance) |
| `user_role.md` | Developer is bilal (Windows); spec owner is Andrew |
| `feedback_standards_security.md` | Expect best-practice, secured, layered code |

## Restoring on another PC

Claude Code auto-memory lives in your home directory, not the repo. To make
Claude pick these up automatically on the new machine, copy them into the
project's memory folder:

```powershell
# Windows
$dst = "$env:USERPROFILE\.claude\projects\C--python-sarmaya\memory"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "docs\agent-memory\*.md" -Destination $dst -Force
```

```bash
# macOS/Linux (project-slug folder name may differ; match the existing one)
mkdir -p ~/.claude/projects/C--python-sarmaya/memory
cp docs/agent-memory/*.md ~/.claude/projects/C--python-sarmaya/memory/
```

Otherwise just read them directly for context.

## Project state — where we are

**MVP is functionally complete**, and the governance-first platform layer (the
four Build Book differentiators) is built on top:

1. **Configuration-first policies** — approval matrix + workflow transitions are
   editable DB config (not hardcode); admin CRUD API; new tenants get defaults
   via `POST /config/initialize-defaults`.
2. **Live Audit Mode** — `GET /audit/timeline/{type}/{id}`: per-object timeline
   with a plain-English reason per event and the policy routing reason; the
   routing reason is **snapshotted at decision time** (no drift if policy edited).
3. **Decision Inbox** — `GET /inbox`: one prioritized worklist, each pending
   invoice reduced to its top blocker (duplicate > vendor > approval), filtered
   to what the caller can act on, linking to timelines.
4. **Restricted Autopilot** — opt-in, low-risk, reversible, logged auto-approval
   within configured bounds; `GET/PUT /config/autopilot`, `GET /autopilot/preview`,
   `POST /autopilot/run`, `POST /autopilot/{id}/revert`.

**Review-and-fix pass also done:** enforced permissions on invoice
create/update/delete/upload; removed the unauthenticated `/files/upload`
placeholder and dead middleware; stopped error-detail leakage on the AI
endpoints (+ gated them); fixed a conversations N+1; made role checks
case-insensitive; and resolved current-user identity (role/active) live from the
DB instead of trusting JWT claims.

Tests: **171 passing** (`./.venv/Scripts/python.exe -m pytest`).

## Known follow-ups (not yet done)

- **Token revocation on logout** — stateless JWT can't be revoked; needs a token
  store or per-user token version. (Claim *staleness* is already fixed.)
- **Workflow/policy versioning** — config is edited in place; Build Book wants
  versioned JSON per workflow.
- **Cryptographic audit immutability** — audit trail is append-only by
  convention, not hash-chained.
- **Decision Register** — Build Book process for logging ambiguities/defaults.
- **AI-gating discipline** for the existing chat / NL→SQL query agent
  (schema-validated JSON, explainability trace, prompt/model version).
- **`print()` → logging** in `app/agents/**` (left intentionally for now —
  console logging wasn't surfacing).
- Aspirational Build Book stack (Temporal, OPA/Rego, NATS, Keycloak, S3/MinIO,
  OpenSearch, Qdrant) is **not** wired — current system is a FastAPI + Postgres
  modular monolith, which the Build Book sanctions as "monolith first".

## Dev environment notes

- Run app deps as least-privilege `os_app` role; migrations via `ADMIN_DATABASE_URL`
  (Alembic). Real secrets live in `.env` (gitignored — never commit).
- Tests need a live Postgres; test DB is `os_test`. Some new columns must be
  applied to `os_test` manually since conftest's `create_all` doesn't ALTER
  existing tables.
- Migration head: `008_seed_config_policies`.
