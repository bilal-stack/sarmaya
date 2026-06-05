---
name: project-build-book
description: "Sarmaya OS Build Book — the long-term product vision (governance-first Ops OS, all variants), non-negotiables, and stack defaults that explain architecture choices"
metadata: 
  node_type: memory
  type: project
  originSessionId: 83618a84-98c1-432e-a373-0e6e64f6a52e
---

The **Build Book** (`C:\python\sarmaya\build-book.txt`, v1.0, 2025-12-16, **Owner: Andrew**) is the declared single source of truth for Sarmaya OS across ALL variants — far broader than the invoice MVP. The MVP ([[project-mvp-spec]]) is just the first slice (Finance OS / Procure-to-Pay) of this larger vision.

**Product vision:** a governance-first "Ops OS" — not a generic ERP. Makes policies executable, evidence traceable, decisions explainable. Core differentiators: **Decision Inbox** (one cross-department task surface), **Live Audit Mode** (any txn opens as a full timeline with policy reasons + evidence), **Restricted Autopilot** (opt-in, low-risk, reversible, logged), cross-department handoffs as primitives.

**Non-negotiables (these drive every design decision):**
- **Configuration-first:** policies, thresholds, approval matrices, module toggles editable WITHOUT code deploys.
- **Workflow-first:** every process is a state machine with explicit states/transitions/guards/audit, stored as versioned JSON per workflow.
- **Strict auditability:** immutable append-only audit trail, policy evaluation snapshots, evidence-pack integrity hashing.
- **AI is gated:** structured JSON only, always schema-validated, human-in-the-loop (HITL) when risk/confidence thresholds require. AI never finalizes high-risk actions. Always attach explainability trace + prompt/model version.
- **Enterprise-ready:** deploy-per-client (VPC/on-prem) is PRIMARY model, but code must stay multi-tenant compatible (tenant_id scoping everywhere). ← This is why the current backend uses multi-tenant + RLS even though the MVP spec said single-company.

**Stack defaults (editable, but principles fixed):** FastAPI+Pydantic (modular monolith first), Temporal (workflow engine), OPA/Rego (policy engine, versioned, snapshot every decision), NATS JetStream (events), Postgres (system-of-record, pg_partman for audit), OpenSearch (audit/doc search), S3/MinIO (hashed evidence), Keycloak OIDC/SAML (RBAC+SoD), Document AI / Azure Form Recognizer (OCR, Tesseract fallback), Qdrant/pgvector (copilot, tenant-scoped), Metabase/Superset (dashboards), Docker+Terraform on ECS/Fargate→EKS, OTel/Prometheus/Grafana/Loki/Sentry, GitHub Actions CI/CD with reversible migrations.

**Variants (packs on top of Core Platform):** A=Finance OS (P2P, Treasury, R2R), B=Procurement OS, C=HR OS, D=Supply Chain OS, E=Operations OS, F=RevOps OS, G=ITSM OS, H=GRC/Audit OS, I=Data Backbone OS.

**Process rule:** if something is ambiguous, log it in a **Decision Register** (template in Build Book Appendix C) and propose the best default — Andrew does NOT want ad-hoc interpretation.

**How to apply:** When building, align with the non-negotiables above and the canonical entities (Tenant, OrgUnit, User, Role, Vendor, WorkItem, PolicyEval, AuditEvent, EvidencePack). Current code is an early Finance-OS slice; expect it to grow toward this. Flag ambiguities rather than guessing.
