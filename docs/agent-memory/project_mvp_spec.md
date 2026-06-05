---
name: project-mvp-spec
description: "Sarmaya OS MVP specification — scope, roles, approval rules, and where the current code diverges from the spec"
metadata: 
  node_type: memory
  type: project
  originSessionId: 83618a84-98c1-432e-a373-0e6e64f6a52e
---

The MVP spec (v1.0, 2025-11-19, "Approved for Development") lives at `C:\Users\Bilal\Downloads\MVP-Sarmaya.pdf`. It defines an **invoice-processing core** for 1-2 users (AP Clerk + Approver), ~1 month timeline.

**Spec-defined scope (source of truth for MVP):**
- **Roles (3 only):** Admin (full), AP Clerk (create/edit invoices), Approver (approve/reject). Approval routing splits to Manager vs CFO by amount.
- **Workflow states (no skipping, all transitions logged):** Draft → Validated → Pending Approval → Approved → Rejected → Paid.
- **Approval policy:** amount ≤ 250,000 PKR → Manager approves; > 250,000 PKR → CFO. Rules stored in DB (configurable).
- **OCR extracts 6 fields:** vendor name, invoice number, invoice date, total amount, tax/VAT, currency (default PKR). Plus per-field confidence score + "Why?" snippet explanation.
- **Duplicate detection:** exact match (vendor+invoice#) = hard block; fuzzy (vendor + amount ±5% + date within 7 days) = soft warning, overridable with logged reason.
- **Other modules:** vendor master (single bank account), file storage (PDF + SHA-256 hash), audit trail, basic dashboard (KPI cards, top-5 vendors, quick actions), email notifications on approve/reject.

**Where current code already diverges from / exceeds the spec:**
- Spec says **single-company, NO multi-tenant** (multi-tenant is Phase 5). The code is built **multi-tenant with PostgreSQL RLS** — ahead of spec.
- Spec lists **line items as out-of-scope**, but they were added (commit `8b6328e`).
- Spec's AI scope is OCR + duplicate detection only; code adds an **AI chat + NL→SQL Query Agent** — beyond spec.
- OCR: spec recommends Google Cloud Vision / AWS Textract; code uses **Google Document AI** (with ocr_space + textract stubs present).

**Why:** This is the contracted MVP deliverable.

**How to apply:** Treat the PDF as the requirements baseline. When asked to build a feature, check it against spec scope (and the "Out of Scope / Add Later" lists) before expanding. Flag when a request conflicts with spec scope or duplicates work already done beyond spec. See [[project-sarmaya-overview]] for what's implemented.
