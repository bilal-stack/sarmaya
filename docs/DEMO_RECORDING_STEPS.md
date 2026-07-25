# Sarmaya OS — Step-by-Step Recording Guide

A tick-off shot list. Work top to bottom. Each step is either:

- **`[DO]`** — an action on screen
- **`[SAY]`** — read this aloud, word for word
- **`[WAIT]`** — pause for the app (and to give your editor a clean cut point)

> For the scene-level overview and OBS settings, see `DEMO_SCRIPT.md`.
> This file is what you keep open **while** recording.

---

# PART A — Setup (do once, before recording)

### A1. Reset the data
- [ ] **`[DO]`** Open PowerShell in `C:\python\sarmaya`
- [ ] **`[DO]`** Run: `.\.venv\Scripts\python.exe _demo_bootstrap.py`  *(first time only)*
- [ ] **`[DO]`** Run: `.\.venv\Scripts\python.exe _demo_seed.py`
- [ ] **`[DO]`** Run: `.\.venv\Scripts\python.exe _demo_make_pdfs.py`  *(first time only)*
- [ ] **`[DO]`** Run: `.\.venv\Scripts\python.exe _demo_ids.py` → **screenshot this output** or put it on a second screen. You will paste these IDs on camera.

### A2. Start the servers (leave both running, minimized)
- [ ] **`[DO]`** Terminal 1: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- [ ] **`[DO]`** Terminal 2: `cd C:\react\sarmaya-frontend` then `npm run dev`
- [ ] **`[WAIT]`** Both are up: `http://127.0.0.1:8000/docs` and `http://localhost:9002` load

### A3. Browser prep
- [ ] **`[DO]`** Tab 1 → `http://localhost:9002` → log in as `admin@demo.com` / `demo1234`
- [ ] **`[DO]`** Tab 2 → `http://127.0.0.1:8000/docs`
- [ ] **`[DO]`** In Swagger, click **Authorize** → username `admin@demo.com`, password `demo1234` → **Authorize** → **Close**
- [ ] **`[DO]`** Zoom both tabs to **125%** (Ctrl + `+`)
- [ ] **`[DO]`** Hide the bookmarks bar (Ctrl+Shift+B), close all other tabs
- [ ] **`[DO]`** Turn on Windows **Do Not Disturb**; silence your phone

### A4. Rehearse once (not recorded)
- [ ] **`[DO]`** Upload `demo_assets/invoice_orion_1042.pdf` in the frontend — confirm OCR + Claude respond
- [ ] **`[DO]`** Re-run `_demo_seed.py` afterwards to clear the rehearsal upload

### A5. OBS
- [ ] **`[DO]`** Scene has **Display Capture** + **Audio Input Capture** (your headset mic)
- [ ] **`[DO]`** Say a test line — mic meter peaks in **yellow**, never red
- [ ] **`[DO]`** Record 10 seconds, play it back, confirm audio + video are both there

---

# PART B — Recording

> **Method:** record the *screen only* first (silent, following the `[DO]` steps),
> then record narration while watching playback. Doing both at once is the #1
> cause of scrapped takes.
>
> If you prefer live narration, that works too — just pause 1 second after each
> action before speaking.

---

## SCENE 0 — Hook  *(~25 sec)*

- [ ] **`[DO]`** Start on the frontend dashboard
- [ ] **`[SAY]`**

> "This is Sarmaya OS — an accounts-payable automation backend I built with FastAPI, PostgreSQL, and Claude.
>
> Most invoice tools stop at data capture. This one is built around governance: every policy is configuration, every decision is explainable, and every action — including every AI action — is recorded in a tamper-evident audit trail.
>
> Let me show you what that means in practice."

- [ ] **`[WAIT]`** 1 sec pause → cut point

---

## SCENE 1 — Upload & AI extraction  *(~60 sec)*

- [ ] **`[DO]`** Frontend → **AI Tools → Invoice Upload**
- [ ] **`[DO]`** Drop in `demo_assets/invoice_orion_1042.pdf`
- [ ] **`[WAIT]`** Extraction finishes (a few seconds — do not talk over the spinner)
- [ ] **`[SAY]`**

> "I'll upload a real invoice PDF.
>
> Behind the scenes, two things happen. First OCR pulls the raw text. Then Claude cleans it up — it merges fragmented line-item descriptions and normalizes the fields into structured JSON.
>
> And that's the important part: the AI's output isn't trusted blindly. It's validated against a strict schema before it touches the invoice. If the model returns something malformed, the result is rejected and the raw OCR data stands. The AI assists — it never decides."

- [ ] **`[DO]`** Move the cursor slowly over the extracted fields, then the confidence score
- [ ] **`[SAY]`**

> "We get the vendor, invoice number, date, total, tax — and a confidence score that drives what happens next."

- [ ] **`[WAIT]`** 1 sec → cut point

---

## SCENE 2 — Duplicate detection  *(~45 sec)*

- [ ] **`[DO]`** Upload `demo_assets/invoice_orion_1051.pdf`
- [ ] **`[WAIT]`** The duplicate warning appears
- [ ] **`[SAY]`**

> "Now I'll upload a second invoice from the same vendor. Different invoice number — but the amount is within a third of a percent, and it's dated three days later.
>
> The system flags it as a potential duplicate. Not a hard block — a soft warning, because sometimes those are legitimate. But it cannot be approved until a human reviews it and overrides it with a written reason. That reason goes into the audit trail."

- [ ] **`[DO]`** Hover the duplicate warning so it's clearly visible
- [ ] **`[WAIT]`** 1 sec → cut point
- [ ] 💡 **Do this scene twice** — it's a money shot.

---

## SCENE 3 — Decision Inbox  *(~50 sec)*

- [ ] **`[DO]`** Switch to the **Swagger tab**
- [ ] **`[DO]`** Find **Decision Inbox → `GET /api/v1/inbox`** → **Try it out** → **Execute**
- [ ] **`[WAIT]`** Response renders
- [ ] **`[SAY]`**

> "This is the Decision Inbox — one prioritized worklist across everything waiting on you.
>
> Each pending invoice is reduced to its single most blocking next step. Not a list of invoices — a list of decisions."

- [ ] **`[DO]`** Scroll slowly through the items; pause on the first one (`INV-2001`)
- [ ] **`[SAY]`**

> "This one is blocked because its vendor isn't verified yet. These are waiting on approval. And notice this first one is flagged overdue — it's been sitting in pending approval for seventy-two hours, past the forty-eight-hour SLA. Breached items sort to the top automatically."

- [ ] **`[DO]`** Set `overdue_only` = **true** → **Execute**
- [ ] **`[SAY]`**

> "And I can filter to just the SLA breaches."

- [ ] **`[WAIT]`** 1 sec → cut point

---

## SCENE 4 — SLA escalation  *(~40 sec)*

- [ ] **`[DO]`** **`POST /api/v1/inbox/escalate-overdue`** → **Try it out** → **Execute**
- [ ] **`[WAIT]`** Response shows `escalated_count: 1`, `escalated_to: "cfo"`
- [ ] **`[SAY]`**

> "SLAs are configured per workflow state — forty-eight hours in pending approval, then escalate to the CFO. The timer starts the moment an invoice enters a state.
>
> Running the escalation records an audit event and notifies the CFO. And it's idempotent — it escalates each breach exactly once per state entry, so I can safely wire it to a cron job.
>
> The escalated invoice also becomes visible in the CFO's inbox — even though the original approver was a manager. The original approval chain is preserved in the trail; escalation adds to it, it doesn't rewrite it."

- [ ] **`[WAIT]`** 1 sec → cut point
- [ ] ⚠️ **If it returns `escalated_count: 0`** — it already escalated on an earlier take. Re-run `_demo_seed.py` and redo this scene.

---

## SCENE 5 — The AI agent  *(~50 sec)*

- [ ] **`[DO]`** **Invoices → `GET /api/v1/invoices/{invoice_id}/next-action`** → **Try it out**
- [ ] **`[DO]`** Paste the **INV-2003** id from your cheat sheet; set `use_ai` = **false** → **Execute**
- [ ] **`[SAY]`**

> "This is the workflow agent. You ask it: what should happen to this invoice next?
>
> It answers `verify_vendor` — and it shows its work. These are the signals it reasoned from: the invoice is pending approval, and the vendor is still pending verification."

- [ ] **`[DO]`** Point the cursor at the `signals` array
- [ ] **`[DO]`** Replace the id with **INV-2002**; set `use_ai` = **true** → **Execute**
- [ ] **`[WAIT]`** Claude responds (2–4 sec)
- [ ] **`[SAY]`**

> "With AI enabled, Claude writes the explanation and scores its confidence — but here's the critical design decision: the AI is not allowed to choose the action. Policy determines what's permitted; the model can only phrase it.
>
> If the model tries to return a different action — say it decides to just approve something — that output is discarded, the deterministic result stands, and the attempt is logged as a schema failure. That's tested."

- [ ] **`[DO]`** Point at `"source": "ai"` and `"ai_model"`
- [ ] **`[WAIT]`** 1 sec → cut point

---

## SCENE 6 — Governance gates  *(~45 sec)*

### 6a — Segregation of duties
- [ ] **`[DO]`** Click **Authorize** → **Logout** → log in as `manager@demo.com` / `demo1234`
- [ ] **`[DO]`** **`POST /api/v1/invoices/{invoice_id}/approve`** with the **INV-2004** id → **Execute**
- [ ] **`[WAIT]`** **403** appears
- [ ] **`[SAY]`**

> "Now the controls. Segregation of duties: this manager created this invoice, so they cannot approve it. Maker-checker, enforced in the service layer — and the blocked attempt is itself written to the audit trail."

### 6b — Vendor gate
- [ ] **`[DO]`** **Authorize** → **Logout** → log back in as `admin@demo.com`
- [ ] **`[DO]`** **`POST /api/v1/invoices/{invoice_id}/approve`** with the **INV-2003** id → **Execute**
- [ ] **`[WAIT]`** **403** appears
- [ ] **`[SAY]`**

> "And here's the vendor gate. This invoice's vendor is still pending verification, so no one can approve it — not even an admin. Money doesn't move against an unverified vendor. Someone with vendor-management rights has to verify that vendor first, and they can't be the person who created it either."

- [ ] **`[WAIT]`** 1 sec → cut point
- [ ] 💡 **Do both 403s twice** — money shots.

---

## SCENE 7 — Live Audit Mode  *(~45 sec)*

- [ ] **`[DO]`** **`GET /api/v1/audit/timeline/invoice/{object_id}`** with the **INV-2003** id → **Execute**
- [ ] **`[DO]`** Scroll slowly through the events
- [ ] **`[SAY]`**

> "Every object opens as a full timeline — what happened, when, who did it, and why. Each event carries a plain-English reason, including the exact policy that routed the approval, snapshotted at the moment of the decision. So if the policy changes later, the history still shows what rule actually applied."

- [ ] **`[DO]`** **`GET /api/v1/audit/verify/invoice/{object_id}`** — same id → **Execute**
- [ ] **`[WAIT]`** `"verified": true`
- [ ] **`[SAY]`**

> "And the audit trail is tamper-evident. Every entry is hash-chained to the one before it. If someone edited or deleted a row directly in the database, this check would fail and tell you exactly which event broke."

- [ ] **`[DO]`** **`GET /api/v1/audit/ai-actions`** → **Execute**
- [ ] **`[SAY]`**

> "The same discipline applies to AI. Every single AI call is logged — which model, which prompt version, the confidence, the latency, and whether the output passed schema validation or fell back. Full reproducibility."

- [ ] **`[WAIT]`** 1 sec → cut point

---

## SCENE 8 — Close  *(~30 sec)*

- [ ] **`[DO]`** *(optional)* **`GET /api/v1/config/versions/workflow/invoice`** → **Execute** — shows versioned config
- [ ] **`[DO]`** Switch to a terminal showing `261 passed` (run `pytest -q` beforehand and leave it on screen)
- [ ] **`[SAY]`**

> "Everything you saw is configuration, not hardcode — approval thresholds, workflow states, transition guards, SLAs. All editable through the API, all versioned, and any version can be rolled back.
>
> The whole thing is multi-tenant with PostgreSQL row-level security, and it's covered by two hundred and sixty-one tests.
>
> Thanks for watching."

- [ ] **`[DO]`** Hold the final frame for 2 seconds before stopping the recording

---

# PART C — After recording

- [ ] **`[DO]`** Trim dead air between scenes
- [ ] **`[DO]`** Add a title card at the start: *"Sarmaya OS — Governance-First AP Automation | FastAPI · PostgreSQL · Claude"*
- [ ] **`[DO]`** Add lower-third captions for each scene name (optional but looks sharp)
- [ ] **`[DO]`** Export **1080p, 30fps, MP4**
- [ ] **`[DO]`** Watch it once end to end with fresh ears — cut anything that drags

**Target length: 6 minutes.** If you're over 7, cut Scene 8's config-versions call and tighten Scene 3.

---

# Troubleshooting mid-shoot

| Symptom | Fix |
|---|---|
| `escalated_count: 0` in Scene 4 | Re-run `_demo_seed.py`, redo the scene |
| Swagger says 401 | Click **Authorize** again — the token expired or you logged out in Scene 6 |
| Upload hangs / OCR error | Check your OCR.space + Anthropic keys in `.env`; check the uvicorn terminal |
| Duplicate warning didn't show | You uploaded 1051 before 1042 — re-run `_demo_seed.py` and upload in order |
| Invoice IDs don't match | Re-run `_demo_ids.py`; IDs change only if the database was rebuilt |
| A take is ruined | `_demo_seed.py` resets everything — just start the scene again |
