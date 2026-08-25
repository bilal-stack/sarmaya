# Regression checklist

Build Book, Definition of Done: *"Test pack: acceptance tests, regression
checklist, and performance smoke tests."*

**What this deliberately does not contain.** No item here re-checks something a
machine already checks. 1,167 tests run on every push, `ruff` runs with them,
the migration chain is applied to an empty database on every push, and
`scripts/verify_deployment.py` fails on a deployment that bypasses RLS, has
`DEBUG` on, leaves self-registration open, still carries demo accounts, or has
a table with no row-level security policy. A checklist that asked a human to
confirm any of that would be asking them to do a worse job of something already
being done, and the reliable outcome of a checklist full of pointless items is
that people stop reading the ones that matter.

So what is left here is the genuinely un-automatable: things that need a real
third party, a real browser, or a person deciding whether what they are looking
at is right.

**How to use it.** Work top to bottom. Items marked **[first deploy]** are one
-time; the rest are per release. Anything that fails is a release blocker
unless somebody writes down why it is not.

---

## 0. The mechanical gates — confirm they ran, do not repeat them

- [ ] CI is green on the commit being released — not on an earlier one.
      Check the run, not the badge: a workflow that was never triggered shows
      no failure either.
- [ ] The test step in that run took a plausible length of time (~75s for the
      backend). `tests/integration/conftest.py` calls `pytest.skip` when the
      database is unreachable, so a suite that tested nothing at all still
      reports success. A near-instant test step means it skipped.
- [ ] `python -m scripts.verify_deployment https://<api> --database-url <url> --origin https://<frontend>`
      passes against the deployed instance, after deploying.

## 1. The scheduled jobs are actually installed

The single most common quiet failure in this system. Nothing errors when a
cron was never created — the queues simply fill, and everything looks healthy
until somebody is waiting on a message or a ledger entry that will never come.

- [ ] Open `/ai-tools/system`. Every job shows a recent run, not "Not running".
      There are three: `dispatch_notifications` (1 min), `run_workflow_timers`
      (60 min), `dispatch_integration_posts` (5 min).
- [ ] A job that has never run at all is distinguishable from one that ran and
      failed. Both are visible here; only the second leaves anything in a log.

## 2. Email actually leaves the building

**Never verified anywhere.** There is no mail server in the dev environment,
so delivery is exercised only at the `_deliver` boundary — the code that hands
a message to SMTP is tested; SMTP accepting it is not.

- [ ] `SMTP_ENABLED=true` and the `SMTP_*` settings are set. While false,
      messages are *held* rather than attempted, which is correct behaviour and
      also means an unset mailer looks exactly like a working one from the
      queue's point of view.
- [ ] Trigger a real notification (submit an invoice for approval) and confirm
      it arrives in a real inbox.
- [ ] `GET /api/v1/notifications/queue?status=failed` is empty afterwards.
      Watch this specifically on the first deploy.

## 3. The money path, end to end, on the deployed instance

The suite proves each control in isolation. This proves they compose, against
real data shapes and a real browser.

- [ ] Upload a real supplier PDF. Extraction returns something sane — this is
      the one request that calls an external OCR provider synchronously, so it
      is also where a third-party outage shows up as a slow upload.
- [ ] Take the invoice through validate → submit → approve as the roles the
      policy requires, and confirm it refuses when the same person tries to do
      two steps that must be separate.
- [ ] Prepare a payment run, then attempt to release it **as the person who
      prepared it**. It must refuse. This is the single control the payments
      module exists for, and it is worth confirming by hand on every release.
- [ ] Release it as somebody else. The invoices settle.
- [ ] Download the bank file and confirm the hash on the payment record matches
      the file you actually received.

## 4. Third parties, with real credentials

Each of these is faked in the suite, on purpose — a test that talks to a live
vendor API fails for reasons that have nothing to do with the change.

- [ ] **AI**: with real keys set, an extraction and a classification return
      schema-valid output. Without keys the features degrade to their
      rule-based fallbacks rather than erroring — confirm that too, since it is
      the state most deployments are in.
- [ ] **QuickBooks** [first deploy]: connect via the real Intuit consent
      screen. The browser must genuinely leave the app and come back to
      `/ai-tools/system/integrations?connected=quickbooks`. Then set the
      posting accounts, release a payment, wait for the drain, and check the
      journal entry appears **in the client's own books** with the transaction
      id Sarmaya recorded. Nothing in the suite can prove this: the connector is
      faked throughout.
- [ ] **QuickBooks, second run**: run the drain again. The same entry must not
      post twice. The `DocNumber` check is the only thing standing between a
      lost response and a duplicated expense in somebody's ledger.

## 5. Browser-level behaviour

- [ ] Exports download and open: a dashboard as CSV, and an evidence pack whose
      seal recomputes from the downloaded file.
- [ ] The OAuth connect button performs a full navigation, not a `fetch`. If it
      silently does nothing, that regressed.
- [ ] Check the console on the main screens. A React key collision or a hook
      -order error is invisible to `tsc` and to the test suite, and shows up
      only here. (ESLint catches the second one at build; the first it does not.)
- [ ] One pass at a narrow viewport on the screens a person actually uses daily
      — the Decision Inbox and the invoice list.

## 6. Data safety

- [ ] [first deploy] A backup exists and **a restore has been performed at
      least once**. An untested backup is a belief, not a backup.
- [ ] Before any release carrying a migration: apply it to a *copy of
      production data*, not to an empty database. CI proves the chain applies to
      an empty schema, which says nothing about how long it takes or what it
      does to ten thousand existing rows.
- [ ] Confirm `alembic current` on the deployed database matches
      `alembic heads` in the release.

---

## Sign-off

| | |
|---|---|
| Release / commit | |
| Checked by | |
| Date | |
| Items skipped, and why | |

An item skipped with a stated reason is a decision. An item skipped silently is
the thing this document exists to prevent.
