# Sarmaya OS — Portfolio Demo Video Kit

Everything needed to record a ~6-minute portfolio demo: setup, scene-by-scene
actions, and word-for-word narration. Every scene below was executed against the
real API before this script was written — the outputs quoted are what you will
actually see.

---

## 1. Before you hit record

### Reset to camera-ready state (run between takes)
```powershell
cd C:\python\sarmaya
.\.venv\Scripts\python.exe _demo_bootstrap.py    # first time only (tenant + admin)
.\.venv\Scripts\python.exe _demo_seed.py         # users, vendors, invoices, SLA reset
.\.venv\Scripts\python.exe _demo_make_pdfs.py    # sample invoice PDFs (first time only)
```

### Start the servers (two terminals)
```powershell
# Backend
cd C:\python\sarmaya
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Frontend
cd C:\react\sarmaya-frontend
npm run dev
```

### Demo accounts (password for all: `demo1234`)
| Email | Role | Used for |
|---|---|---|
| `admin@demo.com` | admin | Most scenes |
| `manager@demo.com` | manager | Segregation-of-duties scene |
| `cfo@demo.com` | cfo | Escalation / high-value routing |
| `clerk@demo.com` | ap_clerk | Upload scene (optional) |

### Sample invoices — `demo_assets/`
| File | Why |
|---|---|
| `invoice_orion_1042.pdf` | Main upload — Orion Supplies, PKR 185,965.50 |
| `invoice_orion_1051.pdf` | **Fuzzy duplicate** of 1042 (0.3% apart, 3 days later) |
| `invoice_meridian_9001.pdf` | PKR 796,800 → routes to **CFO** |

### Browser prep
- Two tabs: **http://localhost:9002** (frontend) and **http://127.0.0.1:8000/docs** (Swagger).
- In Swagger click **Authorize** → username `admin@demo.com`, password `demo1234` → Authorize.
  (This works via the OAuth2 `/auth/token` endpoint added for tooling.)
- Zoom browser to **125%** so text is readable at 1080p.
- Hide bookmarks bar; close unrelated tabs; enable Do Not Disturb.

---

## 2. OBS setup

**Settings → Video:** Base & Output resolution `1920x1080`, FPS `30`.
**Settings → Output:** Mode `Simple`, Recording quality `High Quality`, format `MP4`,
encoder `Hardware (NVENC/AMF/QuickSync)` if available.
**Settings → Audio:** Sample rate `48 kHz`, Mic on your headset (not laptop mic).

**Scene 1 — "Demo":** Sources → **Display Capture** (or Window Capture on the browser)
+ **Audio Input Capture** (mic).

**Mic filters** (right-click mic → Filters) — these three make a huge difference:
1. **Noise Suppression** (RNNoise)
2. **Compressor** (ratio 4:1, threshold −18 dB)
3. **Limiter** (−2 dB)

Speak 15–20 cm from the mic. Watch the meter — peaks in the **yellow**, never red.

**Recording approach — record narration separately (recommended).** Record the
screen silently, then record voice-over while watching playback. It removes the
pressure of doing both at once and lets you retype a failed step without ruining
a good take. If you prefer live narration, pause between scenes so cuts are easy.

**Post:** trim dead air, add a title card at the start
("Sarmaya OS — Governance-First AP Automation | FastAPI · PostgreSQL · Claude").

---

## 3. The script

Total ≈ 6 minutes. `[ACTION]` = what you do on screen. Narration is written to be
read aloud — short sentences, natural pauses.

---

### SCENE 0 — Hook (0:00–0:25)

`[ACTION]` Title card, then the frontend dashboard.

> "This is Sarmaya OS — an accounts-payable automation backend I built with
> FastAPI, PostgreSQL, and Claude.
>
> Most invoice tools stop at data capture. This one is built around governance:
> every policy is configuration, every decision is explainable, and every action —
> including every AI action — is recorded in a tamper-evident audit trail.
>
> Let me show you what that means in practice."

---

### SCENE 1 — Upload & AI extraction (0:25–1:25)

`[ACTION]` Frontend → invoice upload → drop `invoice_orion_1042.pdf` → wait for
extraction → show the extracted fields.

> "I'll upload a real invoice PDF.
>
> Behind the scenes, two things happen. First OCR pulls the raw text. Then Claude
> cleans it up — it merges fragmented line-item descriptions and normalizes the
> fields into structured JSON.
>
> And that's the important part: the AI's output isn't trusted blindly. It's
> validated against a strict schema before it touches the invoice. If the model
> returns something malformed, the result is rejected and the raw OCR data stands.
> The AI assists — it never decides."

`[ACTION]` Point at the confidence score and extracted line items.

> "We get the vendor, invoice number, date, total, tax — and a confidence score
> that drives what happens next."

---

### SCENE 2 — Duplicate detection (1:25–2:10)

`[ACTION]` Upload `invoice_orion_1051.pdf`. Show the duplicate warning.

> "Now I'll upload a second invoice from the same vendor. Different invoice
> number — but the amount is within a third of a percent, and it's dated three
> days later.
>
> The system flags it as a potential duplicate. Not a hard block — a soft warning,
> because sometimes those are legitimate. But it cannot be approved until a human
> reviews it and overrides it with a written reason. That reason goes into the
> audit trail."

---

### SCENE 3 — The Decision Inbox (2:10–3:00)

`[ACTION]` Swagger → `GET /api/v1/inbox` → Execute. Expand the response.

> "This is the Decision Inbox — one prioritized worklist across everything
> waiting on you.
>
> Each pending invoice is reduced to its single most blocking next step. Not a
> list of invoices — a list of decisions."

`[ACTION]` Point to the items.

> "This one is blocked because its vendor isn't verified yet. These are waiting on
> approval. And notice this first one is flagged **overdue** — it's been sitting in
> pending approval for seventy-two hours, past the forty-eight-hour SLA. Breached
> items sort to the top automatically."

`[ACTION]` Run `GET /api/v1/inbox?overdue_only=true`.

> "And I can filter to just the SLA breaches."

---

### SCENE 4 — SLA escalation (3:00–3:40)

`[ACTION]` Swagger → `POST /api/v1/inbox/escalate-overdue` → Execute.
Response shows `escalated_count: 1`, `escalated_to: "cfo"`.

> "SLAs are configured per workflow state — forty-eight hours in pending approval,
> then escalate to the CFO. The timer starts the moment an invoice enters a state.
>
> Running the escalation records an audit event and notifies the CFO. And it's
> idempotent — it escalates each breach exactly once per state entry, so I can
> safely wire it to a cron job.
>
> The escalated invoice also becomes visible in the CFO's inbox — even though the
> original approver was a manager. The original approval chain is preserved in the
> trail; escalation adds to it, it doesn't rewrite it."

---

### SCENE 5 — The AI agent: next action (3:40–4:30)

`[ACTION]` Swagger → `GET /api/v1/invoices/{id}/next-action` with the **INV-2003**
id (the Nimbus one) and `use_ai=false` → Execute.

> "This is the workflow agent. You ask it: what should happen to this invoice next?
>
> It answers `verify_vendor` — and it shows its work. These are the signals it
> reasoned from: the invoice is pending approval, and the vendor is still pending
> verification."

`[ACTION]` Run it again with `use_ai=true` on a clean invoice (INV-2002).

> "With AI enabled, Claude writes the explanation and scores its confidence — but
> here's the critical design decision: the AI is not allowed to choose the action.
> Policy determines what's permitted; the model can only phrase it.
>
> If the model tries to return a different action — say it decides to just approve
> something — that output is discarded, the deterministic result stands, and the
> attempt is logged as a schema failure. That's tested."

---

### SCENE 6 — Governance gates: SoD + vendor (4:30–5:15)

`[ACTION]` Swagger → Authorize as `manager@demo.com` → `POST /invoices/{INV-2004}/approve`.
Returns **403**.

> "Now the controls. Segregation of duties: this manager created this invoice, so
> they cannot approve it. Maker-checker, enforced in the service layer — and the
> blocked attempt is itself written to the audit trail."

`[ACTION]` Back as admin → `POST /invoices/{INV-2003}/approve` → **403**.

> "And here's the vendor gate. This invoice's vendor is still pending
> verification, so no one can approve it — not even an admin. Money doesn't move
> against an unverified vendor. Someone with vendor-management rights has to
> verify that vendor first, and *they* can't be the person who created it either."

---

### SCENE 7 — Live Audit Mode + tamper evidence (5:15–6:00)

`[ACTION]` Swagger → `GET /api/v1/audit/timeline/invoice/{INV-2003 id}`.

> "Every object opens as a full timeline — what happened, when, who did it, and
> why. Each event carries a plain-English reason, including the exact policy that
> routed the approval, snapshotted at the moment of the decision. So if the policy
> changes later, the history still shows what rule actually applied."

`[ACTION]` `GET /api/v1/audit/verify/invoice/{same id}` → `verified: true`.

> "And the audit trail is tamper-evident. Every entry is hash-chained to the one
> before it. If someone edited or deleted a row directly in the database, this
> check would fail and tell you exactly which event broke."

`[ACTION]` `GET /api/v1/audit/ai-actions`.

> "The same discipline applies to AI. Every single AI call is logged — which
> model, which prompt version, the confidence, the latency, and whether the output
> passed schema validation or fell back. Full reproducibility."

---

### SCENE 8 — Close (6:00–6:30)

`[ACTION]` Optionally show `GET /config/versions/workflow/invoice` (versioned config),
then the repo / test output `261 passed`.

> "Everything you saw is configuration, not hardcode — approval thresholds,
> workflow states, transition guards, SLAs. All editable through the API, all
> versioned, and any version can be rolled back.
>
> The whole thing is multi-tenant with PostgreSQL row-level security, and it's
> covered by two hundred and sixty-one tests.
>
> Thanks for watching."

---

## 4. Quick reference — IDs you'll need

Get every invoice id in one call so you can paste them during recording:
```powershell
# after authorizing in Swagger, or:
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/token -d "username=admin@demo.com&password=demo1234" | jq -r .access_token
curl -s http://127.0.0.1:8000/api/v1/invoices/ -H "Authorization: Bearer $TOKEN" | jq -r ".[] | \"\(.invoice_number) \(.id)\""
```

| Invoice | Scene | Expect |
|---|---|---|
| INV-2001 | 3, 4 | overdue, escalates to CFO |
| INV-2002 | 5 | `approve`, required_role **cfo** |
| INV-2003 | 5, 6, 7 | `verify_vendor`, approve → 403 |
| INV-2004 | 6 | manager approve → 403 (SoD) |
| INV-2005 | — | `mark_paid` |
| INV-2007 | — | `validate` |

---

## 5. Recording tips

- **Slow down.** Pause ~1 second after each click before speaking; it makes editing far easier.
- **Do the "money shots" twice** (duplicate flag, 403s, verify=true) so you have a spare take.
- Keep JSON responses **collapsed** until you talk about them, then expand — it directs the eye.
- If a take goes wrong, just re-run `_demo_seed.py` and start the scene again.
- Aim for **6 minutes**. Portfolio viewers rarely watch past that.
