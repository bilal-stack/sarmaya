"""Drain the notification outbox. Run this on a schedule.

Notifications are queued in the same transaction as the action that produced
them and delivered afterwards, outside anybody's request. This is the
"afterwards" — without something running it, messages queue and never send.

    python -m scripts.dispatch_notifications

Every minute is a sensible cadence; it is cheap when the queue is empty and
safe to run concurrently with itself in the sense that matters — a message
already sent is never picked up again. Failures back off and retry, and a
message that keeps failing is marked failed rather than retried forever.

Runs per tenant, because the dispatcher is tenant-scoped like everything else.
Binding each tenant in turn keeps the isolation boundary intact rather than
reaching around it for operational convenience.

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
logger = logging.getLogger("dispatch_notifications")


def _tenants(session):
    """Every active tenant. Read before any tenant is bound, so it sees all."""
    return session.execute(
        text("SELECT id, name FROM tenants WHERE is_active = true")
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Maximum messages per tenant per run (default: 200).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Log only when something was actually sent or failed.",
    )
    args = parser.parse_args()

    from app.core.config import settings
    if not settings.SMTP_ENABLED:
        logger.warning(
            "SMTP_ENABLED is false, so nothing will be delivered. Messages stay "
            "queued — turning it on later sends the backlog."
        )

    from app.services.notification_dispatcher import NotificationDispatcher
    from app.services.system_health_service import record_job_run, job_clock
    from app.models.job_run import JOB_NOTIFICATIONS

    session = SessionLocal()
    failures = 0
    try:
        tenants = _tenants(session)
        for tenant_id, name in tenants:
            # A fresh session per tenant: set_tenant_context binds for the life
            # of the session, and reusing one would carry the first tenant's
            # binding into the next.
            tenant_session = SessionLocal()
            # Recorded whether the run succeeds or not: the admin console's
            # main question is "did this job run at all", and a run that only
            # reports itself on success cannot answer it.
            started_at = job_clock()
            run_error = None
            processed = 0
            try:
                set_tenant_context(tenant_session, str(tenant_id))
                # The dispatcher checks a permission, so it needs an actor. This
                # is the scheduler, not a person: it is given the admin role for
                # the tenant it is draining and touches nothing else.
                actor = {"id": None, "tenant_id": tenant_id, "role": ADMIN}
                result = NotificationDispatcher(tenant_session).dispatch(
                    actor, limit=args.limit
                )
                processed = result["sent"]
                if result["attempted"] or not args.quiet:
                    logger.info(
                        "%s: attempted=%s sent=%s retrying=%s failed=%s",
                        name, result["attempted"], result["sent"],
                        result["retrying"], result["failed"],
                    )
            except Exception as exc:
                failures += 1
                run_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Dispatch failed for tenant %s", name)
            finally:
                # A run that failed inside the database leaves the session in
                # an aborted transaction, where every further write is refused.
                # Clearing it here - where this session is owned and its work is
                # already committed - is what makes the failure recordable at
                # all. Without it the console goes quiet exactly when the job
                # starts failing, which reads identically to the job having
                # stopped, and the two need opposite responses.
                if run_error:
                    tenant_session.rollback()
                record_job_run(
                    tenant_session, tenant_id, JOB_NOTIFICATIONS,
                    started_at, items_processed=processed, error=run_error,
                )
                tenant_session.close()
    finally:
        session.close()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
