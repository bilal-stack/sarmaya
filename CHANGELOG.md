# Changelog

Notable changes to Sarmaya OS, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

**This project has never been released.** There are no tags and nothing is
deployed anywhere, so there is no version history to reconstruct and none is
invented here — a changelog listing releases that never happened is worse than
one that starts today. Everything built before this file existed is recorded in
`docs/DECISION_REGISTER.md`, which covers the same ground in more depth: DR-001
through DR-050, each with what was decided and what was rejected.

Entries start at the first release. Until then, changes land in `Unreleased`.

## [Unreleased]

### Added

- **Integration Hub (QuickBooks Online).** Released payments and paid expense
  claims post to a tenant's own accounting system as journal entries, with
  retries, dead letters and idempotency in three layers. Chart of accounts and
  vendor list pull back on demand. Built and tested; has never spoken to Intuit
  — that needs credentials. (DR-049)
- **Audit packs per period and per control**, alongside the existing per-chain
  pack. Reports applied/blocked/overridden for five controls, sealed with a
  SHA-256 hash. An empty result is sealed here rather than refused, because
  "this control did not fire in Q3" is a finding. (DR-050)
- **Blocked attempts report** — segregation-of-duties refusals, counted apart
  from other gates, with a panel on the Control Room. The data was already
  being written on every refusal and nothing surfaced it.
- **AP/Treasury reports and page** — invoice throughput, payment run status,
  duplicates and anomalies, at `/ai-tools/ap-treasury`. Two figures the Build
  Book asks for are reported as *absent* rather than as zero: match rate cannot
  be recovered, and a bank-side payment failure cannot be observed at all.
- **Regression checklist** (`docs/REGRESSION_CHECKLIST.md`), carrying only what
  CI and the deployment verifier cannot check.
- **CI** on both repositories: ruff, the migration chain against an empty
  database, and the full suite on every push and pull request. Dependabot
  enabled now that something exists to gate its pull requests.
- **`requirements.lock`**, pinning what the image installs. CI fails if it has
  drifted from `requirements.txt`.
- **Structured logging.** JSON in production, readable in development, with a
  request id that starts at the edge and appears on every log line the request
  produces — previously the id existed only inside the 500 handler, so the id a
  client was given matched exactly one entry and nothing leading to it.
- **`docker-compose.yml`** — Postgres, migrations, a seeded demo tenant and the
  API, with the schedulers behind a `jobs` profile.
- **ESLint** on the frontend, with a deliberately narrow rule set.

### Fixed

- **Rework rate reported 200%.** It divided rework *events* by invoices
  captured, so one invoice rejected twice scored double. Now counts affected
  invoices, with the event total reported separately.
- **A duplicate journal-post enqueue could discard the payment that caused
  it.** `enqueue` runs inside `release_payment`'s transaction, and a bare
  rollback on the unique-constraint conflict would have taken the state change,
  the audit entries and the settled invoices with it. Now a savepoint.
- **`UserNav` called hooks after an early return** — a latent violation, masked
  today by the component being a dynamic import.
- **Two "People" cards shared a React key** on the AI Tools index, leaving React
  free to drop or duplicate one.
- **Skipped integration posts were counted as retries**, so a tenant who
  disconnected with posts queued logged what looked like a hundred failures
  every five minutes.

### Documented

- `.env.example` gained every variable `scripts/bootstrap_tenant.py` reads —
  six of them, all previously undocumented, including which are required.
- `CONTRIBUTING.md`, covering setup, the Postgres-only test suite, the lockfile
  workflow, and the conventions that are load-bearing rather than stylistic.
