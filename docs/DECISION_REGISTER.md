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
