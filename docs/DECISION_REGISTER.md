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
