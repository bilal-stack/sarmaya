---
name: project-sarmaya-overview
description: "What Sarmaya OS is — a multi-tenant FastAPI AP/invoice automation backend, its stack, and current stage"
metadata: 
  node_type: memory
  type: project
  originSessionId: 83618a84-98c1-432e-a373-0e6e64f6a52e
---

Sarmaya OS (mini) is a multi-tenant accounts-payable / invoice-management backend.

**Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL (with Row Level Security for tenant isolation) + Alembic migrations. JWT auth (`bcrypt_sha256` hashing). OpenAI for AI features, Google Document AI for invoice OCR. Layered architecture: api → services → repositories → models. Dev on Windows (PowerShell), venv at `.\env`, runs via uvicorn on `127.0.0.1:8000`, API base `/api/v1`.

**Implemented (MVP complete as of merge #5):** auth, invoice CRUD + OCR upload + line items, workflow/approval transitions, vendors, files, audit logging, AI chat with conversation persistence, Query Agent (NL→SQL via function calling), Duplicate Detection Agent (exact/fuzzy/line-item strategies).

**Why:** AP automation — upload invoices, OCR-extract them, route through approval workflow, query/audit via AI.

**Frontend (separate repo):** lives at `C:\frontend-react\sarmaya-frontend` (an additional working dir, NOT under `C:\python\sarmaya`). Next.js 15.3.3 App Router (Turbopack, dev port **9002**) + React 18 + TypeScript + Tailwind/Radix UI, react-hook-form+zod, recharts, Genkit/Firebase for some AI. Talks to this backend via `src/lib/api-config.ts` (`API_BASE_URL = http://127.0.0.1:8000/api/v1`, JWT bearer in localStorage). Endpoints consumed match MVP: auth, invoices (list/detail/upload/submit/approve/reject/mark-paid), conversation (chat/query/detect-duplicate/list/messages/delete), check-duplicate. Routes under `src/app`: login, signup, pricing, dashboard, ai-tools/*. Sarmaya-relevant ai-tools = invoice-upload, invoices, ai-chatbot, query-chatbot, detect-duplicate; the others (business-concept-assessor, investor-pitch-coach, pitch-deck-advisor, life-science-chatbot, deep-research-chatbot) appear to be leftover starter-template boilerplate. Known minor bug: localStorage key inconsistent — `galsi_user_data` in apiFetch vs `sarmaya_user_data` in apiUpload.

**How to apply:** Treat this as the working backend. Endpoint reference lives in ENDPOINTS.md; run/setup details in README.md. Note `os-google.json` (GCP service-account key) is committed in the repo root — flag if security comes up.
