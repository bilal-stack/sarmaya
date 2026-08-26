# Contributing

## Getting it running

The fastest path, and the one that needs nothing installed but Docker:

```bash
docker compose up --build
```

That brings up Postgres, applies every migration, seeds a demo tenant, and
serves the API on <http://localhost:8000/docs>. The schedulers are off by
default — nothing delivers a notification, escalates an SLA or posts to an
accounting system without them, so start them when you are working on any of
that:

```bash
docker compose --profile jobs up
```

Working locally instead:

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements-dev.txt
cp .env.example .env                                # then fill it in
alembic upgrade head
uvicorn app.main:app --reload
```

`.env.example` documents every variable, including which are required and
which have defaults. The app refuses to start with the placeholder
`SECRET_KEY` unless `DEBUG` is on — that is deliberate, and it is the check
that stops a deployment serving forgeable tokens.

## Tests

```bash
pytest -q
```

**Postgres is not optional.** The models use Postgres column types and the
isolation tests need real row-level security, so there is no SQLite path. The
suite builds its own database — `TEST_DATABASE_URL`, or `ADMIN_DATABASE_URL`
with `_test` appended — and never touches your development one.

Two things worth knowing before you trust a green run:

- If the database is unreachable, the integration tests **skip** rather than
  fail, so the suite still reports success having tested almost nothing. A
  suspiciously fast run is the tell.
- The RLS tests connect as `os_app`, a role that is deliberately not a
  superuser and cannot bypass RLS. Without that role they pass vacuously. See
  the README for creating it, or use `docker compose`, which does not need it
  because those tests build their own connection.

## Before you push

```bash
ruff check .
pytest -q
```

CI runs both on every push and every pull request, plus a check that the
migrations apply to an empty database — the suite builds its schema with
`create_all`, so a broken migration passes every test and fails on deploy
instead.

## Dependencies

`requirements.txt` carries deliberately wide ranges. `requirements.lock` pins
what actually gets installed, and it is what the Dockerfile builds from. Change
one and you must regenerate the other:

```bash
pip-compile --strip-extras --output-file=requirements.lock requirements.txt
```

CI regenerates it and fails if the result differs. Without that, a dependency
added to `requirements.txt` and never compiled into the lock would appear to be
added and quietly not be.

## Architecture, briefly

Requests go `api → service → repository`. Business rules live in services;
routes do validation, permission checks and status-code mapping and nothing
else. Two independent tenant locks are always in force — Postgres RLS, and
application-level scoping — and neither is a substitute for the other.

Some conventions that are load-bearing rather than stylistic:

- **Audit entries must be committed with the action they describe.** A test
  asserts it structurally (`test_audit_durability.py`), because the failure
  mode is silent: the action persists and the trail does not.
- **Money-moving actions carry segregation-of-duties rules with no admin
  exemption.** If you are adding one, it needs a rule and the rule needs a
  test that proves the refusal.
- **A new model that owns a correlation chain must be mapped** in
  `audit_service._VIEW_PERMISSION`, or its timeline becomes readable only by
  auditors. A registry test catches this.
- **A workflow needs both halves of its clock** — `WORKFLOW_TYPE` on the model
  *and* configured states. Declaring one without the other leaves records
  outside every SLA, invisibly. See DR-048; it happened twice in one week.

## Writing it down

Significant decisions go in `docs/DECISION_REGISTER.md` as a numbered `DR-NNN`
entry. Not what changed — the diff says that — but what was considered, what
was rejected, and why. Several entries exist specifically because somebody
later "simplified" a thing that looked redundant and was not.

Commit messages follow the same idea: imperative subject, then the reasoning.
If the change is a fix, say what the bug actually did.

## Releasing

`docs/REGRESSION_CHECKLIST.md` covers what CI cannot: SMTP actually delivering,
QuickBooks against real Intuit, whether the schedulers were installed at all,
and a restore having been performed at least once. Everything mechanical is
already automated and deliberately not repeated there.
