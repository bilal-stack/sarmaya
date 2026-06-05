---
name: feedback-standards-security
description: "User expects new/rewritten code to follow best practices, project conventions, and security standards — not quick prototypes"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 83618a84-98c1-432e-a373-0e6e64f6a52e
---

When building or extending modules, the user expects code that follows best practices, established project conventions, and security standards — not throwaway prototypes.

**Why:** When asked which module to build next, the user replied "your recommended module. keep in mind that we need to follow best practices, standard, secured." This came right after a hardening pass on the MVP. The existing `app/api/vendors.py` was a non-conforming prototype (JWT passed as a URL query param, no schemas, no permission checks, no service/repository layering, raw hard delete) — it was fully rewritten to match the invoice module's standard.

**How to apply:** Match the layered pattern already in the codebase (api → service → repository → model), use header-based auth via `app.api.deps.get_current_user` + RLS via `get_db_session` (never token-in-query-param), add Pydantic schemas with validation, gate writes with `has_permission(...)` raising `PermissionError`, prefer soft-delete/deactivate over destructive deletes when records are referenced, and add tests. See [[project-sarmaya-overview]].
