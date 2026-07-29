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

Tests: **286 passing** (`./.venv/Scripts/python.exe -m pytest`).

**Frontend integration backlog:** the post-MVP endpoints (esp. `GET
/invoices/{id}/next-action`) have no UI yet — integration guidance lives in
`ENDPOINTS.md` → "Post-MVP Governance & AI Endpoints".

**Invoice next-action agent** (`app/agents/invoice_agent.py`, blueprint Part 7
"Workflow agent" class): `GET /invoices/{id}/next-action` suggests — never
executes — the next step (review_extraction / fix_missing_fields / validate /
submit_for_approval / resolve_duplicate / verify_vendor / approve / mark_paid).
Deterministic signals fix the policy-permitted action; the AI only phrases the
suggestion within that gate (schema-validated; strays/malformed output falls
back to rules with status=failed_schema); HITL-type suggestions log
hitl_requested; every run lands in ai_action_logs (DR-007).

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
- **Workflow-engine depth** — guards **and SLAs+escalation done**. Guards:
  `app/services/workflow_guards.py` + `guards` JSON on `workflow_states`
  (migration 013), enforced by `transition_state`, versioned, seeded. SLAs
  (migration 014, DR-009): per-state `sla` config (`{"hours", "escalate_to"}`,
  editable via `PUT .../states/{state}/sla`, versioned), timer =
  `invoices.state_entered_at` restarted on every transition, deadlines computed
  at read time; Decision Inbox items carry `sla_due_at`/`overdue`/`escalated`,
  breached items sort first (`overdue_only` filter = the "Overdue" view), the
  escalation role gains visibility live once breached, and
  `POST /inbox/escalate-overdue` records one audited `sla_escalated` event per
  state entry + notifies (idempotent; cron-able). Remaining: delegation, and
  consolidating the invoice service's explicit gates onto the guard engine
  (DR-006).
- ~~**AI-gating discipline**~~ — **done** across all AI surfaces. Schema-validated
  structured output + provenance + fallback-on-malformed for: duplicate detection
  (`DuplicateDetectionResult`), the next-action agent
  (`InvoiceNextActionSuggestion`, gate: AI may only phrase the policy-permitted
  action), and OCR extraction enhancement (`InvoiceExtractionResult`, lenient
  scalar coercion / strict structure — DR-008; malformed AI output rejected, raw
  OCR stands). **Every AI action logged** (migration `012_ai_action_logs`):
  duplicate detection, NL→SQL query, next-action, and invoice extraction all
  record provider/model, prompt version, confidence, latency, in/out summary,
  status (completed / failed_schema / hitl_requested / error). Read via
  `GET /audit/ai-actions`.
- ~~**PolicyEval records**~~ — **done** (migration `015_policy_evals`): every
  approval-routing decision is snapshotted as a `policy_evals` row with the
  matched rule, its `config_versions` number, the inputs, the decision, and the
  reasons — recorded on both submit and approve. A decision stays reproducible
  after the policy is edited or rolled back (tested). Read via
  `GET /audit/policy-evals` (auditor or policy admin).
- ~~**Universal correlation_id**~~ — **done** (migration `016_correlation_id`):
  every invoice mints a chain id at creation; audit events, policy evaluations
  and AI actions inherit it automatically (`log_audit` resolves it when not
  passed, so no call site can drop an event out of its story).
  `GET /audit/chain/{correlation_id}` merges all three record types into one
  time-ordered feed across every object in the chain — the surface future
  modules (PR/PO/GRN/payment) join without changing the endpoint. Deliberately
  excluded from the audit integrity hash (DR-011).
- ~~**Evidence Pack Generator**~~ — **done** (migration `017_evidence_packs`):
  `POST /audit/evidence-pack/{correlation_id}` assembles an audit-ready bundle
  for a chain — objects, full audit trail + its hash-chain verification, policy
  evaluation snapshots, AI action log, and every attachment with its content
  hash — sealed with a SHA-256 `pack_hash`. Regenerating and comparing hashes
  shows whether anything underlying the export changed. `GET` previews without
  recording; `GET /audit/evidence-packs` lists what was generated, when, by whom.
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
- Migration head: `017_evidence_packs`.
