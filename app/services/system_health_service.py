"""What the admin console shows when somebody asks "is anything broken?"

Definition of Done, admin console: config screens, job monitor, audit viewer,
error monitor. The first three exist as their own screens. This is the fourth,
and it is deliberately built around a different question than the others ask.

The failures worth catching here are the quiet ones. A background job that
throws is already in the logs and already retried; a background job that
*stopped* produces nothing at all - no error, no queue growth if the system is
idle, no signal of any kind until somebody notices a week later that no email
has arrived since Tuesday. So the primary reading is staleness, not errors:
every scheduled job reports when it last ran, and a job that has not run within
a generous multiple of its expected cadence is the headline.

The rest is the evidence that supports it: what is stuck in the outbox, what
the AI layer rejected, and whether delivery is switched off entirely - which is
a legitimate configuration, and therefore reported as configuration rather than
as a fault, because a monitor that cries wolf about a deliberate setting is a
monitor people learn to ignore.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.roles import has_permission, PERM_VIEW_AUDIT
from app.models.ai_action_log import AIActionLog
from app.models.job_run import (
    JobRun, EXPECTED_INTERVAL_MINUTES, STATUS_ERROR,
    JOB_NOTIFICATIONS, JOB_WORKFLOW_TIMERS,
)
from app.models.notification_outbox import (
    NotificationOutbox, STATUS_PENDING, STATUS_FAILED, MAX_ATTEMPTS,
)
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

#: How far past its cadence a job may drift before it is called late. A job due
#: every minute that is 3 minutes late is a blip on a busy host; one that is an
#: hour late has stopped. The multiple absorbs the former without hiding the
#: latter.
LATE_MULTIPLE = 10

#: A floor under that multiple, so the per-minute job is not declared dead for
#: a 10-minute host restart while the hourly one gets a sensible 10 hours.
MIN_LATE_MINUTES = 15

#: How far back the error counts look. A day is what somebody means by "is it
#: broken now" - a week of history would bury today's incident in last week's.
ERROR_WINDOW_HOURS = 24

#: A pending message older than this has missed many drains. Distinct from
#: "late job": this is evidence in the queue itself, and it is what shows up
#: when the dispatcher is running but failing to make progress.
STUCK_MESSAGE_MINUTES = 30

HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_DOWN = "down"

#: Ordered worst-first, so combining component readings is a min() over this.
_SEVERITY = {HEALTH_DOWN: 0, HEALTH_DEGRADED: 1, HEALTH_OK: 2}

MONITORED_JOBS = (JOB_NOTIFICATIONS, JOB_WORKFLOW_TIMERS)


def _now():
    """Naive UTC, matching the columns. See DR-011."""
    return make_naive(to_utc(utc_now()))


def job_clock():
    """The clock the scheduled scripts stamp their runs with.

    Public so a script does not have to reach for a private helper or restate
    the naive-UTC conversion, which is the kind of duplication that ends with
    one caller storing an aware timestamp and the monitor reporting a job as
    hours late.
    """
    return _now()


def _worst(*statuses: str) -> str:
    return min(statuses, key=lambda s: _SEVERITY[s])


class SystemHealthService:
    def __init__(self, db: Session):
        self.db = db

    def report(self, current_user: dict) -> Dict:
        """The whole monitor in one read.

        One call rather than four, because the console shows these together and
        the interesting cases are the correlations - a stalled queue *and* a
        late dispatcher is one incident, not two.
        """
        if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "view system health"
            )

        jobs = self._jobs()
        queue = self._notification_queue()
        ai = self._ai_failures()

        status = _worst(
            *(j["status"] for j in jobs),
            queue["status"],
            ai["status"],
        )

        return {
            "status": status,
            "checked_at": _now(),
            "jobs": jobs,
            "notifications": queue,
            "ai": ai,
            "notes": self._configuration_notes(),
        }

    # --- scheduled jobs ------------------------------------------------------

    def _jobs(self) -> List[Dict]:
        """Each job's last run, and whether that is recent enough.

        A job that has *never* run reads as down rather than unknown. On a
        fresh deployment that is briefly a false alarm; the alternative is a
        console that stays silent about a cron nobody remembered to install,
        which is the exact failure this screen exists to catch.
        """
        now = _now()
        readings = []

        for job_name in MONITORED_JOBS:
            last = (
                self.db.query(JobRun)
                .filter(JobRun.job_name == job_name)
                .order_by(JobRun.started_at.desc())
                .first()
            )
            expected = EXPECTED_INTERVAL_MINUTES.get(job_name, 60)
            tolerance = max(expected * LATE_MULTIPLE, MIN_LATE_MINUTES)

            if last is None:
                readings.append({
                    "job": job_name,
                    "status": HEALTH_DOWN,
                    "last_run_at": None,
                    "minutes_since": None,
                    "expected_every_minutes": expected,
                    "last_error": None,
                    "items_processed": 0,
                    "failures_in_window": 0,
                    "detail": "Has never run. Check that the scheduler is "
                              "installed and pointing at this deployment.",
                })
                continue

            age = (now - last.started_at).total_seconds() / 60.0
            recent_failures = (
                self.db.query(func.count(JobRun.id))
                .filter(
                    JobRun.job_name == job_name,
                    JobRun.status == STATUS_ERROR,
                    JobRun.started_at >= now - timedelta(hours=ERROR_WINDOW_HOURS),
                )
                .scalar() or 0
            )

            if age > tolerance:
                status = HEALTH_DOWN
                detail = (
                    f"Last ran {int(age)} minutes ago; expected every "
                    f"{expected}. The scheduler has probably stopped."
                )
            elif last.status == STATUS_ERROR:
                status = HEALTH_DEGRADED
                detail = "Its most recent run failed."
            elif recent_failures:
                status = HEALTH_DEGRADED
                detail = (
                    f"Running, but {recent_failures} run(s) failed in the last "
                    f"{ERROR_WINDOW_HOURS} hours."
                )
            else:
                status = HEALTH_OK
                detail = "Running on schedule."

            readings.append({
                "job": job_name,
                "status": status,
                "last_run_at": last.started_at,
                "minutes_since": round(age, 1),
                "expected_every_minutes": expected,
                "last_error": last.error,
                "items_processed": last.items_processed,
                "failures_in_window": recent_failures,
                "detail": detail,
            })

        return readings

    # --- the notification queue ----------------------------------------------

    def _notification_queue(self) -> Dict:
        """What is waiting, and what has been given up on.

        Delivery being switched off is not a fault - it is the documented
        default until SMTP is configured - so a queue backing up behind a
        disabled dispatcher is reported as configuration. Reporting it as an
        error would train people to ignore this panel.
        """
        now = _now()

        pending = (
            self.db.query(func.count(NotificationOutbox.id))
            .filter(NotificationOutbox.status == STATUS_PENDING)
            .scalar() or 0
        )
        failed = (
            self.db.query(func.count(NotificationOutbox.id))
            .filter(NotificationOutbox.status == STATUS_FAILED)
            .scalar() or 0
        )
        stuck = (
            self.db.query(func.count(NotificationOutbox.id))
            .filter(
                NotificationOutbox.status == STATUS_PENDING,
                NotificationOutbox.created_at
                <= now - timedelta(minutes=STUCK_MESSAGE_MINUTES),
            )
            .scalar() or 0
        )
        oldest = (
            self.db.query(func.min(NotificationOutbox.created_at))
            .filter(NotificationOutbox.status == STATUS_PENDING)
            .scalar()
        )

        delivery_enabled = bool(getattr(settings, "SMTP_ENABLED", False))

        if not delivery_enabled:
            status = HEALTH_OK
            detail = (
                f"Email delivery is off (SMTP_ENABLED is false), so {pending} "
                "queued message(s) are held rather than failed. In-app "
                "notifications are unaffected."
            )
        elif failed:
            status = HEALTH_DEGRADED
            detail = (
                f"{failed} message(s) gave up after {MAX_ATTEMPTS} attempts. "
                "They will not retry on their own."
            )
        elif stuck:
            status = HEALTH_DEGRADED
            detail = (
                f"{stuck} message(s) have been pending over "
                f"{STUCK_MESSAGE_MINUTES} minutes. The dispatcher is not "
                "making progress."
            )
        else:
            status = HEALTH_OK
            detail = "Draining normally." if pending else "Empty."

        return {
            "status": status,
            "pending": pending,
            "failed": failed,
            "stuck": stuck,
            "oldest_pending_at": oldest,
            "delivery_enabled": delivery_enabled,
            "detail": detail,
        }

    # --- the AI layer --------------------------------------------------------

    def _ai_failures(self) -> Dict:
        """Schema rejections and errors over the last day.

        A schema rejection is the router working - a malformed model response
        was refused rather than trusted - so a few are unremarkable and only a
        rate worth investigating is surfaced. Errors are different: those are
        requests that produced nothing.
        """
        since = _now() - timedelta(hours=ERROR_WINDOW_HOURS)

        rows = (
            self.db.query(AIActionLog.status, func.count(AIActionLog.id))
            .filter(AIActionLog.created_at >= since)
            .group_by(AIActionLog.status)
            .all()
        )
        counts = {status: count for status, count in rows}
        total = sum(counts.values())
        errors = counts.get("error", 0)
        rejected = counts.get("failed_schema", 0)

        if errors and total and errors / total > 0.25:
            status = HEALTH_DEGRADED
            detail = (
                f"{errors} of {total} AI requests errored in the last "
                f"{ERROR_WINDOW_HOURS} hours. Check the provider key and quota."
            )
        elif errors:
            status = HEALTH_DEGRADED
            detail = (
                f"{errors} AI request(s) errored in the last "
                f"{ERROR_WINDOW_HOURS} hours."
            )
        elif rejected:
            status = HEALTH_OK
            detail = (
                f"{rejected} response(s) failed schema validation and were "
                "refused rather than trusted. That is the guard working."
            )
        else:
            status = HEALTH_OK
            detail = "No AI failures." if total else "No AI activity."

        return {
            "status": status,
            "window_hours": ERROR_WINDOW_HOURS,
            "total": total,
            "errors": errors,
            "schema_rejections": rejected,
            "by_status": counts,
            "detail": detail,
        }

    # --- configuration -------------------------------------------------------

    def _configuration_notes(self) -> List[str]:
        """Settings that change what the readings above mean.

        Stated rather than inferred, so nobody has to reverse-engineer why a
        panel is green while nothing is being delivered.
        """
        notes = []
        if not getattr(settings, "SMTP_ENABLED", False):
            notes.append(
                "SMTP_ENABLED is false - email is queued but not sent. Set it, "
                "with the SMTP_* values, to enable delivery."
            )
        if not getattr(settings, "AI_API_KEY", None):
            notes.append(
                "No AI provider key is configured - AI features fall back to "
                "their deterministic paths."
            )
        return notes


def record_job_run(
    db: Session, tenant_id, job_name: str, started_at,
    items_processed: int = 0, error: Optional[str] = None,
) -> None:
    """Write the heartbeat for one job run against one tenant.

    Called by the scheduled scripts. Errors here are logged and swallowed: a
    monitoring write must never be the reason the work it monitors fails.

    Deliberately does *not* roll the session back, even though a failed run
    usually leaves one poisoned and unwritable. Clearing that is the caller's
    call, because the caller is the only one that knows whether the session is
    holding anything worth keeping - a recorder that rolled back on its own
    would quietly discard a caller's uncommitted work the first time somebody
    used it from inside a request. The scheduled scripts own a disposable
    per-tenant session and roll it back themselves before calling here.
    """
    from app.models.job_run import STATUS_OK, RETENTION_DAYS

    try:
        db.add(JobRun(
            tenant_id=tenant_id,
            job_name=job_name,
            status=STATUS_ERROR if error else STATUS_OK,
            started_at=started_at,
            finished_at=_now(),
            items_processed=items_processed,
            error=(error or "")[:500] or None,
        ))

        cutoff = _now() - timedelta(days=RETENTION_DAYS)
        (
            db.query(JobRun)
            .filter(JobRun.job_name == job_name, JobRun.started_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
    except Exception:
        logger.exception("Could not record job run for %s", job_name)
        db.rollback()
