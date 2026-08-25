"""Drain the outbound queue to a tenant's own accounting system. Run this on
a schedule.

Payments and paid expense claims are queued in the same transaction as the
action that produced them (see `JournalPostingService.enqueue`) and posted
afterwards, outside anybody's request. This is the "afterwards" — without
something running it, entries queue and never reach the client's books.

    python -m scripts.dispatch_integration_posts

Every 5 minutes is a sensible cadence — see `JOB_INTEGRATION_POSTS` in
app/models/job_run.py for why that number and not the notification job's 1.
Safe to run concurrently with itself in the sense that matters: an entry
already posted is never picked up again, and the connector's own DocNumber
check means even a retry that races a previous attempt cannot double-post.

Runs per tenant, because the queue is tenant-scoped like everything else.
Binding each tenant in turn keeps the isolation boundary intact rather than
reaching around it for operational convenience — mirrors
scripts/dispatch_notifications.py exactly for this reason.

Exits non-zero if any tenant errors, so a scheduler notices.
"""
import argparse
import logging
import sys

from sqlalchemy import text

from app.core.database import SessionLocal, set_tenant_context
from app.core.roles import ADMIN

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("dispatch_integration_posts")


def _tenants(session):
    """Every active tenant. Read before any tenant is bound, so it sees all."""
    return session.execute(
        text("SELECT id, name FROM tenants WHERE is_active = true")
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Maximum posts per tenant per run (default: 100).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Log only when something was actually posted or failed.",
    )
    args = parser.parse_args()

    from app.services.integration_posting_service import JournalPostingService
    from app.services.system_health_service import record_job_run, job_clock
    from app.models.job_run import JOB_INTEGRATION_POSTS

    session = SessionLocal()
    failures = 0
    try:
        tenants = _tenants(session)
        for tenant_id, name in tenants:
            # A fresh session per tenant: set_tenant_context binds for the
            # life of the session, and reusing one would carry the first
            # tenant's binding into the next.
            tenant_session = SessionLocal()
            # Recorded whether the run succeeds or not, same reasoning as the
            # notification job: "did this job run at all" has to be
            # answerable from a failed run too, not only a successful one.
            started_at = job_clock()
            run_error = None
            processed = 0
            try:
                set_tenant_context(tenant_session, str(tenant_id))
                # The scheduler, not a person. Given the admin role for the
                # tenant it is draining and touches nothing else — the same
                # actor shape every other scheduled job in this codebase uses.
                actor = {"id": None, "tenant_id": tenant_id, "role": ADMIN}
                result = JournalPostingService(tenant_session).drain(
                    actor, limit=args.limit
                )
                processed = result["posted"]
                if result["attempted"] or not args.quiet:
                    logger.info(
                        "%s: attempted=%s posted=%s retrying=%s failed=%s "
                        "skipped=%s connections_expired=%s",
                        name, result["attempted"], result["posted"],
                        result["retrying"], result["failed"], result["skipped"],
                        result["connections_marked_expired"],
                    )
            except Exception as exc:
                failures += 1
                run_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Integration post drain failed for tenant %s", name)
            finally:
                # A failed run leaves the session in an aborted transaction,
                # where every further write is refused. Clearing it here -
                # where this session is owned and its work is already
                # committed - is what makes the failure recordable at all.
                if run_error:
                    tenant_session.rollback()
                record_job_run(
                    tenant_session, tenant_id, JOB_INTEGRATION_POSTS,
                    started_at, items_processed=processed, error=run_error,
                )
                tenant_session.close()
    finally:
        session.close()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
