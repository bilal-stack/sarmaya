# Decision Register

The Build Book (Appendix C) requires that, when something is ambiguous, the
developer logs the decision and the chosen default rather than interpreting
ad-hoc. This file is that register — append-only; one entry per non-obvious
design call. Template:

```
Decision ID:
Date:
Context:
Options:
Decision:
Rationale:
Impact:
Owner:
```

---

**DR-001 — Workflow/policy versioning model**
- Date: 2026-06-05
- Context: Build Book wants config "stored as versioned JSON per workflow"; existing config was edited in place.
- Options: (a) append-only snapshot-history table; (b) full versioned-document model where the active version is read by the engine.
- Decision: (a) snapshot-history table (`config_versions`); live tables stay the current source of truth.
- Rationale: Lowest risk, no refactor of `transition_state`/approval routing, satisfies "versioned JSON", enables rollback. (b) is a larger refactor for little near-term gain.
- Impact: Added `config_versions` (migration 011) + rollback (`restored` versions). A full document model remains possible later.
- Owner: bilal (dev)

**DR-002 — Audit immutability: per-object hash chain**
- Date: 2026-06-05
- Context: Build Book requires immutable audit / evidence integrity hashing.
- Options: (a) per-object hash chain; (b) tenant-wide sequenced ledger.
- Decision: (a) per-object chain (`prev_hash`/`entry_hash` on `audit_logs`, migration 010), ordered by `(timestamp, id)`.
- Rationale: Pairs with the per-object Live Audit timeline; detects altered/deleted/reordered events within an object without a global sequence. (b) is the next step if cross-object deletion detection is needed.
- Impact: Tamper-evident per-object trail + `GET /audit/verify`. Known edge: two same-microsecond events for one object.
- Owner: bilal (dev)

**DR-003 — JWT revocation via per-user token_version**
- Date: 2026-06-05
- Context: Logout/`token revocation` for stateless JWTs.
- Options: (a) per-user `token_version` embedded in the JWT; (b) jti denylist table.
- Decision: (a) token_version (migration 009).
- Rationale: Zero extra queries (the user row is already loaded each request), no new table; also invalidates on password change. Tradeoff: logout invalidates all of a user's sessions, not one device.
- Impact: `/auth/logout` + `/auth/change-password` bump the counter; `/auth/refresh` is revocation-aware.
- Owner: bilal (dev)

**DR-004 — AI-action logging as a dedicated table**
- Date: 2026-06-05
- Context: Build Book "AI is gated" + Appendix A `ai.*` events require every AI action logged with provenance.
- Options: (a) dedicated `ai_action_logs` table; (b) reuse `audit_logs`.
- Decision: (a) dedicated table (migration 012).
- Rationale: `audit_logs.object_id` is NOT NULL and hash-chained per object; AI actions (chat/query) often have no object and need AI-specific fields (model, prompt version, latency, confidence). A separate table matches the blueprint's AI-log field spec and keeps the per-object timeline clean.
- Impact: `log_ai_action()` + `GET /audit/ai-actions`.
- Owner: bilal (dev)

**DR-005 — SoD admin exemption**
- Date: 2026-06-05
- Context: Build Book maker-checker SoD (lines 190-193) says "unless explicitly allowed"; a single-admin demo must still function.
- Options: (a) enforce SoD for everyone; (b) exempt the admin role as the "explicitly allowed" carve-out.
- Decision: (b) admins exempt; non-admins blocked from approving an invoice they created / activating a vendor they created.
- Rationale: Enforces the control where it bites (clerks/managers both hold vendors.manage) while keeping admin operability. Finer config (per-rule toggles, thresholds, the vendor-bank-change rule) is a follow-up.
- Impact: `app/services/sod.py` enforced in invoice approval + vendor activation; blocked attempts are audited.
- Owner: bilal (dev)

**DR-006 — Transition guards: engine layer alongside existing service gates**
- Date: 2026-06-05
- Context: Build Book wants per-transition guards in versioned config; the invoice service already enforces the same checks imperatively (validate = required fields; approve = vendor active + duplicate resolved), with their own audit records and specific error types.
- Options: (a) add a config-driven guard engine in `transition_state`, keep the service's explicit gates (dual enforcement, transitional); (b) rip the gates out of the service and delegate entirely to the engine now.
- Decision: (a) — add the guard engine + seed guards matching current behavior; leave the service gates in place for now.
- Rationale: Delivers the workflow-first mechanism (guards as versioned config, usable by any workflow) with zero behavior change and no risk to the live invoice flow. Full consolidation onto the engine is a follow-up.
- Impact: `app/services/workflow_guards.py`, `guards` on `workflow_states` (migration 013); seeded defaults; versioned in the workflow snapshot. Invoice flows are momentarily dual-checked (service gate fires first; engine guard is a config-first safety net).
- Owner: bilal (dev)

**DR-007 — Invoice next-action agent is suggestion-only, rules fix the gate**
- Date: 2026-06-05
- Context: The ERP Blueprint (Part 7) names a "Workflow agent" class (validation, routing, exception detection), but the Build Book forbids AI from moving workflow states or finalizing decisions ("Agents Assist. Policies Decide.").
- Options: (a) an AI agent that chooses among extract/validate/escalate and acts; (b) a suggestion-only agent where deterministic signals fix the single policy-permitted action and the AI may only phrase/score it, schema-validated.
- Decision: (b). The AI's output must name exactly the permitted action; anything else (wrong action, malformed JSON) is discarded in favor of the rules result and logged as `failed_schema`.
- Rationale: Delivers the blueprint's agent UX (a "what should I do next?" surface with reasoning + confidence + signals) without ever letting the model override policy. HITL triggers (low OCR confidence, duplicate, unverified vendor) log `hitl_requested` per Appendix A.
- Impact: `app/agents/invoice_agent.py` + `GET /invoices/{id}/next-action`; `AI_EXTRACTION_REVIEW_THRESHOLD` setting (default 70); every run logged to `ai_action_logs` with provenance.
- Owner: bilal (dev)

**DR-008 — Extraction gating: lenient scalars, strict structure**
- Date: 2026-06-05
- Context: Gating the AI-enhanced OCR extraction (Build Book: schema validation always). LLM output is fuzzy — amounts arrive as "Rs 1,250,000.50", confidence as strings — and rejecting a whole extraction over a formatting quirk would throw away good field data the clerk then re-types.
- Options: (a) strict validation — any nonconforming field voids the result; (b) lenient coercion on scalars (money strings cleaned, unparseable → 0.0 which the required-fields guard later catches; confidence clamped 0–100) but strict on structure (result must be an object; line_items must be a list of objects), with structural violations rejecting the whole result in favor of raw OCR.
- Decision: (b), implemented in `InvoiceExtractionResult.try_validate`.
- Rationale: Scalar quirks are recoverable and fail safe (a zeroed amount blocks validation, routing to fix_missing_fields); structural garbage means the model didn't follow the contract at all and nothing in it should be trusted. Rejections are logged as `failed_schema` in ai_action_logs.
- Impact: `app/services/ocr/__init__.py` validates AI enhancement before merging; the extraction step is now logged (action=invoice_extraction, prompt invoice-extract-v1).
- Owner: bilal (dev)

**DR-009 — SLA escalation without a background scheduler**
- Date: 2026-06-05
- Context: Build Book wants SLA timers per state with escalation, and its stack default is Temporal. The current sanctioned architecture is a FastAPI + Postgres monolith with no job runner.
- Options: (a) add a scheduler (celery/APScheduler/Temporal) now; (b) lazy evaluation — deadlines computed at read time from `state_entered_at` + per-state SLA config, escalation visibility granted live in the Decision Inbox once breached, and a manual/cron-able idempotent runner (`POST /inbox/escalate-overdue`) that records the audited escalation event and notifies.
- Decision: (b). Deadlines are always current (an SLA config change re-prices every open timer instantly), no new infrastructure, and the runner is idempotent per state entry so wiring it to cron or a future scheduler is trivial.
- Rationale: Correctness without infra risk; the read-time model actually beats stored deadlines for configuration-first behavior. A real scheduler (Temporal per Build Book) slots in later by simply invoking the same runner.
- Impact: `workflow_states.sla` + `invoices.state_entered_at` (migration 014, timer restarted by `transition_state`), inbox overdue surfacing/sort/filter + escalation-role visibility, `SlaService.run_escalations`, `PUT .../states/{state}/sla` (versioned). Escalation preserves the original approver chain (routing snapshot stays; escalation is an additive audit event).
- Owner: bilal (dev)

**DR-010 — PolicyEval reuses config_versions rather than its own version counter**
- Date: 2026-06-05
- Context: The Build Book requires each policy evaluation to store a `policy_version`. Policies have no version column of their own; versions live in the `config_versions` history added for DR-001.
- Options: (a) add a `version` column to `policies` and bump it on edit; (b) record the policy's current `config_versions` number at evaluation time.
- Decision: (b).
- Rationale: One source of truth for "what version of this rule". A recorded `policy_version` points at an actual restorable snapshot in config history, so an auditor can fetch the exact rule text that made a decision via `GET /config/versions/approval_policy/{id}/{version}`. Option (a) would create a second, parallel counter that could drift from the history and restore nothing.
- Impact: `app/services/policy_eval.py` looks up `max(config_versions.version)` for the matched policy; snapshots record `null` when no configured rule matched and the hardcoded default applied. Recorded on both submit and approve (an invoice can be evaluated twice, and the second evaluation is the one that authorized the approval).
- Owner: bilal (dev)

**DR-011 — correlation_id excluded from the audit integrity hash**
- Date: 2026-06-05
- Context: Adding `correlation_id` to `audit_logs` raised whether it should join `HASHED_FIELDS` in the per-object integrity chain.
- Options: (a) include it and rehash every existing row in the migration; (b) leave it out of the hash.
- Decision: (b).
- Rationale: It is a linking/index field, not a claim about what happened — the substantive content of each event is already covered. Including it would invalidate every hash written before this migration, and because local/dev databases are built with `create_all` rather than Alembic, the compensating rehash would never run there and `GET /audit/verify` would start reporting false tampering on existing data. Accepted residual risk: re-pointing an event at a different chain is not hash-detectable; the event's own content still is.
- Impact: `correlation_id` added to invoices, audit_logs, policy_evals and ai_action_logs (migration 016) with a back-fill; `HASHED_FIELDS` unchanged, so all existing chains stay valid.
- Owner: bilal (dev)

**DR-012 — server-side timestamp defaults moved to UTC; historical rows left untouched**
- Date: 2026-07-30
- Context: Every timestamp column in the schema is `TIMESTAMP WITHOUT TIME ZONE`, and application code writes UTC into all of them (`utc_now`/`make_naive`). The server-side defaults did not: `now()` returns a `timestamptz`, which Postgres converts to the *session's* zone when storing into a naive column. All 29 server-defaulted columns — including `audit_logs.timestamp`, `invoices.state_entered_at` and every `created_at` — were therefore written in local time while everything around them was UTC. Found while verifying delegation timestamps against the frontend: `created_at` and `starts_at` on the same row were five hours apart.
- Options: (a) leave it and compensate in each reader; (b) repoint the defaults to `timezone('utc', now())` and rewrite existing rows; (c) repoint the defaults and leave existing rows alone.
- Decision: (c).
- Rationale: (a) spreads a schema defect across every consumer and guarantees someone eventually forgets. Against (b): `audit_logs.timestamp` is part of the hash-chain payload (`HASHED_FIELDS` in `services/audit_integrity`), so rewriting historical timestamps would invalidate every existing chain — destroying the tamper-evidence the column exists to provide in order to correct a display offset. Correcting historical data is an operational decision made with knowledge of the zone in effect at write time, not something a schema migration should assume.
- Impact: `UTC_NOW` added to `app/models/base.py` and used by `TimestampMixin`, `AuditLog.timestamp` and `Invoice.state_entered_at`; migration `019_utc_timestamp_defaults` repoints defaults, discovering columns from the catalog rather than a hardcoded list so new tables cannot silently reintroduce the bug; `tests/integration/test_timestamp_defaults.py` fails if any naive timestamp column defaults to a bare `now()`. Frontend `src/lib/datetime.ts` (`parseApiDate`) treats offset-less API timestamps as UTC, since FastAPI serialises these naive columns without a `Z` and JavaScript would otherwise read them as local time.
- Residual risk: none in a real deployment. A database provisioned through Alembic runs 019 before it holds any data, so the local-time default is never in effect and no row is ever written with the wrong clock. The skew exists only in developer databases built with `create_all`, which skip migrations entirely; as of this decision those hold demo data only and were left uncorrected. Should a `create_all` database ever need correcting, the statement for the non-hashed tables is
  `UPDATE <table> SET created_at = created_at AT TIME ZONE '<original server zone>' AT TIME ZONE 'UTC';`
  and `audit_logs.timestamp` must not be corrected this way while chain verification is expected to pass, because it is part of the hashed payload.
- Owner: bilal (dev)

**DR-013 — tenant scoping enforced once in the ORM, not per query**
- Date: 2026-08-06
- Context: Tenant isolation was left entirely to Postgres RLS. Those policies are created by migration 003, so they do not exist in a database built with `create_all` — which is every developer and test database in this project. Confirmed against a running dev server: `GET /users` listed another tenant's staff and a demo-tenant admin applied a role change to a user in a different tenant. A survey found ~39 functions across the services and repositories querying a tenant-owned table without naming `tenant_id`.
- Options: (a) add an explicit `tenant_id` filter to each of the ~39 call sites; (b) require every read to go through a scoped repository base class; (c) apply the restriction once, in a `do_orm_execute` listener, to every ORM SELECT on a session that has a tenant bound.
- Decision: (c).
- Rationale: (a) is a large diff whose weakest point is the next query someone writes — the isolation guarantee would depend on 39 places staying correct forever, and the bug it fixes is silent, because a query returning another tenant's rows looks exactly like one that works. (b) is the same problem with extra ceremony and would still be bypassable by a plain `db.query(...)`. (c) makes the boundary structural: new models are covered the moment they gain a `tenant_id` column, and new queries are covered without anyone remembering. It is a second lock rather than a replacement — RLS still guards raw SQL and anything outside the ORM, and this guards every environment where RLS is absent.
- Impact: `_scope_query_to_tenant` in `app/core/database.py` applies `with_loader_criteria` per tenant-owned mapper, discovered from the registry. Skipped when no tenant is bound, so provisioning, migrations and multi-tenant fixtures still work. Deliberately uses the eager expression form, not `with_loader_criteria`'s lambda: lambdas are cached by code location, so a closed-over tenant would bake the first request's tenant into every later one — a far worse leak than the gap being closed. `tests/integration/test_tenant_scoping.py` covers per-model scoping, fetch-by-known-UUID, and rebinding a session to a second tenant (the test that catches the caching failure).
- Notes: no raw SQL path exists in `app/` outside `set_config`, and the NL query agent uses typed ORM tool calls that already filter `tenant_id`, so the ORM listener is sufficient coverage today. Bulk ORM updates/deletes would bypass it; none exist, and all deletes act on objects fetched through a filtered SELECT.
- Owner: bilal (dev)

**DR-014 — one tenant isolation strategy, and the speculative scaffolding removed**
- Date: 2026-08-06
- Context: The Build Book lists per-tenant isolation strategies as a non-negotiable. `app/core/database.py` carried a `TenantConnectionFactory` offering 'rls', 'schema' and 'database', where the latter two raised `NotImplementedError`; `tenants.isolation_level` recorded the intended strategy per tenant. Nothing in the codebase called the factory, `get_db_with_rls` (its only consumer) was itself unused, and its 'rls' branch did exactly what `get_db_session` already does. Every tenant is 'rls'.
- Options: (a) implement schema-per-tenant and database-per-tenant now; (b) leave the scaffolding in place raising NotImplementedError; (c) remove the factory, keep the one implemented strategy, and record what completing the others would require.
- Decision: (c).
- Rationale: (a) is not a code-only change. A session pointed at a per-tenant schema or database is useless without provisioning — creating the schema or database when a tenant is created, running every migration against it, and pooling connections per tenant — and building that with no consumer, no deployment model for where tenant databases live, and no tenant asking for it would be building the wrong thing precisely. (b) is worse than nothing: dead code advertising a capability reads as "supported but untested" to anyone scanning the module, and the factory's presence implies session construction goes through it when it does not. (c) leaves one honest, implemented strategy and preserves the option with a written account of its real cost.
- Impact: `TenantConnectionFactory`, `connection_factory` and `get_db_with_rls` removed from `app/core/database.py`; no references existed elsewhere, and no `NotImplementedError` now remains in `app/`. `tenants.isolation_level` is kept — it costs nothing, is already populated, and records the intent a future implementation would read.
- What implementing either would require, if it is ever wanted: schema-per-tenant is the cheaper of the two and maps onto SQLAlchemy's `schema_translate_map` execution option, but needs schema creation and per-schema migration runs wired into tenant provisioning. Database-per-tenant additionally needs an engine cache keyed by tenant, a URL template, and a migration runner that iterates every tenant database; connection-pool sizing becomes a function of tenant count. Both also need the RLS policies and the ORM-level scoping to stay in place for tenants that remain on 'rls', since the strategies would coexist.
- Owner: bilal (dev)

**DR-015 — the last-admin guard is kept although currently unreachable**
- Date: 2026-08-06
- Context: `PATCH /users/{id}/role` refuses to demote the last active administrator. The branch cannot fire as the roles stand: only `admin` holds users.manage, and changing your own role is already refused, so the caller is always another admin and is always counted among the remaining ones. Its test stubs the permission gate to reach the code. Checked for other routes to the same lockout: no endpoint writes `User.is_active`, so demotion is the only way a tenant could be left without an administrator.
- Options: (a) delete the guard and its test as dead code; (b) keep it as defence in depth and state plainly in the code that it is currently unreachable and why.
- Decision: (b).
- Rationale: This is not the same case as DR-014. That removed a subsystem advertising capability it did not have — misleading in itself. This is correct code enforcing a real invariant that simply has no path to it yet, at a cost of six lines. The condition that makes it live is a single line elsewhere: granting users.manage to another role, which the Build Book's HR and procurement administration will plausibly do. Whoever grants it will be thinking about who may manage users, not about leaving a tenant with nobody who can administer it — which is unrecoverable through the API, since the only actor who could restore an admin is an admin. Deleting it optimises for a tidy coverage report over an irreversible failure.
- Impact: Guard retained in `app/api/users.py` with a comment recording that it is unreachable by construction today, the precise reason, and the change that would make it live. `tests/integration/test_auth_privilege_escalation.py::test_last_admin_cannot_be_demoted` monkeypatches `has_permission` to exercise it, and says so in its docstring. The accompanying test that an admin *can* be demoted while another remains is the one that runs against real permissions.
- Owner: bilal (dev)
