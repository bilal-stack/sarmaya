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

Tests: **231 passing** (`./.venv/Scripts/python.exe -m pytest`).

## Known follow-ups (not yet done)

- ~~**Token revocation on logout**~~ — **done** (migration `009_user_token_version`):
  a per-user `token_version` is embedded in every JWT and checked live in
  `get_current_user`; `/auth/logout` and `/auth/change-password` bump it,
  invalidating all of that user's outstanding tokens. `/auth/refresh` now routes
  through `get_current_user`, so a revoked token can't be exchanged for a fresh
  one. (Tradeoff: logout invalidates all sessions/devices, not just one.)
- ~~**Workflow/policy versioning**~~ — **done** (migration `011_config_versions`):
  append-only `config_versions` table stores a full JSON snapshot + monotonic
  version per config object on every change (approval policy, autopilot settings,
  workflow transitions — the whole workflow as one versioned document). History
  is read via `GET /config/versions/{config_type}/{config_key}[/{version}]`. Live
  tables stay the current source of truth. **Rollback done too**: `POST
  /config/versions/{config_type}/{config_key}/{version}/restore` re-applies a past
  snapshot as current config and records the rollback as a new `restored` version.
- ~~**Cryptographic audit immutability**~~ — **done** (migration `010_audit_hash_chain`):
  per-object hash chain on `audit_logs` (`prev_hash`/`entry_hash`, SHA-256 of
  prev + canonical row), written in `log_audit` and verifiable via
  `GET /audit/verify/{object_type}/{object_id}`. Detects altered/deleted/reordered
  events within an object. (Scope: per-object, not a tenant-wide ledger; ordering
  uses `(timestamp, id)`, so two same-microsecond events for one object are a
  known edge — a tenant-wide sequenced ledger is the next step if needed.)
- ~~**Decision Register**~~ — **done**: `docs/DECISION_REGISTER.md` (Appendix-C
  template), backfilled with the key design calls (DR-001..005). Append going forward.
- ~~**SoD enforcement**~~ — **done** (`app/services/sod.py`): maker-checker on
  invoice approval (creator ≠ approver) and vendor activation (creator ≠ activator),
  blocked attempts audited; admins exempt as the "explicitly allowed" carve-out.
  Follow-ups: per-rule config/thresholds, and the vendor-bank-change-vs-first-payment rule.
- **Workflow-engine depth** — *started*: **configurable transition guards** done
  (`app/services/workflow_guards.py` + `guards` JSON on `workflow_states`,
  migration 013). `transition_state` blocks a transition unless its configured,
  named guards pass (required_fields_present / vendor_active / duplicate_resolved);
  guards are versioned in the workflow snapshot and seeded by provisioning.
  Remaining: SLAs + escalation, delegation, and consolidating the invoice
  service's explicit gates onto the guard engine (currently dual-enforced —
  see Decision Register DR-006).
- **AI-gating discipline** — *largely done*. Duplicate-detection output is
  schema-validated (`app/schemas/ai.py` `DuplicateDetectionResult`) with
  model/provider provenance; malformed AI output falls back to a safe
  non-duplicate "manual review" result (AI never finalizes). **Every AI action is
  now logged** (migration `012_ai_action_logs` + `log_ai_action`): the
  duplicate-detection and NL→SQL query agents record provider/model, prompt
  version, confidence, latency, in/out summary, and status (completed /
  failed_schema / error) — the Build Book Appendix-A `ai.*` event family. Read
  via `GET /audit/ai-actions` (auditor/admin). Still to do: schema-validate the
  OCR extraction result, and a richer "signals used" explainability trace.
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
- Migration head: `013_workflow_transition_guards`.
