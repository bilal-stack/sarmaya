"""Check that a deployment is actually sound, not merely responding.

A deployment can return 200 on every endpoint and still be wrong in ways
nothing surfaces:

  * The app connected as a **superuser or the database owner**, which bypasses
    every row-level security policy. The application-level tenant scoping still
    works, so nothing looks broken — the second lock is simply gone, and there
    is no symptom until there is an incident. This is the easy one to get wrong:
    the connection string a hosting provider hands you is the owner's.
  * **Migrations not at head**, so a table added last week is missing and the
    feature that needs it 500s the first time somebody opens it.
  * **DEBUG left on**, which returns the exception type and message to whoever
    provoked the error.
  * **Self-registration open**, letting a stranger enrol into a tenant.
  * **CORS not admitting the real frontend**, so the app loads and every request
    fails in the browser with nothing useful in the server log.
  * **No tenant bootstrapped**, so nobody can sign in at all.

Run it after deploying, and again after any change to the environment:

    python -m scripts.verify_deployment https://sarmaya-api.onrender.com

Add the database URL to include the checks that need it — the RLS one in
particular, which is the reason this script exists:

    python -m scripts.verify_deployment https://... --database-url postgresql://...

Add your frontend's origin to check CORS the way a browser would:

    python -m scripts.verify_deployment https://... --origin https://sarmaya.vercel.app

Exits non-zero if anything failed, so it can gate a release.
"""
import argparse
from datetime import datetime, timedelta
import json
import sys
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

#: (ok, headline, detail). `ok is None` means "could not check", which is
#: reported separately — an unchecked control is not a passing one.
Result = Tuple[Optional[bool], str, str]

TIMEOUT = 30


def _request(url: str, method: str = "GET", headers: Optional[dict] = None,
             body: Optional[dict] = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)
    except Exception as exc:  # connection-level problems are results too
        return 0, str(exc), {}


# --- checks against the running API -----------------------------------------

def check_alive(api: str) -> Result:
    status, body, _ = _request(f"{api}/api/v1/ping")
    if status == 0:
        return False, "API unreachable", body[:200]
    if status != 200:
        return False, "API not healthy", f"/api/v1/ping returned {status}"
    return True, "API is up", ""


def check_debug_is_off(api: str, token: Optional[str] = None) -> Result:
    """DEBUG returns the exception type and message to the caller.

    Only visible on a 500, and a 500 needs a request that gets past
    authentication — so without a token this cannot be answered. The first
    version of this check probed an unauthenticated route, got a 401, saw no
    `debug` key in it and reported DEBUG off on a deployment where it was on:
    a pass earned by never reaching the thing being tested.

    So it reports "not checked" unless it actually provoked an error body.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    status, body, _ = _request(f"{api}/api/v1/invoices/not-a-uuid", headers=headers)

    if "\"debug\"" in body or "'debug'" in body:
        return False, "DEBUG is on", (
            "Error responses include the exception type and message. "
            "Set DEBUG=false."
        )
    if status >= 500:
        return True, "DEBUG is off", "a server error carried no internals"
    return None, "DEBUG not checked", (
        f"the probe returned {status}, not a server error, so nothing would "
        "have leaked either way. Pass --token for an authenticated probe."
    )


def check_registration_closed(api: str) -> Result:
    status, body, _ = _request(
        f"{api}/api/v1/auth/register?tenant=demo",
        method="POST",
        body={"email": "probe@verify.invalid", "password": "x" * 16},
    )
    if status == 201:
        return False, "Self-registration is OPEN", (
            "A stranger can enrol into a tenant by naming its slug and then "
            "create vendors, raise invoices and prepare payment runs. Set "
            "ALLOW_SELF_REGISTRATION=false."
        )
    if status == 403:
        return True, "Self-registration is closed", ""
    if status == 400:
        # The ALLOW_SELF_REGISTRATION gate is the first thing the endpoint
        # checks; a 400 means it was passed and the request failed later, on
        # the tenant slug. So registration is open even though this particular
        # attempt did not create anything.
        return False, "Self-registration is OPEN", (
            "The request got past the gate and failed only on the tenant name "
            f"({body[:80]}). A stranger who knows a real slug would be enrolled. "
            "Set ALLOW_SELF_REGISTRATION=false."
        )
    return None, "Self-registration state unclear", f"register returned {status}: {body[:120]}"


def check_no_demo_accounts(api: str) -> Result:
    """The seeded accounts share a password published in the repository."""
    status, _, _ = _request(
        f"{api}/api/v1/auth/login?tenant=demo",
        method="POST",
        body={"email": "admin@demo.com", "password": "password123"},
    )
    if status == 200:
        return False, "DEMO ADMIN ACCOUNT IS LIVE", (
            "admin@demo.com signs in with the password published in this "
            "repository. Delete the demo tenant's users immediately."
        )
    return True, "No demo admin account", "the published credentials do not work"


def check_cors(api: str, origin: Optional[str]) -> Result:
    if not origin:
        return None, "CORS not checked", "pass --origin to check it"
    status, _, headers = _request(
        f"{api}/api/v1/ping",
        method="OPTIONS",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    allowed = headers.get("access-control-allow-origin") or headers.get(
        "Access-Control-Allow-Origin"
    )
    if allowed in (origin, "*"):
        return True, "CORS admits the frontend", f"{origin} is allowed"
    return False, "CORS refuses the frontend", (
        f"{origin} is not in CORS_ORIGINS (preflight returned {status}, "
        f"allow-origin {allowed!r}). Every request from the app will fail in "
        "the browser."
    )


def check_migrated_schema(api: str) -> Result:
    """A route from the newest module answers as a route, not as a crash.

    401 means the code and its tables are both there and it got as far as
    asking who you are. 500 means the endpoint exists and its table does not.
    """
    status, body, _ = _request(f"{api}/api/v1/requisitions")
    if status in (401, 403):
        return True, "Newest module is deployed", "requisitions route responds"
    if status == 404:
        return False, "Newest module missing", (
            "/api/v1/requisitions is not routed — the deployed image predates it."
        )
    if status >= 500:
        return False, "Schema is behind the code", (
            "The requisitions route exists but errors, which usually means "
            "`alembic upgrade head` has not run. " + body[:120]
        )
    return None, "Schema state unclear", f"requisitions returned {status}"


# --- checks that need the database ------------------------------------------

def check_database(database_url: str) -> List[Result]:
    """The checks worth the trouble of a database connection.

    The RLS one is the reason this script exists: it is invisible from outside,
    it is easy to get wrong, and getting it wrong removes a control silently.
    """
    results: List[Result] = []
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        return [(None, "Database checks skipped", "SQLAlchemy is not installed here")]

    try:
        engine = create_engine(database_url, connect_args={"connect_timeout": 15})
        with engine.connect() as conn:
            role, is_super, bypass = conn.execute(text(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )).one()

            if is_super or bypass:
                results.append((False, "THE APP BYPASSES ROW-LEVEL SECURITY", (
                    f"DATABASE_URL connects as '{role}', which is "
                    f"{'a superuser' if is_super else 'BYPASSRLS'}. Every RLS "
                    "policy is skipped for this connection, so tenant isolation "
                    "rests on the application layer alone. Create a plain role "
                    "and point DATABASE_URL at it — see the README."
                )))
            else:
                results.append((True, "App role cannot bypass RLS",
                                f"connected as '{role}'"))

            version = conn.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar()
            results.append((True, f"Migrations at {version}",
                            "compare with `alembic heads`"))

            unprotected = conn.execute(text("""
                SELECT c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN information_schema.columns col
                  ON col.table_name = c.relname AND col.column_name = 'tenant_id'
                WHERE n.nspname = 'public' AND c.relkind = 'r'
                  AND NOT c.relrowsecurity
                ORDER BY 1
            """)).scalars().all()
            if unprotected:
                results.append((False, "Tables carry tenant_id without RLS",
                                ", ".join(unprotected)))
            else:
                results.append((True, "Every tenant-owned table has RLS", ""))

            # Notifications are queued and drained by a scheduler. If nobody
            # runs the drain, the app looks perfectly healthy while telling
            # nobody anything — no approval requests, no SLA escalations, no
            # watchlist alerts. Invisible from outside, which is the same
            # argument that put the RLS check in this script.
            #
            # Counted per tenant with the GUC bound, for the reason spelled out
            # below the users check: through the app's role with no tenant set,
            # RLS hides every row, so a plain COUNT(*) here returns 0 on a
            # completely stuck queue. The first version of this check did
            # exactly that and reported a healthy queue against a deliberately
            # stalled one — a check that can only pass is worse than none.
            try:
                queue_tenants = conn.execute(text("SELECT id FROM tenants")).scalars().all()
                pending = 0
                failed = 0
                oldest_pending = None
                for tenant_id in queue_tenants:
                    conn.execute(
                        text("SELECT set_config('app.current_tenant_id', :t, false)"),
                        {"t": str(tenant_id)},
                    )
                    row = conn.execute(text("""
                        SELECT MIN(created_at) FILTER (WHERE status = 'pending'),
                               COUNT(*) FILTER (WHERE status = 'pending'),
                               COUNT(*) FILTER (WHERE status = 'failed')
                        FROM notification_outbox
                    """)).one()
                    if row[0] and (oldest_pending is None or row[0] < oldest_pending):
                        oldest_pending = row[0]
                    pending += row[1] or 0
                    failed += row[2] or 0
                conn.execute(
                    text("SELECT set_config('app.current_tenant_id', '', false)")
                )
                queue_readable = True
            except Exception as exc:
                queue_readable = False
                results.append((None, "Notification queue not checked", str(exc)[:120]))

            if queue_readable:
                stale = bool(
                    oldest_pending
                    and (datetime.utcnow() - oldest_pending) > timedelta(hours=1)
                )
                if stale:
                    results.append((False, "NOTIFICATIONS ARE NOT BEING SENT", (
                        f"{pending} message(s) queued, oldest from "
                        f"{oldest_pending:%Y-%m-%d %H:%M} UTC. Either no "
                        "scheduler is running `python -m scripts."
                        "dispatch_notifications`, or SMTP_ENABLED is false. "
                        "Approval requests and SLA escalations are going "
                        "nowhere."
                    )))
                elif failed:
                    results.append((False, f"{failed} notification(s) gave up", (
                        "Delivery failed repeatedly. Check "
                        "GET /api/v1/notifications/queue?status=failed, fix the "
                        "cause, then POST /api/v1/notifications/queue/retry-failed."
                    )))
                else:
                    results.append((True, "Notification queue is moving",
                                    f"{pending} pending, none stale"))

            # Counted per tenant with the GUC bound, exactly as a request does.
            # A plain `SELECT COUNT(*) FROM users` through the app's role
            # returns 0 no matter how many users exist, because RLS hides
            # every row when no tenant is bound — the first version of this
            # check read that as "nobody can sign in" on a database with a
            # perfectly good administrator.
            tenants = conn.execute(text("SELECT id, name FROM tenants")).all()
            if not tenants:
                results.append((False, "No tenant exists", (
                    "Run `python -m scripts.bootstrap_tenant`; nobody can sign "
                    "in to an empty database."
                )))
            else:
                populated = []
                for tenant_id, name in tenants:
                    conn.execute(
                        text("SELECT set_config('app.current_tenant_id', :t, false)"),
                        {"t": str(tenant_id)},
                    )
                    count = conn.execute(text(
                        "SELECT COUNT(*) FROM users WHERE is_active"
                    )).scalar()
                    populated.append((name, count))
                conn.execute(
                    text("SELECT set_config('app.current_tenant_id', '', false)")
                )

                empty = [name for name, count in populated if not count]
                if empty:
                    results.append((False, "A tenant has no users", (
                        f"{', '.join(empty)} — nobody can sign in to it. Run "
                        "`python -m scripts.bootstrap_tenant`."
                    )))
                else:
                    summary = ", ".join(f"{name}: {c} user(s)" for name, c in populated)
                    results.append((True, f"{len(tenants)} tenant(s) with users", summary))

                # Binding a tenant and getting rows back proves the policies
                # admit the right ones, not merely that they block.
                if any(c for _, c in populated):
                    results.append((True, "RLS admits a bound tenant's own rows",
                                    "policies filter rather than deny"))

    except Exception as exc:
        results.append((False, "Database unreachable", str(exc)[:200]))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check that a Sarmaya deployment is sound, not just responding.",
    )
    parser.add_argument("api_url", help="e.g. https://sarmaya-api.onrender.com")
    parser.add_argument("--database-url", help="enables the RLS and schema checks")
    parser.add_argument("--origin", help="your frontend's origin, to check CORS")
    parser.add_argument("--token", help="an access token, so the DEBUG check can "
                                        "provoke a real error")
    args = parser.parse_args()

    api = args.api_url.rstrip("/")
    print(f"Checking {api}\n")

    results: List[Result] = [
        check_alive(api),
        check_migrated_schema(api),
        check_debug_is_off(api, args.token),
        check_registration_closed(api),
        check_no_demo_accounts(api),
        check_cors(api, args.origin),
    ]
    if args.database_url:
        results.extend(check_database(args.database_url))
    else:
        results.append((None, "Database checks skipped", (
            "pass --database-url to check whether the app can bypass RLS, "
            "which is the failure this script mainly exists to catch"
        )))

    for ok, headline, detail in results:
        mark = {True: "  ok  ", False: " FAIL ", None: "  ?   "}[ok]
        print(f"[{mark}] {headline}")
        if detail:
            print(f"          {detail}")

    failed = [r for r in results if r[0] is False]
    unknown = [r for r in results if r[0] is None]
    print()
    print(f"{len(results) - len(failed) - len(unknown)} passed, "
          f"{len(failed)} failed, {len(unknown)} not checked")
    if unknown and not failed:
        print("An unchecked control is not a passing one.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
