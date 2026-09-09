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

**DR-016 — a payment run carries its own chain rather than inheriting one**
- Date: 2026-08-11
- Context: Every other module joins a correlation chain by inheriting it from the record upstream — a goods receipt takes the purchase order's, an invoice keeps its own from upload. A payment run does not fit: it may settle several invoices belonging to several different chains, so there is no single chain to inherit.
- Options: (a) restrict a run to invoices sharing one chain; (b) give the run the chain of its first invoice; (c) give the run its own chain and write an audit entry onto each settled invoice's chain naming the payment.
- Decision: (c).
- Rationale: (a) breaks the actual business practice — an AP run pays many vendors at once, and forcing one run per chain makes the feature useless. (b) is a quiet lie: the run would appear inside one invoice's evidence pack and be invisible from the others, so an auditor pulling the second invoice's story would see it approved and never see it paid. (c) means the run appears in every story it touches without pretending to belong to one: its own chain covers prepare/release/export, and each invoice's chain gains a `marked_paid` entry naming the run.
- Impact: `Payment.correlation_id` is minted fresh in `prepare_payment`; `release_payment` writes a `marked_paid` audit entry against each settled invoice carrying that invoice's own chain. Payment is registered as a chain owner via OBJECT_TYPE, so its evidence pack and timeline work like any other module's.
- Owner: bilal (dev)

**DR-017 — no admin exemption on payment release**
- Date: 2026-08-11
- Context: The existing segregation-of-duties rules exempt admins (DR-005), so a single-admin demo tenant still functions — an admin may approve an invoice they created. Payment release needed the same decision.
- Options: (a) exempt admins for consistency with DR-005; (b) enforce maker-checker on release with no exemption.
- Decision: (b).
- Rationale: The DR-005 carve-out exists so a one-person tenant is operable, and its cost is bounded — a wrongly approved invoice is still subject to every downstream control, including this one. Release has no downstream control; it is the last gate before an instruction reaches a bank, and releasing your own run is precisely the action the module exists to prevent. Consistency is not worth an exemption on the only step that cannot be caught later. The cost is real and accepted: a genuinely single-user tenant cannot release a payment, which is the correct answer rather than a limitation.
- Impact: `sod.violates_self_release` deliberately has no `_is_admin` check, unlike its siblings. Verified against a running server: an admin who prepared a run was refused its release with 403, and a second user released it.
- Owner: bilal (dev)

**DR-018 — bank matching is a suggestion a human confirms, never automatic**
- Date: 2026-08-11
- Context: Reconciliation compares released payment runs against imported bank statements. Most of the matching is mechanical — an exact amount, our own payment reference echoed in the bank's narrative, a date two days later — and auto-matching those would clear the majority of a month's statement without anyone touching it.
- Options: (a) auto-match above a confidence threshold and leave the rest for review; (b) auto-match only on an exact reference *and* exact amount; (c) never match automatically — score, explain, and require a person to confirm each one.
- Decision: (c).
- Rationale: A wrong automatic match is strictly worse than no match. It does two things at once: it certifies a payment as cleared when it did not clear, and it consumes the statement line that was the evidence of something else — most likely the unexplained debit the module exists to surface. Both failures are silent and both remove the finding rather than flag it. (b) narrows the window but does not change the shape: a duplicate bank debit carrying the same reference and amount as its legitimate twin is exactly the case that must not be auto-cleared, and it is also the case a threshold rule matches most confidently. The cost of (c) is a human clicking confirm on obvious matches, which is the cheapest control in the system.
- Impact: `app/services/reconciliation.py` scores candidates and returns named reasons rather than a bare number, so a reconciler can interrogate a suggestion instead of rubber-stamping it; a differing amount returns no candidate at all rather than a low score, since it cannot be outweighed by a date and a name that happen to fit. `confirm_match` records `suggested_score`, `suggested_reasons` and `confirmed_against_suggestion` in the audit entry, so a match a human made against a weak suggestion — or against none — is findable later. `GET /bank-statements/reconciliation` returns both directions together, because a reconciler shown only outstanding payments never sees the debit nobody instructed.
- Owner: bilal (dev)

**DR-019 — the releaser may not reconcile their own payment**
- Date: 2026-08-11
- Context: Reconciliation needed its own segregation-of-duties rule. The candidates are the preparer, the releaser, both, or neither.
- Options: (a) no rule — reconciliation is clerical; (b) refuse the preparer; (c) refuse the releaser; (d) refuse both.
- Decision: (c).
- Rationale: Reconciliation is the check *on* the release, not a continuation of it. One person authorising money to leave and then certifying that the bank line explaining it is correct holds both the instruction and its evidence — which is precisely how a misdirected payment stays hidden. The preparer is deliberately not blocked: their work was already checked by a second person at release, so (d) adds a second control over an already-controlled step while removing the only people small teams have available to reconcile — the practical result being that nobody reconciles at all, which is worse than the risk it addresses.
- Impact: `sod.violates_self_reconciliation`, with no admin exemption for the same reason as DR-017. `bank_statements.reconcile` is a separate permission from `payments.prepare` and `payments.release`; the CFO holds view but not reconcile, since they release runs and the rule would refuse those anyway. A refused match is committed to the audit trail on its own, so the attempt survives although the action does not.
- Owner: bilal (dev)

**DR-020 — the database session is pinned to UTC; existing rows left untouched**
- Date: 2026-08-11
- Context: The other half of DR-012, found while verifying bank reconciliation against a running server. `utc_now()` returns a *timezone-aware* datetime, and Postgres converts an aware value into the session's zone before dropping the offset to fit a `TIMESTAMP WITHOUT TIME ZONE` column. On a server set to Asia/Karachi every application-written timestamp was therefore stored five hours off from every server-defaulted one. Confirmed on real rows: `payments.released_at` 06:20 against `created_at` 01:19 on a run created a minute earlier, and `audit_logs.timestamp` disagreeing with its own `created_at` on the same row. DR-012 corrected the server defaults and left this untouched, which is why the skew survived.
- Options: (a) wrap each write in `make_naive`; (b) give every timestamp column a `TypeDecorator` that normalises on bind; (c) pin the connection's timezone to UTC.
- Decision: (c).
- Rationale: (a) is ~40 call sites whose weakest point is the next one someone writes, and the failure is silent — a timestamp five hours out type-checks, serialises and renders without complaint. (b) is structurally sound but a large diff across every model, and a column declared with plain `DateTime` by mistake would silently opt out. (c) is one line at the single place every write already passes through, and it states the intended property directly: a database session in this project speaks UTC. Same argument as DR-013.
- Impact: `ENGINE_CONNECT_ARGS` in `app/core/database.py`, exported so `tests/integration/conftest.py` builds its engine identically — a test engine without the pin would silently pass timestamps the real one corrupts. `tests/integration/test_timestamp_defaults.py::TestApplicationWrittenTimestampsAreUTC` covers the round trip, the session setting, and the production engine's configuration; all three were verified to fail with the pin removed.
- Residual risk: rows written before this change hold local time in the affected columns. As with DR-012 the affected databases hold demo data only, and `audit_logs.timestamp` is part of the hash-chain payload, so it must not be rewritten while chain verification is expected to pass.
- Owner: bilal (dev)

**DR-021 — demo seed data is opt-in; a deployment bootstraps its own administrator**
- Date: 2026-08-12
- Context: The migrations had never been run against an empty database — every developer and test database in this project is built with `create_all`, so `alembic upgrade head` had no proven behaviour anywhere. Running it on a fresh database for the first time showed migration 002 creating five active accounts, including `admin@demo.com` with role ADMIN, all sharing the password `password123` written in the migration file. On a deployed server that hands full administrative control to anyone who can reach the login page. The same run also found `delegations` to be the one tenant-owned table with no RLS policies at all, and migration 008 configuring workflow and approval policy for the demo tenant specifically.
- Options: (a) delete migration 002 and 008's demo config; (b) leave them and rely on a deployment runbook saying "remember to delete the demo users"; (c) gate the seed behind an explicit environment flag and give deployments a separate bootstrap path.
- Decision: (c).
- Rationale: (b) is not a control — it is a note, and the failure mode is a live administrator account with a published password on a system that approves and releases payments. (a) throws away something genuinely useful: the demo tenant is how local work gets a populated database in one command. (c) keeps that and makes the dangerous case require an explicit act. The flag is read from the process environment rather than from application settings on purpose, so a stray value in a `.env` file cannot switch it on. Editing an applied migration would normally be forbidden; here nothing had ever applied it, so there was no history to rewrite.
- Impact: `002_seed_demo_data` returns early unless `SEED_DEMO_DATA` is set, printing why. `008_seed_config_policies` returns early when the demo tenant does not exist, since its inserts would otherwise fail on a foreign key. `024_delegations_rls` adds the missing ENABLE + FORCE RLS and both tenant policies to `delegations` — it matters more than the omission suggests, because a delegation is a grant of someone else's approval authority. `scripts/bootstrap_tenant.py` creates the first tenant and administrator from environment variables, refuses to run against a database that already has users, refuses a password under 12 characters, and provisions the tenant's workflow and approval config. `tests/integration/test_rls_isolation.py` and `test_conversation_rls.py` now honour `TEST_DATABASE_URL`; they previously read from a hardcoded database, so running the suite against a migrated one silently compared the wrong thing.
- Verified: `alembic upgrade head` on an empty database produces the schema at head with zero rows and RLS on all 22 tenant-owned tables; the full suite passes 530/530 against that migrated database as well as against the usual `create_all` one; 024 downgrades and re-upgrades cleanly; the bootstrap script refuses a second run and a short password. Remaining migration-versus-model drift is recorded in the README and is all the database being wider or stricter than the models.
- Owner: bilal (dev)

**DR-022 — the API test client binds a tenant, as the real dependency does**
- Date: 2026-08-12
- Context: Isolation was checked by standing up two tenants on a production-shaped database — migrated schema, RLS enabled, the app connecting as the non-bypass `os_app` role — and probing 116 read, write and reference-injection paths as each tenant's *administrator*. Nothing leaked. Writing the same checks as regression tests, however, produced fourteen failures against the code that had just passed live. The cause was the shared `client` fixture: it overrode `get_db_session` with a bare session, while the real dependency calls `set_tenant_context` first. Every API test in the suite had therefore been running with tenant scoping switched off, and any test asking "can this caller see another tenant's rows?" would have passed just as happily against a server with no isolation at all.
- Options: (a) bind the tenant only in the new cross-tenant tests; (b) make the shared fixture mirror the real dependency.
- Decision: (b).
- Rationale: (a) leaves the blind spot in place for every other API test and for every one written later — and the blind spot is invisible, because a test that cannot fail looks exactly like a test that passes. The fixture existed to make the HTTP contract testable; a fixture that disables the request's most important behaviour is not testing that contract. The cost was one pre-existing assertion that read a row back after the request and now has to say which tenant it is reading as, which is a fair thing to have to state.
- Impact: the `client` fixture resolves the authenticated user from `app.dependency_overrides` and binds that tenant, exactly as `get_db_session` does. `tests/integration/test_cross_tenant_api.py` covers list exclusion, fetch-by-known-id, the absence of an existence oracle, writes against another tenant's records, reference injection through the request body, and a validly signed token whose tenant claim is not the user's own — that last one runs the real authentication dependency rather than overriding it, since overriding it would test the scoping with the check it depends on switched off.
- Owner: bilal (dev)

**DR-023 — two isolation findings from the cross-tenant probe, neither a leak**
- Date: 2026-08-12
- Context: The probe found no cross-tenant data disclosure in 116 attempts, and no write of any kind succeeded. It did find two things worth correcting. `GET /purchase-orders/{id}/receipts` answered `200 []` for another tenant's order where every sibling endpoint answers 404: the service listed receipts by `purchase_order_id` without ever loading the order, so the isolation came from the receipts happening to be tenant-scoped rather than from checking the order. And `PaymentService.prepare_payment` flushes the run header before validating the invoices — a mixed request naming one payable invoice and one belonging to another tenant is refused, but leaves a headerless run in the session.
- Decision: fix both, although neither leaked.
- Rationale: the receipts endpoint was safe by accident, and the accident is one refactor from ending — a query that never looks at its parent object states no relationship between them, so nothing warns the next person who changes how receipts are scoped. The payment header is a similar shape: nothing persisted, because the request-scoped session closes without committing, but that is the session lifecycle providing the guarantee rather than the function, and it silently stops being true the moment anything commits earlier on that path. Verified empirically both before and after: no orphan payment row existed in the two-tenant database after the probe ran.
- Impact: `GoodsReceiptService.list_for_order` loads the order first and raises "not found" when it is invisible, so the endpoint now 404s like its siblings. `PaymentService.prepare_payment` rolls back explicitly when preparation fails partway. `app/api/vendors.py` maps "not found" to 404 rather than 400 — the same information either way, since another tenant's vendor is indistinguishable from one that does not exist, but the inconsistency invited someone to read meaning into the difference.
- Owner: bilal (dev)

**DR-024 — registration cannot choose a role, and is closed by default**
- Date: 2026-08-12
- Context: Found while enumerating what the cross-tenant probe had *not* covered. `/auth/register` is unauthenticated and takes a tenant slug as a query parameter; its body was typed as `UserCreate`, which inherits `role` from `UserBase`. The endpoint assigned `user_in.role or DEFAULT_ROLE`. Posting `{"role": "admin"}` at any existing tenant slug therefore returned 201 with an administrator's access token. Verified: status 201, role `admin`, working token. This is the same defect class as the `PUT /auth/me?role=admin` escalation fixed earlier, on a path that needs no authentication at all — and it defeats every isolation control in the system without bypassing any of them, because it enrols the attacker into the target tenant legitimately.
- Options: (a) validate the requested role against a whitelist; (b) ignore the role field and always assign the default; (c) remove the field from the request model and close self-registration unless a deployment opts in.
- Decision: (c).
- Rationale: (a) does not address it — `admin` is a valid role, which is exactly the problem. (b) fixes the escalation but leaves a field the API accepts and silently discards, which reads as an oversight to the next person and invites them to wire it back up; it also leaves open enrolment in place, and even at the default clerk role a stranger can create vendors, raise invoices, prepare payment runs and import bank statements. An accounts-payable system enrols staff; it does not take walk-ins. Closing registration required somewhere for accounts to come from, so the two halves land together.
- Impact: `RegistrationRequest` carries email, password and full_name only; the role assignment in `/auth/register` is unconditional. `ALLOW_SELF_REGISTRATION` defaults to False and the endpoint 403s when it is unset. `POST /users` creates an account in the caller's tenant behind users.manage, with a 12-character password minimum, and audits the role granted. `tests/integration/test_auth_privilege_escalation.py` asserts the requested role is not honoured, that the returned token is not an administrator's, that registration is refused while closed, and that the new endpoint is permissioned and audited.
- Frontend: the signup form does not send a role, so nothing there breaks — but it will receive 403 against a deployment that has not opted in, and shows the endpoint's message. Whether to keep a public signup page at all is a product decision left open.
- Owner: bilal (dev)

**DR-025 — two findings from probing the surfaces the first cross-tenant sweep missed**
- Date: 2026-08-12
- Context: The first sweep covered lists, fetch-by-id and writes. This one covered what it had not: the conversation endpoints including the natural-language query agent, query-parameter filters, evidence-pack *generation* rather than preview, config versioning and rollback, and the tenant-wide writes (autopilot, SLA escalation). 44 checks, no data leak in either direction. The query agent is the interesting one: asked plainly for the other tenant's invoices it did execute a query — `query_invoices`, `sql_executed: true` — and returned zero rows, and an unrestricted "how many invoices are there in total" returned only the caller's. Restoring another tenant's approval policy was refused. Two other things surfaced.
- Finding 1 — provisioning could leave a tenant unconfigured. `ConfigProvisioningService` decided whether a tenant was already configured with `workflow_repo.count_states(...)` and `policy_repo.list_by_type(...)`, neither of which names a tenant; both rely on the session's bound tenant. Provisioning runs where nothing is bound — a setup script, an onboarding job — and there the check saw the *previous* tenant's rows, concluded the work was done, and seeded nothing. Reproduced by provisioning two tenants in one script: the second came up with zero workflow states and zero approval policies, so every routing decision would silently fall back to the hardcoded defaults and the configuration screens would be empty. Not a leak, but a tenant onboarded that way is running on rules nobody chose. Fixed with explicit `tenant_id` filters; idempotence for a genuinely configured tenant is unchanged and still tested.
- Finding 2 — evidence packs were sealed over nothing. Generating a pack for another tenant's correlation id returned 200 with `objects: 0` and `all_chains_verified: true`, hashed and permanently recorded. Nothing leaks — the pack is empty precisely because the caller cannot see those records — but a sealed evidence pack exists to be pointed at later, and one that certifies an absence is worse than an error. Generation now refuses when the chain has no visible objects, and the endpoint maps that to 404 rather than letting the ValueError become a 500. Preview is unchanged: previewing nothing is harmless, sealing it is not.
- Note on method: two of the probe's own results were wrong in ways worth recording. Its first pass at the query agent sent the wrong field name, so the request 422'd before reaching the agent and scored a pass; and its "did the agent actually run?" check compared a needle containing a space against a space-stripped body. A probe that cannot fail is indistinguishable from one that passes, which is the same trap the `client` fixture fell into (DR-022).
- Owner: bilal (dev)

**DR-026 — autopilot's tenant boundary is tested by actually running it**
- Date: 2026-08-12
- Context: `POST /autopilot/run` is the widest write in the system: it approves every eligible invoice it finds in a single call, so a missing tenant boundary there does not leak one record, it approves another company's payables. Both earlier cross-tenant probes recorded it as fine, and both were wrong to — the endpoint refuses to run unless autopilot is enabled, and neither probe had enabled it, so both scored a pass on a 400. The scan (`repository.get_pending_with_vendor`) names no tenant and relies on the session's bound one, which is the same shape as the provisioning defect in DR-025, so reasoning about it was not good enough.
- What was done: enabled autopilot for *both* tenants on a production-shaped two-tenant database, gave each an identical eligible invoice, and ran it as one tenant. Alpha's run approved `Alpha-AUTO-001` and left `Beta-AUTO-001` in pending_approval with a null `approved_by`; Beta's run did the mirror image. The preview showed one candidate rather than two. Reverting the other tenant's auto-approval was refused. Verified in the database, not only in the response — an approval that happened but went unreported is the worse outcome.
- Decision: no code change needed; the boundary holds. Pinned with tests rather than left to a one-off probe.
- Impact: `TestAutopilotActsOnlyOnItsOwnTenant` in `tests/integration/test_cross_tenant_api.py` enables autopilot for both tenants in the fixture, so the class cannot repeat the mistake of passing on a refusal — the run assertion requires the caller's own invoice to appear in the approved list, which fails if nothing executed.
- Also closed: file storage. Uploads are written to `UPLOAD_DIR/<tenant_id>/` and no endpoint serves them; the only file-returning route (`/payments/{id}/bank-file`) builds its content in memory and was already covered. Confirmed by enumerating every route in the OpenAPI document rather than by reading the storage module and inferring it.
- Owner: bilal (dev)

**DR-027 — autopilot cannot approve beyond the authority of whoever runs it**
- Date: 2026-08-12
- Context: Found while explaining what `POST /autopilot/run` actually does. It approves every eligible pending invoice in one call by calling `transition_state` directly rather than going through `InvoiceService.approve_invoice`, so it never consulted the approval matrix. Autopilot's own ceiling (`max_auto_approve_amount`) is configured by an admin and is unrelated to what the person clicking run may approve. Reproduced against the service: `can_approve_amount("manager", 900_000)` refuses with "can only approve invoices up to 250000", and the same manager's autopilot run approved that invoice and recorded `approved_by` as them. The audit trail then asserts a manager approved something the matrix denies them, which is worse than a silent gap — it is a false record of who held the authority.
- Options: (a) restrict who may run autopilot to unlimited approvers; (b) cap the configured amount at configuration time against the configuring admin's limit; (c) evaluate each candidate against the running user's own approval limit, reusing the matrix rule.
- Decision: (c).
- Rationale: (a) removes the feature for the role most likely to use it daily. (b) checks the wrong person — the admin who sets the ceiling is rarely the one who clicks run, and the ceiling is a policy about invoices, not about a user. (c) makes the two paths agree by construction: whatever a role may approve by hand is exactly what it may approve here. `can_approve_amount` is reused rather than reimplemented so the two cannot drift.
- Impact: `_evaluate` takes `current_user` and returns the matrix's own refusal message, so the preview explains the exclusion rather than silently dropping the candidate. Note this also makes the `approver` role approve nothing through autopilot, matching the manual path, where `approver` holds the permission but has no entry in `APPROVAL_LIMITS` and is refused any amount. `TestAutopilotCannotExceedTheRunnersOwnAuthority` covers the refusal, the preview's reason, that a within-limit invoice still runs, and that an unlimited approver is unaffected; the first two were verified to fail with the check removed.
- Owner: bilal (dev)

**DR-028 — the requisition, not the purchase order, is the first record in the chain**
- Date: 2026-08-12
- Context: Procure-to-pay was complete from the purchase order onward. The order was therefore the first record, so an approver had nothing upstream to check it against, and the audit trail could prove an order was properly approved without ever answering why it was ordered at all. Every control downstream — three-way matching, maker-checker on payment, reconciliation — verifies that money followed the order faithfully; none of them verifies the order should have existed.
- Decision: build the upstream half — requisition, RFQ, quotes, award, conversion — with the requisition minting the correlation id that everything downstream inherits.
- Rationale for the specific controls, each chosen against a known failure:
  - **A requisition names no vendor.** It states a need. Naming a supplier there would let the requester pre-select the winner before anyone has quoted.
  - **The requester cannot approve their own**, and the approval matrix's amount thresholds apply — reusing `can_approve_amount` rather than reimplementing it, so the rule that governs invoice approval governs authorising a request to spend and the two cannot drift.
  - **An RFQ requires an approved requisition.** Going to market on an unapproved need commits the company's name to a purchase nobody authorised.
  - **Issuing requires at least two invited vendors.** One quote is not a comparison. Single-source buying is legitimate but is a different decision and should be raised as a direct order rather than dressed as a tender.
  - **Quotes lock when the RFQ closes** — for everyone, including the buyer who captured them. A quote that can be edited once the field is known is not a quote, and back-dating a losing bid downwards is the cheapest way to make a rigged award look competitive. Closing snapshots the field into the audit entry.
  - **Running the tender and awarding it are separate permissions.** The buyer who collects the quotes must not decide which wins; as shipped the clerk holds `sourcing.manage` and the manager/CFO hold `sourcing.award`.
  - **Awarding anything but the lowest compliant quote requires a written reason**, stored with the figure it beat. This is the single most examined decision in procurement, and "compliant" is judged explicitly so that a cheaper bid for the wrong specification does not silently become the benchmark every legitimate award has to justify itself against.
  - **The order may not exceed the approved estimate**, and converting marks the requisition converted so one approval cannot cover two orders. The market coming back higher is normal — it needs the requisition re-approved rather than quietly absorbed.
- Impact: `purchase_requisitions`, `rfqs`, `rfq_vendors`, `quotes`, `quote_lines` and lines tables, migration `025`; `requisition` and `rfq` workflows added to the seeded defaults with four new transition guards; five new permissions split across the roles. Verified end to end over HTTP against a freshly migrated database, each step performed by the role that holds the authority for it, with every control demonstrated failing closed.
- Also fixed: correlation chains resolved a record's display reference through a hardcoded chain of `getattr` fallbacks that every new module had to remember to extend — and payments had already not been, so a payment appeared in its own story as a raw UUID. Each model now declares `REFERENCE_FIELD`, and a test fails if a chain owner does not.
- Not built: budgets as records (a requisition carries a free-text `budget_code`, which is what a reviewer checks against but is not enforced against an allocation), and contract-based purchasing.
- Owner: bilal (dev)

**DR-029 — new workflows are backfilled for existing tenants by migration**
- Date: 2026-08-12
- Context: `ConfigProvisioningService` seeds workflow states per workflow type precisely so a tenant provisioned before a workflow existed can pick it up on the next run — but nothing runs it again on its own. Adding the `requisition` and `rfq` workflows therefore left every existing tenant able to *create* requisitions and unable to *move* them: the first submit failed with "No workflow configured for 'requisition' state 'draft'". Found by using the new screens against the development tenant, which predates them. A fresh deployment is unaffected, so this would have shipped looking correct and been broken for exactly the customers who already had data.
- Options: (a) document that administrators must re-run `POST /config/initialize-defaults` after upgrading; (b) auto-seed a missing workflow lazily on first use; (c) backfill in a migration.
- Decision: (c).
- Rationale: (a) makes a silent runtime failure the customer's problem to know about in advance, and the failure surfaces as a 400 in the middle of somebody's work rather than at deploy time. (b) means a write path can silently create configuration, which is exactly what the configuration-first design exists to prevent — config should be seeded deliberately and versioned, not conjured by whoever happens to click first. (c) puts it where the rest of the schema evolution lives and runs once, at the moment the code that needs it arrives.
- Impact: `026_backfill_new_workflows` seeds any workflow type a tenant is missing, leaving every existing state untouched so a tenant that has edited its own workflow keeps it. It imports `DEFAULT_WORKFLOWS` rather than copying it, so it cannot drift from the canonical defaults. `downgrade` deliberately does nothing: there is no way to tell a state it inserted from one a tenant has since edited, and dropping a workflow mid-flight would strand every record in it. Verified on a database migrated to 025 with a bootstrapped tenant whose new workflows were removed to mimic an existing customer — 026 seeded exactly the two missing ones, and re-running reported "every tenant already had every workflow" and added nothing.
- Note for future modules: adding a workflow type to `DEFAULT_WORKFLOWS` is not enough on its own. Existing tenants need a backfill migration like this one, and the omission is invisible until someone tries to move a record.
- Owner: bilal (dev)

**DR-030 — autopilot applies maker-checker, closing the last gap in its approval path**
- Date: 2026-08-12
- Context: The companion to DR-027. Autopilot calls `transition_state` directly rather than going through `InvoiceService.approve_invoice`, so it consulted neither of the two checks that path makes. DR-027 restored the approval-matrix limits; this restores segregation of duties, which it also skipped: nothing in autopilot refused approving an invoice the person running it had created.
- Reachability, stated plainly because it decides how much this is worth: it cannot fire as the roles stand. Only `admin` holds both `invoices.create` and `invoices.approve`, and DR-005 exempts admins from SoD anyway, so the manual path would allow it too. It goes live the moment any other role gains both — which the Build Book's HR and procurement administration roles plausibly will.
- Decision: add the check now rather than when it becomes reachable.
- Rationale: the same argument as DR-015. Whoever eventually grants `invoices.create` to an approving role will be thinking about who may raise invoices, not about a bulk-approval path that silently skips maker-checker — and they will have no reason to look here. The cost is four lines and a reused rule; the failure it prevents is an approver auto-approving their own invoices in bulk, which is precisely the shape maker-checker exists to stop. Reusing `sod.violates_self_invoice_approval` rather than reimplementing it also means the admin carve-out behaves identically on both paths, so autopilot is neither stricter nor laxer than approving by hand.
- Impact: `_evaluate` calls the SoD rule and returns its refusal as the candidate's reason, so the preview explains the exclusion rather than dropping the row silently. `TestAutopilotRespectsMakerChecker` creates the condition deliberately — a manager is granted `invoices.create` for the duration — and covers the refusal, the preview's wording, that someone else's invoice still runs, and that an admin remains exempt. The first two were verified to fail with the check disabled.
- With DR-027 this closes both gaps between autopilot and the manual approval path: whatever a role may approve by hand, and only that, is what autopilot will approve for them.
- Owner: bilal (dev)

**DR-031 — misconfiguration stops the deploy rather than serving something forgeable**
- Date: 2026-08-14
- Context: Preparing the first deployment surfaced three settings that were safe locally and dangerous anywhere else. `DEBUG` defaulted to True, and the 500 handler returns the exception type and message when it is on. `SECRET_KEY` had a working default published in a public repository — every access token is signed with it, so a deployment that inherited it could have tokens forged by anyone who read the source, including one claiming an administrator's id. `CORS_ORIGINS` defaulted to localhost, which is not dangerous but means the deployed frontend is refused by the browser.
- Options: (a) document the required values in a runbook; (b) warn at startup; (c) refuse to start.
- Decision: (c) for the secret and the origins, with `DEBUG` defaulting to False.
- Rationale: the failure mode is what decides this. A deployment with the placeholder secret *works* — it serves traffic, signs in users, and behaves correctly right up until someone mints their own admin token, at which point nothing in the logs distinguishes them from a real administrator. A runbook cannot help with a defect that is invisible while it is happening, and a warning is a line in a log nobody reads on a green deploy. Refusing to start puts the failure at the only moment somebody is definitely watching. The checks are skipped when DEBUG is on, so local development is unaffected.
- Impact: `DEBUG` defaults to False; a model validator refuses the placeholder secret, a secret under 32 characters, and a localhost origin whenever DEBUG is off. `Dockerfile`, `render.yaml`, `.dockerignore` and `.env.example` added; the README carries the runbook.
- Found while testing it: `CORS_ORIGINS` accepts a comma-separated string, because that is what a hosting provider's environment box produces — but pydantic-settings JSON-decodes list fields *inside the settings source*, before any validator runs, so the tolerant parser never saw the value and `CORS_ORIGINS=https://x` died with a raw JSON error. Fixed with `NoDecode`. Worth recording because the validator looked correct and was never reached; only running it with a realistic value showed otherwise.
- Frontend: `API_BASE_URL` was hardcoded to `http://127.0.0.1:8000/api/v1`, so a deployed build could not reach a deployed API at all. Now `NEXT_PUBLIC_API_BASE_URL`, with the localhost value kept as a fallback so local development needs no setup.
- Hosting: Neon for Postgres (real Postgres, and its free tier does not expire — Render's free database is deleted after 30 days), Render for the API, Vercel for the frontend. Nothing depends on those choices; the Dockerfile is plain and every setting comes from the environment.
- Owner: bilal (dev)

**DR-032 — a vendor's bank details change by request, not by edit**
- Date: 2026-08-14
- Context: Build Book A1 names the control — *vendor bank change verification with dual approval and cooling period policy* — and `sod.py` had listed the rule as a follow-up since the module was written. It was exploitable, not theoretical: `PATCH /vendors/{id}` wrote bank fields directly behind `vendors.manage`, which the AP clerk holds, and `prepare_payment` copies those details onto the payment line. One person with a clerk account could redirect a legitimate approved invoice to their own account, and the releaser would see a vendor name and an amount. The audit entry for a vendor update recorded only `legal_name`, so the change was not merely uncontrolled but invisible.
- Why the existing controls do not catch it: every downstream check verifies that money followed the order faithfully, and here it does. The invoice is real, the approval is real, maker-checker on the release is satisfied by a genuine second person releasing a genuine run. Only the destination changed. Reconciliation would eventually show a payment cleared to a counterparty that does not match — detection after the money has gone.
- Decision: bank fields cannot be edited directly. They move through a request that a second person approves, followed by a cooling period before any payment may use them.
- Rationale for each part:
  - **Old values snapshotted on the request**, because the substitution is what a reviewer is judging, and once applied the vendor row no longer holds what it replaced.
  - **A separate permission** (`vendors.approve_bank_change`) held by manager/CFO/admin and deliberately not by the clerk who maintains vendors — whoever keeps vendor records is exactly who would make this change.
  - **No admin exemption on the SoD rule**, unlike invoice approval and vendor activation. Those carve-outs exist so a one-person tenant functions and their cost is bounded by downstream controls; this one has no downstream control, and the carve-out that keeps a one-person tenant working would keep a one-person fraud working.
  - **Approval and application are separate steps.** Approving and applying together would make the cooling period a comment rather than a control.
  - **Payments held to either account while a change is open.** If the change is fraudulent the old account may already be compromised; if it is genuine the vendor is expecting the new one. Holding is the only answer that is right in both cases. Enforced at preparation and re-checked at release, because a run can wait days and the line already carries the details copied when it was prepared.
- Impact: `vendor_bank_changes` table and migration `027`; `VendorBankService`; `sod.violates_self_bank_change_approval`; `VENDOR_BANK_CHANGE_COOLING_HOURS` (24h, an operator setting rather than tenant config — a fraud control whose timing a tenant can edit is one an attacker with a tenant login sets to zero); six endpoints; `update_vendor` refuses bank fields and now audits every changed field rather than only the name. 22 tests including the full fraud attempt end to end; five were verified to fail with the SoD rule and payment gate disabled.
- Found while building it: SQLAlchemy's `Enum` stores the member *name* (`PENDING_APPROVAL`), not the lowercase value the Python code compares against — it translates on the way in and out. The partial unique index was first written with a lowercase predicate, which matches nothing, meaning the constraint would have existed in name only. Both the model and the migration now use the stored form.
- Owner: bilal (dev)

**DR-033 — the Decision Inbox reads every module, and asks the SoD rules rather than restating them**
- Date: 2026-08-17
- Context: the Build Book names one inbox across all departments as a core differentiator, and "supports every work item type in the variant" as a Definition of Done item. The service read invoices only — that was all that existed when it was written. Requisitions, tenders, orders, payment runs, bank changes and reconciliation breaks were each built afterwards and none reported to it, so the product's central claim was true of one module out of eight and a manager with four approvals waiting saw an empty screen.
- Decision: each module contributes a collector returning one neutral shape (`object_type`/`object_id`/`reference`/`subtitle`/`detail_url`). A new module joins by adding one function; the reader never changes. Both links come from the item, because building them client-side meant one route per category and every new module silently sent the reader to an invoice page.
- Precedence: unexplained debit (0) → open bank change (1) → duplicate (2) → vendor (3) → payment release (4) → approvals. The ordering is a claim about what is most costly to leave: money that already left without an instruction beats money about to leave, which beats a commitment, which beats a request. Breached SLAs still sort above everything.
- Found while building it, in rough order of how quietly each would have failed:
  - **The endpoint 500'd on every non-empty inbox.** The response model still required `invoice_id`. The whole suite stayed green because the service tests bypass the API and the one API test that hit this path had an empty inbox — and an empty list validates against any item schema. Only opening the page in a browser surfaced it. There is now an API test that asserts items exist before checking the contract.
  - **`notify_sla_escalation` assumed an invoice** and read `invoice_number` off whatever it was handed. For every other workflow it raised, and its own `except` logged the failure and moved on: the audit trail recorded the breach as escalated to the CFO while no message was ever sent. Nothing would ever have surfaced this, so the test asserts on what was sent rather than on the absence of an exception. `describe_record`/`record_reference` now read the `OBJECT_TYPE`/`REFERENCE_FIELD` the models already declare, which is the same rule the correlation chain had privately.
  - **SLA escalation was not idempotent for anything but invoices.** The runner scans every declared workflow, but the once-per-state-entry check queried audit rows with `object_type == "invoice"` hardcoded while the escalation writes the workflow type. Every run re-escalated the same overdue requisition and re-notified the approver, indefinitely.
  - **The inbox restated the SoD rules instead of asking them.** An inline `created_by == caller` hid a requisition from the admin who raised it — but `violates_self_approval` exempts admins precisely so a one-person tenant functions, and that admin may approve it. The item stalled with nothing on screen explaining why. The rules that deliberately have no admin exemption (release, bank change) are the mirror case, and one blanket comparison gets one of the two wrong whichever way it is written. Every collector now calls `sod`.
- Impact: `decision_inbox_service.py` rewritten as seven collectors; `app/utils/records.py`; `schemas/inbox.py`; `sla_service.py`; `notification_service.py`; frontend types, nine category badges, and links driven by `detail_url`. 15 inbox tests + 2 SLA tests; each was verified to fail with its fix removed, and the API contract test was verified to fail against the old schema.
- Owner: bilal (dev)

**DR-034 — a vendor's account number is a credential, and whoever changes it does not approve the payment that follows**
- Date: 2026-08-18
- Context: two halves of the same control, both named in the Build Book. Line 113: *field-level masking for sensitive fields (bank accounts, national IDs)*. Line 193: *same person cannot change vendor bank details and approve the first payment after change*. DR-032 made *changing* bank details a dual-approved act with a cooling period, while *reading* them stayed open and the window *after* a change was unguarded — so the control covered the middle of the fraud path and neither end.
- **Masking.** Five roles hold `vendors.view`, including the read-only auditor — the account most easily obtained and the one whose compromise looks harmless — and the response carried the full IBAN. That is precisely the reconnaissance a payment redirection needs. `vendors.view_bank_details` now gates the full value, held by the roles that act on payment details (clerk maintains vendors and prepares runs; manager and CFO approve changes and must compare old against new) and deliberately not by the auditor, who needs to see *that* details changed — which the audit trail gives them — not the live credential. Masked values keep the last four so a reviewer can still tell two accounts apart; values of six characters or fewer are hidden entirely, since revealing four of six gives away most of it.
  - The same identifiers sat on **three** surfaces: the vendor record, the bank change (old and new together, which is worse), and the destination copied onto each payment line, reachable with `payments.view` which the auditor also holds. Masking one and not the others just moves the leak, so the tests end with a sweep across every endpoint a read-only role can reach that fails if a full value appears anywhere — verified by granting the auditor the permission and watching it name all three.
  - Not enforced in the service layer: the bank file generator and payment preparation legitimately need real values, and a service that masked by default would either break them or grow an exemption argument at every call site. Masking is applied where the role is known and the value leaves the system.
- **The first payment after a change.** DR-032 holds payments while a change is *open*, which is the window before anyone has agreed to it; afterwards is when the money actually moves. Someone who requests a change, gets it approved by a colleague who glances at it, waits out the cooling period and then releases the first run to that vendor has completed the redirection with every control formally satisfied — maker-checker included, since a different person prepared the run. The second signature at approval only means something if it is not the same person again at the payment.
  - Both the requester and whoever applied it count as having changed the details: applying needs only `vendors.manage`, so it can be a third person, and either of them choosing the destination and then authorising the money is the same conflict. This required a new `applied_by` column (migration `028`) — the record named the requester and the approver but not who wrote the account onto the vendor.
  - Enforced at **release only**, not preparation. The Build Book says "approve the first payment", and release is that step; barring preparation would stop the clerk who maintains vendor records from preparing anything for that vendor, which breaks ordinary work to no extra benefit — a second person still authorises.
  - "First" is measured against **releases**, not preparations: a run prepared and rejected paid nobody, so it does not discharge the rule. The restriction lifts by itself once one payment has gone out with somebody else's signature. It is a gate on one moment, not a standing ban.
  - No admin exemption, consistent with the rest of DR-032. The admin is in fact the only role today holding both `vendors.manage` and `payments.release`, so exempting them would exempt the only person who can currently walk this path.
- Impact: `PERM_VIEW_BANK_DETAILS`; `app/utils/masking.py`; `for_user` on `VendorResponse`, `BankChangeResponse` and `PaymentResponse`; ten endpoints rewired; `sod.violates_first_payment_after_bank_change`; `PaymentService._first_payment_after_bank_change`; migration `028`; frontend payment page states when a destination is masked. 22 new tests; each was verified to fail with its fix removed, and the masking sweep was verified to name all three surfaces.
- Owner: bilal (dev)

**DR-035 — records are withdrawn, not destroyed**
- Date: 2026-08-18
- Context: Build Book non-negotiable — *immutable audit: guardrails to prevent hard deletes*. Deleting a vendor, a draft invoice or an approval policy removed the row while the audit entry describing the deletion stayed behind, pointing at an id that no longer resolved. The trail recorded that something happened to something the database says never existed. For policies it was worse than a dangling reference: `config_versions` keeps a snapshot per change *including the deletion itself*, so the rollback this system offers could restore a version of a policy whose row was gone. For vendors, the bank-change history recorded against them (DR-032/034 — who moved that account, and when) survived with nothing to attach it to.
- Decision: `SoftDeleteMixin` carrying `deleted_at`/`deleted_by`/`deletion_reason`. Carrying the mixin does two things automatically, so nothing depends on a service remembering: withdrawn rows drop out of every ORM SELECT via `with_loader_criteria`, and `session.delete()` on the model raises.
- Rationale for each part:
  - **The global filter, not per-repository filtering.** Same mechanism and the same argument as the tenant scoping in DR-013: one rule applied centrally beats ~40 call sites remembering a filter, and the one that forgets is the one that shows a withdrawn vendor in a payment run. Unlike tenant scoping it is *not* conditional on a bound tenant — that filter is a safety net over RLS, which enforces it in the database; there is no second mechanism behind this one.
  - **`before_flush` refuses hard deletes** rather than trusting convention. The Build Book asks for a guardrail, and a guardrail that only exists in the three services that happen to call `withdraw()` is a convention. The error names the alternative, because whoever hits it is trying to do something reasonable.
  - **A reason is mandatory** — and at the API, at least ten characters. A deletion is the one event nobody can reconstruct from what is left: every other action leaves the changed record behind to be read, and this one leaves an absence. "x" satisfies "required" and explains nothing.
  - **Sent as a body on DELETE, not a query parameter.** A reason is free text a person writes, and free text does not belong in a URL where every proxy in between logs it.
  - **Orphaned uploads are still destroyed.** `file_service.delete_file` runs only when OCR or invoice creation failed *after* the file was saved — a file that never became evidence of anything. Soft-deleting there would leave a row and a file on disk forever with nothing that would ever clean either up. A test pins that it remains reachable from exactly one call site.
- Impact: `SoftDeleteMixin`; `_exclude_soft_deleted` + `_refuse_hard_deletes` + `include_deleted()` in core.database; `services/soft_delete.py`; three services and three endpoints now take a reason; migration `029`. 16 tests, verified to fail with the exclusion filter disabled.
- Owner: bilal (dev)

**DR-036 — the change watchlist, and why notifications became opt-in**
- Date: 2026-08-18
- Context: Build Book differentiator — *vendor bank changes, master data edits, and policy overrides trigger real-time alerts to a watchlist role.* All three were already audited, and that is not the same thing. An audit trail answers "what happened to this record" for somebody who has already decided to look at that record, and none of these three give anyone a reason to look. They share the property that makes that dangerous: each changes where money goes, or who may authorise sending it, **without touching a single invoice**. Somebody watching invoices sees nothing at all until a payment lands somewhere new, and by then it has landed.
- Decision: a `watchlist_alerts` table written by the three trigger points, with delivery to a watchlist role resolved by permission.
- Rationale for each part:
  - **Rows, not only email.** An alert nobody can list is an alert nobody can prove they reviewed. The acknowledgement, with a note, is that evidence.
  - **Whoever caused the change cannot acknowledge it.** The alert exists to put a second person in front of the change; self-acknowledgement would let the one action the watchlist is for clear its own flag. Same shape as the SoD rules, and for the same reason.
  - **The author is excluded from the email.** Telling somebody about their own action is noise, and noise is what stops people reading the alerts that matter.
  - **`watchlist.receive` held by admin, CFO and auditor** — deliberately not by the clerk and manager who maintain vendors and policies. They are the subjects of the watchlist, not its audience. Making the role tenant-configurable is a follow-up; today it is a permission like every other recipient rule here.
  - **Account numbers are masked in the alert too** (DR-034), because the auditor is on this list and cannot see full account numbers anywhere else.
  - **Raising an alert can never fail the action it describes.** It is a parallel observation, not a step: a bank change that was correctly requested and approved must not roll back because the telling failed.
- Found while building it: widening the triggers exposed something that was always true and had simply never mattered. **Every notification is sent synchronously, inside the request that triggered it**, with a 10-second socket timeout and its exception swallowed. That was tolerable while notifications fired only on invoice submit/approve/reject; the watchlist fires on ordinary vendor edits, which put a mail-server connect in the path of a routine save. It surfaced as a test file that had run in 4.9s hanging past 150s. Delivery is now opt-in (`SMTP_ENABLED`, default false) with a 5s timeout, so a deployment that has not configured SMTP pays nothing instead of paying a timeout per write and swallowing the error — and the skip is logged at info, so a deployment that *expected* mail can see why none arrived. The full suite dropped from 92s to 62s as a side effect. Moving delivery off the request path entirely is the real fix and is not done here.
- Impact: `WatchlistAlert` + migration `030` (RLS as usual); `watchlist_service.py`; three trigger call sites; `watchlist.receive`/`watchlist.view`; two endpoints; `SMTP_ENABLED`/`SMTP_TIMEOUT`. 16 tests, verified to fail with alert creation disabled.
- Owner: bilal (dev)

**DR-037 — every module announces its work, and every waiting state has a clock**
- Date: 2026-08-18
- Context: `NotificationService` was called only from `invoice_service`, and RFQ was the one workflow with no SLA on any state. The two gaps compound: a requisition approver, a tender awarder and a payment releaser were told nothing when work arrived, so the first message any of them ever received about a decision was an SLA escalation saying it was late. That inverts what escalation means — it stops signalling "this slipped" and starts signalling "this exists" — and for a closed tender there was not even that, because a timer that does not exist never breaches. Quoting ends, the vendors wait, and the item is absent from the escalation runner and from the "overdue only" view alike: invisible in exactly the place built to make delay visible.
- Decision: `notify_awaiting_action(record, permission, action_label)` called from requisition submit, PO submit, RFQ close and payment submit-for-release; an SLA of 48h escalating to manager on the RFQ `closed` state.
- Rationale for each part:
  - **Recipients resolved by permission, not role.** Who may award a tender is a capability; naming roles in the notifier would drift from `roles.py` the moment one is granted somewhere new. The generic helper also means the next module notifies correctly by passing its permission rather than by growing another bespoke method.
  - **The creator is excluded.** Segregation of duties refuses them at the decision anyway, so telling them it is waiting on somebody is noise.
  - **48h on `closed` only, not on `issued`.** An issued RFQ has `closes_at` — its delay is the deadline working as intended. `closed` is where people, rather than the process, are the hold-up.
  - **A test that no workflow lacks an SLA entirely**, because the gap survived this long by being nobody's obvious responsibility, and the next workflow would inherit the same silence.
- Found while building it: the escalation runner built its `reference` from `invoice_number` or `po_number` with a UUID fallback — the same hardcoded-fields bug DR-033 fixed elsewhere and did not sweep up here. Every workflow outside that pair escalated under a raw UUID, so the notification named a number nobody could look up. Now reads the model's `REFERENCE_FIELD` via `record_reference`.
- Found in my own migration, and worth recording because the first defect hid the second: `031`'s downgrade compared `sla = :value`, but the column is `json`, not `jsonb`, and Postgres gives `json` no equality operator — so the statement *raised* rather than matching nothing. The failed downgrade left the database at `031`, which made the follow-up `upgrade` a no-op, which made the backfill look like it had run correctly against a row it had never touched. Matching field-by-field with `->>` fixes it; comparing `sla::text` would have worked only while stored key order and whitespace happened to match the literal. Verified properly afterwards on a seeded tenant: the empty SLA is filled, a tenant's own 4h/CFO config is left alone, and the downgrade clears only what the migration set.
- Impact: `notify_awaiting_action` + `_permission_holders`; four service call sites; RFQ `closed` SLA in `config_defaults`; migration `031` backfilling existing tenants; `sla_service` reference fix. 9 tests, 8 of which were verified to fail with the two fixes disabled.
- Owner: bilal (dev)

**DR-038 — notifications are queued in the action's transaction and delivered outside it**
- Date: 2026-08-19
- Context: every notification was sent synchronously inside the request that produced it, over SMTP with a socket timeout, with the exception swallowed. Two costs. An approval waited on a mail server before the user got a response — DR-036 recorded a test file going from 4.9s to over 150s once the watchlist started firing on ordinary vendor edits, which is what forced `SMTP_ENABLED=false` as a stopgap. And when delivery failed nobody learned: the audit trail said an SLA breach had been escalated to the CFO while no message was ever sent.
- Decision: a transactional outbox. `NotificationService._send` writes rows; `NotificationDispatcher` drains them afterwards, driven by `scripts/dispatch_notifications.py` on a schedule or `POST /notifications/dispatch`.
- Why a table rather than a thread or a task queue:
  - **Atomicity.** The row is written in the same transaction as the action. An approval that rolls back queues nothing; one that commits queues for certain. A thread started mid-action gives neither guarantee — it can send mail for work that was rolled back, and lose mail for work that was not.
  - **Durability.** An SLA escalation survives a restart. For a product whose argument is that governance events are recorded, "the approver was told" should not depend on a process staying up.
  - **Visibility.** A failure is a row with an error and an attempt count, not a swallowed exception. `GET /notifications/queue` makes a stuck queue visible without a database console.
  - **No new infrastructure.** Same operational shape as the SLA escalation runner (DR-009): safe to call repeatedly, driven by a button or a cron. Redis and a worker fleet would be a heavier answer to a problem this size.
- Details worth recording:
  - **One row per recipient**, so one bad address cannot take the others down and each retries on its own schedule.
  - **Backoff lives in the row** (`next_attempt_at`), not in the dispatcher's memory, so it survives a restart like everything else. Five attempts, then `failed` — a permanently bad address must not consume the queue forever — with a manual `retry-failed` for after the cause is fixed. Automatic resurrection would loop on a genuinely undeliverable address, which is what the limit exists to stop.
  - **The dispatcher is tenant-scoped** like every other query. The scheduler binds each tenant in turn rather than reaching around the isolation boundary for convenience.
- Found while building it, both by running rather than reading:
  - **The invoice service enqueued after `commit()`.** Its three notify calls sat deliberately after the commit — correct when they sent immediately ("notify only once the trail is durable"), and silently destructive once they wrote rows: the row was added to a session nothing subsequently commits, so the notification was dropped. The atomicity the whole design exists for was absent in exactly the module that had been notifying the longest. Caught by a test asserting a committed submit queues a message; the four newer call sites were already inside their transactions.
  - **A disabled mailer was being treated as a delivery failure.** Running the scheduler against this deployment showed each pass burning an attempt, so five scheduled runs would have marked an entire queue permanently `failed` — and turning SMTP on afterwards would have found a backlog it refused to send, the precise opposite of why the rows are kept. The dispatcher now holds untouched messages when SMTP is off and reports them as `held`.
- Not verified: actual SMTP transmission. There is no mail server in this environment, so delivery is exercised at the `_deliver` boundary. Everything above it — queueing, draining, retry, backoff, giving up, holding — is tested and was run end to end against the live API, where a vendor edit returned in 89ms with three messages queued and nothing contacted.
- Impact: `NotificationOutbox` + migration `032` (RLS as usual); `notification_dispatcher.py`; `_send` signature now takes tenant and category; four endpoints; `scripts/dispatch_notifications.py`; three invoice call sites moved inside their transaction. 15 tests.
- Owner: bilal (dev)

---

**DR-039 — a role says what you may do; a scope says what you may do it to**
- Date: 2026-08-19
- Decision: org units (`business_unit | location | department | cost_center | project`) in one self-referencing table, assigned to users through `user_org_scopes`, and applied as a global query filter alongside the tenant filter rather than per service.
- Context: the Build Book asks for "RBAC with scopes: tenant, business unit, location, cost center, project". Permissions were tenant-wide, so a manager who ran one warehouse approved invoices for every site and an auditor attached to one business unit read the whole company. Every control in the system said what a role could *do* and nothing said what it could do it *to*.
- Two defaults decided the design, and both are the safe direction of a choice that is silent when wrong:
  - **No scope means no restriction.** A user with no rows resolves to `None`, not `[]`. `None` leaves the session unrestricted; `[]` would mean "nothing at all". Creating these tables therefore changes nothing until somebody configures them — the alternative is a migration that silently hides every record from every user, which is a change discovered by a CFO rather than by a test.
  - **A null `org_unit_id` stays visible.** Otherwise every invoice raised before scopes existed disappears the moment one person is given one.
- A scope grants a unit *and everything beneath it*, resolved per request rather than stored. That is what people mean by "she runs the north region", and a stored closure would need a data migration every time a site opened — with the user it missed silently losing sight of it.
- Payments are deliberately not scoped: a payment run settles invoices across units, so scoping it would break the run rather than restrict it.
- Applied through `do_orm_execute` + `with_loader_criteria`, like the tenant and soft-delete filters, so a query written next month in a module that has never heard of scopes is still scoped.
- Found while building it: the test client's `get_db_session` override bound the tenant but not the scope — the same bug the fixture's own docstring records happening once already with tenant isolation. Every API test would have run with scopes switched off, so an API test asking "can this caller see another unit's invoices?" would have passed just as happily against a server that scopes nothing. The override now resolves scopes exactly as the real dependency does, and the request-path test fails without it.
- Verified live: an auditor scoped to NORTH sees the NORTH invoice and not the SOUTH one through `GET /invoices`, while an unscoped admin sees both.
- Impact: `org_unit.py`, `org_unit_service.py`, `api/org_units.py`, migration `036` (RLS as usual, plus `org_unit_id` on invoices/requisitions/POs), scope binding in `deps.py`, org-scope filter in `database.py`, frontend `/ai-tools/org-units`. 17 tests.
- Owner: bilal (dev)

---

**DR-040 — the error monitor watches for silence, not for errors**
- Date: 2026-08-19
- Decision: every scheduled run writes a `job_runs` heartbeat, and the admin console's health screen leads with how long it has been since each job last ran.
- Context: the Definition of Done asks for an admin console with config screens, a job monitor, an audit viewer and an error monitor. The first three existed as their own screens. The fourth was missing, and the obvious version of it — count the failures — would have caught the least dangerous failure available.
- The failure that matters here is a job that *stopped*. It raises nothing: no exception, no log line, no growing queue if the system happens to be idle. An outbox with nothing pending looks identical whether the dispatcher ran a second ago or died last Tuesday, and the difference only becomes visible when somebody is waiting on a message that will never arrive. Staleness cannot be inferred from the work queues, so it has to be recorded.
- A job that has never run reads as `down`, not `unknown`. On a fresh deployment that is briefly a false alarm; the alternative is a console that stays quiet about a cron nobody remembered to install, which is the exact failure the screen exists to catch.
- Disabled SMTP is reported as configuration, not as a fault. It is the documented default until mail is set up, and a monitor that shows red for a setting somebody chose is a monitor people learn to ignore.
- Found while building it: the first version had `record_job_run` roll the session back before writing, because a run that fails inside the database leaves the transaction poisoned and unwritable — without clearing it the console goes quiet exactly when the job starts failing, which reads identically to the job having stopped. But rolling back inside a monitoring helper silently discards whatever else the caller was holding, which is a trap for the first person to call it from inside a request. The rollback moved to the scheduled scripts, which own a disposable per-tenant session whose work is already committed; a test now pins that the recorder leaves a caller's uncommitted rows alone.
- Verified live: both scripts write heartbeats, and ageing the dispatcher's row by three hours turns the page's headline to "Something has stopped" while the hourly job correctly stays green — per-job cadence, end to end.
- Impact: `job_run.py`, `system_health_service.py`, `api/system.py`, migration `037` (RLS as usual), heartbeats in both cron scripts, frontend `/ai-tools/system`. 20 tests.
- Owner: bilal (dev)

---

**DR-041 — an export is evidence only if its hash can be recomputed from it**
- Date: 2026-08-20
- Decision: CSV, self-contained HTML, and canonical JSON. The HTML evidence pack embeds the exact canonical bundle it was rendered from, so the seal printed on the page can be reproduced from the file.
- Context: the evidence pack has existed since the Build Book's "one-click audit-ready bundle" item — assembly, hashing, sealing, the register of packs, all built and tested. It only ever came back as JSON over the API, which is not something anybody files. The obvious fix is to render a nice PDF with the hash printed on it; that fix is wrong.
- **The hash seals the canonical JSON, not the document.** Re-hashing a rendered page gives a different number, so a page carrying a hash nobody can recompute from what they are holding is decoration — and it looks exactly like one that can. So the document embeds the bundle verbatim in a script block and says how to check it. A test downloads the file from the endpoint, extracts the block, hashes it and compares against the `X-Pack-SHA256` header; adding a single byte to the embedded copy fails it.
- **No PDF library.** HTML prints to PDF from any browser, and adding a third-party renderer to the path that produces audit evidence buys a file format at the cost of a dependency in exactly the wrong place. Nothing in the document is fetched from outside either: an archive that phones home renders differently in five years, or not at all, on a machine with no network.
- **CSV injection.** Any cell opening `=`, `+`, `-` or `@` is prefixed with an apostrophe. A vendor named `=HYPERLINK(...)` is a live formula in Excel, and the person opening the file is an accountant who trusts it because it came from their own finance system. The guard is in the writer rather than only in a helper, so a table assembled elsewhere cannot bypass it.
- **One table per CSV.** A sectioned CSV with blank lines between blocks looks tidy in Excel and is unparseable by everything else. The whole report goes in the HTML instead.
- Exports are gated by the report's own permission, tested by asserting the export and the screen return the *same* status for the same role rather than that an admin may export — a second gate would drift.
- Found while building it: `_canonical_hash` in the evidence pack service and the exporter each had their own `json.dumps` arguments. Two copies of `sort_keys`/`separators` drift, and the day they do, every exported pack claims a hash that cannot be reproduced from it with nothing failing to say so. They now share one function, and a test pins that they agree.
- Impact: `export_service.py`, `api/dashboard_export.py`, evidence pack export in `api/audit.py`, `lib/download.ts`, export controls on the control room and audit screens. 39 tests.
- Owner: bilal (dev)

---

**DR-042 — performance is guarded by query counts, not by a stopwatch**
- Date: 2026-08-20
- Decision: smoke tests that count SQL statements per dashboard and assert the count does not change when the data quadruples. Wall-clock is asserted too, but only as a loose ceiling.
- Context: the dashboards are deliberately not cached, and that decision was made on measurements — ~350ms sequential against a year of volume, 188ms in parallel, most panels under 50ms — with migration 035 adding the indexes that hold it up. A decision justified by numbers, with volume expected to grow, and nothing that re-measures it, quietly stops being true. The first sign would be a user saying a page got slow.
- A timing threshold on a shared runner either flakes or is set so loose it catches nothing. A query count is deterministic, machine-independent, and catches the regression that actually turns 200ms into 20 seconds: an N+1 from iterating rows in Python instead of aggregating in SQL.
- The strongest assertion is that the count is *identical* at 40 rows and 160. That is what "aggregates in SQL" means, stated directly, and it holds at any data size.
- Confirmed by injecting an N+1 into `control_room`: the query-count tests failed immediately (47 queries at 40 invoices, 167 at 160) — and the wall-clock test **passed**, which is precisely the argument for not relying on it.
- Impact: `tests/integration/test_performance_smoke.py`, `performance` marker. 23 tests, 3.5s, kept in the default suite.
- Owner: bilal (dev)

---

**DR-043 — the acceptance item is traceability, not prose**
- Date: 2026-08-20
- Decision: an executable spec-to-test map (`tests/acceptance/requirements.py`) checked by the suite, rendered to `docs/ACCEPTANCE_COVERAGE.md`.
- Context: the Definition of Done asks for "acceptance tests (Given/When/Then)". Read literally that means rewriting 900-odd tests into Given/When/Then prose, which changes nothing about what is verified — they already describe behaviour. What the item is *for* is being able to point at a line of the Build Book and say which tests hold it up, and to see the lines where the answer is "none".
- A traceability matrix maintained by hand is worthless after the second refactor, so this one is executable: every test it names must collect, a requirement marked covered must name tests, and a gap must carry a reason. Pointing a requirement at a renamed test fails the build.
- Current reading: 24 covered, 5 partial, 1 not covered out of 30 tracked requirements. The single uncovered one is the Integration Hub.
- Impact: `tests/acceptance/`, `scripts/generate_coverage_map.py`, `docs/ACCEPTANCE_COVERAGE.md`. 6 tests.
- Owner: bilal (dev)

---

**DR-044 — stock is a ledger, not a number**
- Date: 2026-08-20
- Decision: `stock_movements` is append-only and signed; `stock_balances` is a maintained aggregate over it, rebuildable and checkable. Every change — receipt, adjustment, return, transfer — goes through one writer, `StockService.post_movement`.
- Context: the Build Book's Variant D1 is "Inventory **and Receiving**". Receiving already existed, because three-way matching needs to ask "did this turn up" — but there was nowhere for anything to arrive *to*. No item master, no locations, no balances, and no governed way to change stock without a delivery behind it. Searching the codebase for `inventory`, `stock`, `warehouse`, `sku` returned three hits, all false positives.
- **A quantity column that gets updated cannot answer the question people actually ask.** Not "what is the stock" but "why is it 37 when it should be 40, and who changed it". The ledger answers both, and it is the same principle the audit trail and the soft-delete guard already enforce: in this system nothing is edited away. The balance is kept because summing a forever-growing ledger on every receipt gets slower every day — and `reconcile_balances` exists because a cached total that can drift from its source without anybody noticing is worse than no cache.
- **Stock cannot go negative.** A shelf cannot hold minus five things, so a movement that would drive a balance below zero is a data error in every case. Allowed through it does not fail — it poisons every valuation, reorder calculation and accuracy figure downstream and is discovered during a stock count months later.
- **The balance row is locked before it is changed**, and the unique constraint on (item, location) is the other half: without it, two concurrent receipts create two rows that each hold part of the stock and every later read silently picks one.
- Receiving now posts to the ledger, but only when the order line names a stocked item and the receipt names a location. Both columns are nullable and stay that way: services and one-off spend are ordered and received without ever being held, and every receipt recorded before this existed happened somewhere nobody wrote down. Nothing is backfilled, so no historical goods retroactively appear on a shelf.
- Found while building it: three of the codebase's own guard tests failed — new chain owners with no `REFERENCE_FIELD` (a timeline would have shown raw UUIDs), no timeline permission mapped (only auditors could have read inventory history), and an audit entry written by a helper that never commits (the trail would have been discarded when the session closed). All three are exactly what those meta-tests exist for.
- Impact: `inventory.py`, `stock_service.py`, migrations `038`/`039`, receiving wired to the ledger, `/inventory` API, frontend `/ai-tools/inventory`. 35 tests.
- Owner: bilal (dev)

---

**DR-045 — an adjustment is the fraud surface, so it gets the invoice treatment**
- Date: 2026-08-20
- Decision: inventory adjustments carry a full workflow, a value threshold, dual approval above the limit, an SoD rule with no admin exemption, and mandatory reason codes.
- Context: every other stock movement has a physical event behind it — a lorry arrived, goods went back. An adjustment has nothing but somebody's word, which makes writing stock off the way a theft is covered up. Build Book D1: "adjustment thresholds with dual approval above limit", "SoD separation between receiver and approver".
- **`inventory.adjust` and `inventory.approve_adjustment` are separate permissions, separately granted.** The clerk who counts the shelf holds the first and not the second; the manager holds the second and not the first. One permission covering both would make the separation impossible to enforce however the roles were arranged.
- **No admin exemption**, unlike the invoice approval rules, which carve one out so a one-person demo tenant still functions. This control exists precisely for the person with the most access.
- **The second approver must be a different person.** Otherwise dual approval is one person clicking twice — the control failing while appearing to work.
- **The threshold is evaluated once at submission and stored.** Recomputing at approval time would let the required approver change if an item's standard cost were edited in between, which is a way to route a large write-off past the second signature without touching the adjustment itself. Measured on *absolute* value, because a write-on of 100k deserves the same scrutiny as a write-off, and netting them would let one adjustment move a fortune in both directions and score as zero.
- **Approving and posting are separate.** Approval is a decision; posting moves the ledger. Collapsing them would mean an approval that failed to post leaves stock unchanged with the paperwork saying otherwise.
- Reason codes are a fixed vocabulary, not free text, so "which vendor damages the most goods" is answerable at all. The same reasoning drives `vendor_attributable` on a return being decided at creation and stored: a scorecard that recomputed it would silently rewrite last quarter the moment the definition changed, and a supplier disputing their score is exactly when the number must not move.
- Verified through the running UI: a 60,000 write-on was refused a second signature from the manager who gave the first (403), accepted from the CFO, and posted — stock value moved 31,675 → 91,675 and the reorder flag cleared. The screen now explains "you signed this, it needs someone else" rather than offering a button that always fails.
- Impact: `inventory_control.py`, `inventory_adjustment_service.py`, `quality_check_service.py`, `vendor_return_service.py`, seven new permissions, two workflows with SLAs, two Decision Inbox collectors, three reports, an AI exception explainer. 89 tests.
- Owner: bilal (dev)

---

**DR-046 — an employee is not a user, and pay never leaves the service in the clear**
- Date: 2026-08-20
- Decision: `employees` is a separate table from `users`, linked by a nullable `user_id`. Salary, national ID and bank account are stored in full and masked at the service boundary for callers without `hr.view_compensation`.
- Context: Build Book Variant C, plus "field-level masking for sensitive fields where needed (bank accounts, national IDs)". The user confirmed the split before any of it was built: "employee, because they can be linked together easily if we want."
- **Why not one record.** A user is a login; an employee is a person the company employs. Merging them means either issuing logins to people who should not have one — an access-control decision made accidentally by HR — or losing every employee who never signs in, which in a real workforce is most of them. A picker, a driver and a cleaner are employed, paid, and never sign in; a contractor's login exists for three months against an employment that never existed. Linking is a separate, audited action, and it grants nothing: the role on the account still decides what it may do.
- **Why masking is at the boundary, not in the column.** Payroll variance, headcount cost and every budget check are arithmetic on real numbers, so the stored values must be real. What changes by permission is what *leaves* — the same pattern vendor bank details already use, reused rather than reinvented.
- The withheld value is the string `"restricted"`, not zero and not null. Both of those read as "this person is unpaid", so a report built on a masked list would be quietly wrong rather than obviously unavailable. The masked and unmasked shapes carry identical keys, so no caller has to branch and none can render `undefined` next to somebody's name.
- **Pay is kept out of the audit trail entirely.** `audit.view` is a wider audience than `hr.view_compensation`; writing a salary into an audit entry would route around the masking the module exists to enforce. The trail records that a salary was set and the *size* of a change, never the figure.
- Verified over real HTTP: a manager's `/hr/employees` response contains `"restricted"` and `••••67-1`, and the raw figure appears nowhere in the body; the CFO's contains both in full.
- Impact: `employee.py`, `employee_service.py`, migration `041`, `/hr` API, frontend `/ai-tools/hr`. 30 tests.
- Owner: bilal (dev)

---

**DR-047 — the HR separations, and the one that needed three rules**
- Date: 2026-08-20
- Decision: requesting and approving are separate permissions for headcount, payroll changes and expenses; and payroll approval refuses three distinct conflicts rather than one.
- Context: Build Book Variant C control, "SoD for HR actions and payroll approvals".
- Payroll is the clearest separation-of-duties surface in the product, and one rule is not enough for it:
  - **Raising your own.** Obvious, easy to stop.
  - **Approving your own.** Also easy, and the shape every other module already uses.
  - **Approving a rise for your own manager.** The one that needed thinking about. If I approve my manager's rise and my manager approves mine, both requests pass every check that looks at a single record, and each approval reads as correct on its own. The rule reaches one step up the reporting line to close the pair. It deliberately does not close a three-way ring: the rule people can predict is worth more than the one that catches every arrangement, and a ring shows up in the overrides report.
- Resolved through the optional employee↔user link, which is the only connection between a login and a person. Somebody with no linked account can never be "self" — correct, because a person with no login is not the one making the request.
- **The threshold on a pay change is the size of the change, measured absolutely.** A cut of 200k deserves the same scrutiny as a rise: both are large changes to somebody's pay and both can be a mistake.
- Approving a change applies it in the same transaction. Approved-but-unapplied would be paperwork saying somebody got a rise that never reached their pay.
- **Expenses get the same treatment with no admin exemption.** Self-approval on an expense claim is the version easiest to rationalise ("it was only lunch"), which is exactly what makes it worth making impossible — including for a claim somebody else keyed in on the claimant's behalf.
- The role table now enforces the split structurally: a manager may *request* a pay change and never approve one; the CFO may approve and never request.
- Impact: ten HR permissions, `payroll_change_service.py`, `expense_service.py`, `headcount_service.py`, `onboarding_service.py`, three Decision Inbox collectors, three reports. 82 tests.
- Owner: bilal (dev)

---

**DR-048 — a workflow needs both halves of its clock, so the registries now check each other**
- Date: 2026-08-20
- Decision: registry-level tests asserting that every workflow model declares `WORKFLOW_TYPE`, has configured states, and carries `state_entered_at` — plus a written allowlist for waiting states that deliberately have no SLA.
- Context: this gap has now appeared twice in one week. The inventory workflows shipped with SLA settings on their states but no `WORKFLOW_TYPE` on their models, so the escalation runner never scanned them. The HR workflows shipped with `WORKFLOW_TYPE` and no configured states, so the scan found nothing to measure. Both halves are needed; neither fails loudly on its own. In both cases the records sat outside every clock in the system, the Decision Inbox showed them as never overdue, and nothing would ever have escalated them — the exact failure DR-009 and DR-037 already record.
- Written against the registries rather than a list of workflows, so the next module is covered without anybody remembering to add it.
- The SLA check surfaced eight waiting states with no clock. Four are legitimate: two are passed through inside a single transaction (`inventory_adjustment.approved`, `payroll_change_request.approved`) and two are governed by a domain date instead (a tender's closing date, a PO's expected date). Four are arguable gaps — `requisition.approved`, `purchase_order.approved`, `invoice.approved`, `invoice.validated` — and nothing chases any of them today. They are recorded in `CLOCK_FREE` with that stated, rather than silently given escalations: whether they should be chased is a business decision. A second test fails if an allowlist entry outlives the state it excuses.
- Impact: `tests/integration/test_hr_workflows.py::TestEveryWorkflowHasAClock`, migration `040`. 5 tests.
- Owner: bilal (dev)

---

**DR-049 — the Integration Hub pushes facts and pulls configuration, and one provider proves the interface**
- Date: 2026-08-24
- Decision: connect a tenant's own accounting system per tenant and per provider; push approved payments and paid expense claims out as journal entries; pull chart of accounts and party lists on demand as a wholesale replace. QuickBooks Online implemented; Xero and SAP deliberately not.
- Context: Build Book "Integration connectors implemented with retries, idempotency, dead-letter queues, and reconciliation records" — the last unbuilt Definition-of-Done item. Confirmed unbuilt beforehand: no reference to "integration", "webhook" or "external_id" existed anywhere except the notification outbox's own docstring, which names itself as the shape a webhook sender would reuse.
- **Sync direction was the first argument, and one-way push was the wrong first answer.** My initial proposal was push only. The user pushed back — most clients want their existing data read in rather than re-keyed. Both halves are right about different data: Sarmaya is the system of record for the *decision*, so a decision only ever travels outward; the client's own chart of accounts and vendor list are their record, so those only ever travel inward. Splitting on that line means there is no conflict resolution anywhere in this feature, because Sarmaya never edits a record in the client's books and the two copies cannot disagree about who changed what. What was rejected is continuous two-way sync, which would need exactly that machinery.
- **One provider, built end to end, before generalising.** An interface designed against three vendors' documentation with zero working code behind any of them is an interface shaped by guesswork. SAP in particular is not one API — it varies by deployment and often has no OAuth consent screen at all — and designing for it now would warp the abstraction around a case nobody has exercised. `FinanceConnector` carries only what this slice uses; QuickBooks' `revoke` is called on the concrete class rather than promoted to the ABC, because a method earns a place there once a *second* provider needs the same shape.
- **Resolution is per connection, never a global setting.** This is specifically the thing `app/services/ai/__init__.py` and the OCR module get wrong for this purpose: one `settings.AI_PROVIDER` picks one provider for the whole deployment. A connection is a row scoped to one tenant, so two tenants can be on different providers — or none — without touching configuration.
- **The OAuth `state` value carries the tenant id, because RLS makes the obvious approach impossible.** The plan originally said the callback would look the pending row up by state across all tenants. That cannot work here: the callback carries no bearer token, and a session with no tenant bound sees *zero* rows under this app's policies, not every tenant's — the non-BYPASSRLS `os_app` role has nothing to fall back to. So `state` is `"{tenant_id}:{random_token}"`, self-describing on purpose, and the tenant is bound before the one query that needs it. This follows the precedent `/auth/login` already sets by reading a tenant slug before it can bind anything. The tenant id half is not a secret and grants nothing; the random half does the CSRF work, and the stored, single-use, expiring value is what makes both "never issued" and "already used" refusable.
- **A `JournalEntry`, not a `Bill`.** A Bill tells QuickBooks "you now owe this" — an unpaid liability with a due date, which is false at the moment Sarmaya posts, because Sarmaya only posts *after* the payment released. A JournalEntry records something that already happened, which is what "notify the external ledger" actually means here.
- **Which accounts an entry debits and credits is configured, never guessed.** The approved plan did not specify this and `post_journal_entry` would have had nothing to reference. Matching "Accounts Payable" or "Bank" by name silently posts to the wrong account in a chart that spells either differently — a wrong number in somebody's books that nothing in Sarmaya would show as wrong. Two explicit columns, validated against the last pull, and a connection without them queues nothing.
- **Idempotency is three independent layers, because at-least-once is what the queue actually guarantees.** A unique constraint on `(connection_id, source_type, source_id)` is the database floor. `DocNumber` is set to the queue row's id and checked before every create, which is the only thing covering a response lost *after* QuickBooks wrote and *before* Sarmaya recorded it — QBO has no idempotency-key header. And the enqueue insert runs inside a SAVEPOINT.
- **The savepoint is the one that would have caused real damage.** `enqueue` runs inside `release_payment`'s and `mark_paid`'s own transaction, before their final commit — that shared transaction is the entire point of an outbox. The first version called a bare `self.db.rollback()` on the duplicate-post conflict, which would have discarded the caller's whole transaction: the state change, the audit entries, the settled invoices, over a problem that has nothing to do with the payment. Caught in review before it shipped. `tests/integration/test_integration_hub.py::TestADuplicateQueueRowCannotDiscardThePayment` was run against the reverted code to confirm it fails, and it fails on exactly that — a record written before the conflicting enqueue disappears.
- **A dead connection stops the rest of its own queue for the run.** `ConnectorAuthError` means a human has to reconnect; retrying cannot fix it. Attempting every remaining post on that connection would spend all five of each one's attempts on a problem none of them caused, leaving a queue permanently failed by the time somebody reconnects. Their `next_attempt_at` is left untouched rather than moved. Skipped posts are counted apart from retrying ones for the same reason the distinction exists at all: folded together, a tenant who disconnected with posts queued would log "retrying=100" every five minutes, which reads as a hundred failures on the health page.
- **No `WORKFLOW_TYPE` on either new model**, deliberately. DR-048 records that declaring one without matching states in `DEFAULT_WORKFLOWS`, or the reverse, leaves records outside every clock invisibly — twice in one week. There is no approval routing or SLA escalation on a connection; its status is read by the scheduled health job, not the SLA engine. The fix for DR-048's class of bug is not making the mistake, rather than catching it afterwards.
- **What does not trigger a push, and why.** Invoice approval: nothing has moved yet, and posting it would mean a Bill — a different decision, out of scope. Payroll changes: no GL account structure exists for payroll postings, and inventing one here would be designing ahead of a spec that does not exist, the same mistake as designing for Xero now.
- Tokens are Fernet-encrypted with `app/core/mfa.py`'s existing key derivation, reused verbatim rather than a second scheme — a second configured encryption key is a second way for a deployment to end up unprotected by omission. The audit trail records the company name and never a token; `refresh` stores the rotated refresh token as well as the access token, because QuickBooks rotates it on every use and keeping the old one kills the connection silently at the next refresh.
- Impact: `integration.py`, `finance_connectors/` (base + QuickBooks), `integration_service.py`, `integration_posting_service.py`, `/integrations` API, migrations `042`/`043`, `scripts/dispatch_integration_posts.py`, two new permissions, two enqueue call sites. 62 tests.
- Owner: bilal (dev)

---

**DR-050 — an empty audit pack is a finding, except when it is a failed lookup**
- Date: 2026-08-26
- Decision: audit packs can be scoped to a period, or to one named control across a period, alongside the existing per-chain pack. A period/control pack seals an empty result; a chain pack still refuses to.
- Context: Build Book, Audit/Compliance — "One-click audit pack export per period and per control." The chain pack (`evidence_pack.py`) already answered "what happened to this invoice". It cannot answer the question an auditor actually opens with, which is whether a control *operated* across a quarter.
- **The two empty cases look identical and are opposites.** `EvidencePackService.generate` refuses to seal an empty chain pack, and its reasoning is recorded in the code: a correlation id belonging to another tenant, or to nothing, produced a hash-stamped permanent document covering zero objects and asserting `all_chains_verified: true` — true only in the sense that nothing was checked. That is a lookup failure dressed as evidence. A control pack over a date range is not: the scope is well-formed, the query ran, and "this control did not fire in Q3" is a computed answer an auditor specifically wants on the record. Both behaviours are now pinned by tests in the same file, because the pair is exactly the kind of inconsistency somebody later "tidies up" into one rule and breaks whichever half they did not read.
- **Three numbers per control, not one.** Applied, blocked, and overridden are different facts. A control with applications and no blocks is working and unchallenged; one with no applications at all is either irrelevant to the period or not wired up, and those are worth telling apart. A single "activity" total would say a control was busy without saying whether it ever refused anything.
- **Exceptions are sampled; ordinary applications are only counted.** An auditor testing a control reads the refusals and the overrides and takes the volume of normal operation as a number. Shipping every approval in a quarter would bury the five records that matter in ten thousand that do not.
- **The control registry names only actions services really write**, and a test greps `app/` to prove it. A registry entry nothing emits produces a pack reporting zero forever, which reads as "the control never fired" rather than "nobody wired this entry up" — the failure mode a hand-kept list always eventually has. An unrecognised reason is reported as itself rather than dropped or guessed at.
- **`generated_at` is outside the hash.** Caught by a test asserting two builds of the same period agree: the first implementation hashed the whole payload including its own timestamp, so every regeneration differed and the seal proved nothing. The entire value of `pack_hash` is that regenerating later and getting a different answer means something underneath changed. The chain pack already had this right — hashing `content` and leaving metadata outside it — and the fix was to match it rather than invent a second convention.
- **The scope is constrained at the database.** Migration 044 makes `correlation_id` nullable and adds a check constraint: a chain pack must have a correlation id and no period, a period pack the reverse, a control pack a period and a control. A sealed document is meant to be pointed at later, and one that disagrees with its own stated scope is worth refusing in the schema rather than trusting every future caller. Declared in the model's `__table_args__` as well as the migration, so `create_all` builds it for the test database too — a constraint that exists only in production is one nothing proves.
- Same table rather than a new one: both kinds are sealed, hash-stamped bundles that belong in one list of packs that were produced. What differs is the scope, so the scope became a column.
- Impact: `audit_pack.py`, `evidence_pack.py` model, migration `044`, `/audit/pack` and `/audit/controls`. 19 tests.
- Owner: bilal (dev)
