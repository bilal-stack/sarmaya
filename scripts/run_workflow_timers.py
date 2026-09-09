"""Run the workflow clocks. Put this on a schedule.

Two jobs, both of which only happen if something runs them:

  * **Reminders** — nudge whoever can act on an item that has been waiting a
    while but is not yet late. The point is that the escalation never fires.
  * **Escalations** — when an SLA is breached, tell the escalation role once
    and record it.

Escalation has existed since DR-009 with "admin button, cron, or a scheduler
later" in its docstring. The cron never arrived, so until now a breach was only
escalated if an administrator happened to open the Decision Inbox and click
"Escalate overdue". A deadline nobody is watching is not a deadline.

    python -m scripts.run_workflow_timers

Hourly is plenty — SLAs are measured in hours or days, and both jobs are
idempotent: a breach already escalated since the record entered its state is
skipped, and a reminder is sent at most once per REMINDER_INTERVAL_HOURS.

Runs per tenant, because both services are tenant-scoped like every other
query. Exits non-zero if any tenant errors, so a scheduler notices.
"""
import argparse
import logging
import sys

from sqlalchemy import text

from app.core.config import settings
from app.core.database import SessionLocal, set_tenant_context
from app.core.logging_config import configure_logging
from app.core.roles import ADMIN

# Same configuration the API uses: readable lines in development, one JSON
# object per line in production. These jobs run unattended on a schedule,
# so their output is read in a platform log viewer far more often than by
# a person at a terminal.
configure_logging(debug=settings.DEBUG)
logger = logging.getLogger("workflow_timers")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-reminders", action="store_true",
        help="Escalate breaches only; send no reminders.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Log only when something actually happened.",
    )
    args = parser.parse_args()

    from app.services.sla_service import SlaService
    from app.services.system_health_service import record_job_run, job_clock
    from app.models.job_run import JOB_WORKFLOW_TIMERS

    session = SessionLocal()
    failures = 0
    try:
        tenants = session.execute(
            text("SELECT id, name FROM tenants WHERE is_active = true")
        ).fetchall()
    finally:
        session.close()

    for tenant_id, name in tenants:
        # A fresh session per tenant: set_tenant_context binds for the life of
        # the session, so reusing one would carry the first tenant's binding
        # into the next.
        tenant_session = SessionLocal()
        # Recorded whether the run succeeds or not - the console's question is
        # "did this job run at all", which a success-only record cannot answer.
        started_at = job_clock()
        run_error = None
        reminded = escalated = 0
        try:
            set_tenant_context(tenant_session, str(tenant_id))
            # The scheduler, not a person. Given the admin role for the tenant
            # it is working on and nothing beyond it.
            actor = {"id": None, "tenant_id": tenant_id, "role": ADMIN}
            service = SlaService(tenant_session)

            if not args.skip_reminders:
                reminded = service.run_reminders(actor)["reminded_count"]
            escalated = service.run_escalations(actor)["escalated_count"]

            if reminded or escalated or not args.quiet:
                logger.info(
                    "%s: reminded=%s escalated=%s", name, reminded, escalated
                )
        except Exception as exc:
            failures += 1
            run_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Workflow timers failed for tenant %s", name)
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
                tenant_session, tenant_id, JOB_WORKFLOW_TIMERS, started_at,
                items_processed=reminded + escalated, error=run_error,
            )
            tenant_session.close()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
