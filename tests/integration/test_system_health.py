"""The admin console's error monitor.

The thing this screen exists to catch is a job that *stopped*. That failure has
no error, no exception, no log line — an idle system with a dead dispatcher
looks exactly like an idle system with a healthy one, right up until somebody
is waiting on a message that will never arrive.

So the tests that matter here are the ones about silence: a job that has never
run, a job whose last run has aged out, and a heartbeat that survives the run
it is reporting on having failed. The rest — queue counts, AI errors — is
supporting evidence, and is tested mostly to make sure it does not raise a
false alarm about a deliberate configuration.
"""
import uuid
from datetime import timedelta

import pytest

from app.core.enums import UserRole
from app.models.ai_action_log import AIActionLog
from app.models.job_run import (
    JobRun, JOB_NOTIFICATIONS, JOB_WORKFLOW_TIMERS, STATUS_ERROR, STATUS_OK,
    RETENTION_DAYS,
)
from app.models.notification_outbox import (
    NotificationOutbox, STATUS_PENDING, STATUS_FAILED,
)
from app.services.system_health_service import (
    SystemHealthService, record_job_run, job_clock,
    HEALTH_OK, HEALTH_DEGRADED, HEALTH_DOWN, STUCK_MESSAGE_MINUTES,
)

pytestmark = pytest.mark.integration


def _run(db, tenant_id, job=JOB_NOTIFICATIONS, minutes_ago=0, status=STATUS_OK,
         error=None, processed=0):
    started = job_clock() - timedelta(minutes=minutes_ago)
    row = JobRun(
        id=uuid.uuid4(), tenant_id=tenant_id, job_name=job, status=status,
        started_at=started, finished_at=started, items_processed=processed,
        error=error,
    )
    db.add(row)
    db.flush()
    return row


def _heartbeat_all(db, tenant_id):
    """Both jobs beating, so a test can isolate one component."""
    _run(db, tenant_id, JOB_NOTIFICATIONS, minutes_ago=0)
    _run(db, tenant_id, JOB_WORKFLOW_TIMERS, minutes_ago=1)


def _queued(db, tenant_id, user_id, minutes_ago=0, status=STATUS_PENDING):
    created = job_clock() - timedelta(minutes=minutes_ago)
    row = NotificationOutbox(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user_id,
        channel="email", subject="Something", body="Body",
        status=status, created_at=created,
    )
    db.add(row)
    db.flush()
    return row


class TestTheSilentFailure:
    """A job that stopped. No error is raised anywhere, so if the monitor does
    not notice this, nothing does."""

    def test_a_job_that_never_ran_reads_as_down(self, db, tenant, make_user):
        """A fresh deployment where nobody installed the cron. Reporting
        "unknown" here would be technically truer and operationally useless —
        the console would stay quiet about the exact thing it is for."""
        admin = make_user(UserRole.ADMIN)

        report = SystemHealthService(db).report(admin)

        dispatcher = next(
            j for j in report["jobs"] if j["job"] == JOB_NOTIFICATIONS
        )
        assert dispatcher["status"] == HEALTH_DOWN
        assert dispatcher["last_run_at"] is None
        assert "never run" in dispatcher["detail"]
        assert report["status"] == HEALTH_DOWN

    def test_a_job_that_stopped_hours_ago_reads_as_down(
        self, db, tenant, make_user
    ):
        """The dangerous case: it ran, so history exists and everything looks
        configured. Only the age of the last run gives it away."""
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=180)
        _run(db, tenant.id, JOB_WORKFLOW_TIMERS, minutes_ago=1)

        report = SystemHealthService(db).report(admin)

        dispatcher = next(
            j for j in report["jobs"] if j["job"] == JOB_NOTIFICATIONS
        )
        assert dispatcher["status"] == HEALTH_DOWN
        assert "scheduler has probably stopped" in dispatcher["detail"]

    def test_a_recent_run_is_healthy(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)

        report = SystemHealthService(db).report(admin)

        assert all(j["status"] == HEALTH_OK for j in report["jobs"])
        assert report["status"] == HEALTH_OK

    def test_a_brief_delay_is_not_an_incident(self, db, tenant, make_user):
        """The per-minute job, five minutes late. A monitor that calls this an
        outage is one people mute, and a muted monitor catches nothing."""
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=5)
        _run(db, tenant.id, JOB_WORKFLOW_TIMERS, minutes_ago=1)

        report = SystemHealthService(db).report(admin)

        dispatcher = next(
            j for j in report["jobs"] if j["job"] == JOB_NOTIFICATIONS
        )
        assert dispatcher["status"] == HEALTH_OK

    def test_the_hourly_job_is_judged_on_its_own_cadence(
        self, db, tenant, make_user
    ):
        """40 minutes is dead for the per-minute job and perfectly normal for
        the hourly one. One threshold for both would be wrong twice."""
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=0)
        _run(db, tenant.id, JOB_WORKFLOW_TIMERS, minutes_ago=40)

        report = SystemHealthService(db).report(admin)

        timers = next(
            j for j in report["jobs"] if j["job"] == JOB_WORKFLOW_TIMERS
        )
        assert timers["status"] == HEALTH_OK

    def test_a_failing_run_is_degraded_not_down(self, db, tenant, make_user):
        """Running-but-failing and not-running-at-all need different responses:
        one is a bug to read the logs for, the other is a scheduler to
        restart."""
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=0,
             status=STATUS_ERROR, error="SMTPConnectError: timed out")
        _run(db, tenant.id, JOB_WORKFLOW_TIMERS, minutes_ago=1)

        report = SystemHealthService(db).report(admin)

        dispatcher = next(
            j for j in report["jobs"] if j["job"] == JOB_NOTIFICATIONS
        )
        assert dispatcher["status"] == HEALTH_DEGRADED
        assert "SMTPConnectError" in dispatcher["last_error"]


class TestRecordingARun:
    def test_a_run_is_recorded(self, db, tenant, make_user):
        started = job_clock()

        record_job_run(db, tenant.id, JOB_NOTIFICATIONS, started, items_processed=3)

        row = db.query(JobRun).filter(JobRun.job_name == JOB_NOTIFICATIONS).one()
        assert row.status == STATUS_OK
        assert row.items_processed == 3
        assert row.finished_at is not None

    def test_a_failed_run_is_still_recorded(self, db, tenant, make_user):
        """The heartbeat has to survive the failure it is reporting. If it did
        not, a job that started failing would go silent — indistinguishable
        from one that stopped, and the two need opposite responses."""
        record_job_run(
            db, tenant.id, JOB_NOTIFICATIONS, job_clock(),
            error="RuntimeError: provider unreachable",
        )

        row = db.query(JobRun).filter(JobRun.job_name == JOB_NOTIFICATIONS).one()
        assert row.status == STATUS_ERROR
        assert "provider unreachable" in row.error

    def test_recording_does_not_discard_the_caller_s_other_work(
        self, db, tenant, make_user
    ):
        """A monitoring write must not roll its caller back.

        The recorder commits, and a failed run usually arrives with a poisoned
        transaction — so the tempting fix is to rollback() inside the recorder.
        That would silently throw away whatever else the session was holding
        the first time somebody called this from inside a request. Clearing a
        broken transaction is the caller's decision; the scheduled scripts do
        it themselves, where the session is theirs and its work is committed.
        """
        pending = _queued(db, tenant.id, make_user(UserRole.ADMIN)["id"])

        record_job_run(
            db, tenant.id, JOB_NOTIFICATIONS, job_clock(), error="boom",
        )

        assert db.query(NotificationOutbox).filter(
            NotificationOutbox.id == pending.id
        ).first() is not None

    def test_old_runs_are_pruned(self, db, tenant, make_user):
        """A per-minute job writes 1,440 rows a day per tenant. Without this it
        is a table somebody has to deal with later, and 'later' means when it
        is already slow."""
        _run(db, tenant.id, JOB_NOTIFICATIONS,
             minutes_ago=(RETENTION_DAYS + 1) * 24 * 60)
        recent = _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=1)
        db.commit()

        record_job_run(db, tenant.id, JOB_NOTIFICATIONS, job_clock())

        remaining = {r.id for r in db.query(JobRun).all()}
        assert recent.id in remaining
        assert len(remaining) == 2


class TestTheNotificationQueue:
    def test_disabled_delivery_is_configuration_not_a_fault(
        self, db, tenant, make_user
    ):
        """SMTP off is the documented default. A monitor that shows it red
        teaches people that red means nothing."""
        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        _queued(db, tenant.id, admin["id"], minutes_ago=120)

        report = SystemHealthService(db).report(admin)

        assert report["notifications"]["status"] == HEALTH_OK
        assert report["notifications"]["delivery_enabled"] is False
        assert "held rather than failed" in report["notifications"]["detail"]
        assert any("SMTP_ENABLED" in n for n in report["notes"])

    def test_given_up_messages_are_degraded_once_delivery_is_on(
        self, db, tenant, make_user, monkeypatch
    ):
        """These will never retry on their own, so somebody has to know."""
        from app.core.config import settings
        monkeypatch.setattr(settings, "SMTP_ENABLED", True, raising=False)

        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        _queued(db, tenant.id, admin["id"], status=STATUS_FAILED)

        report = SystemHealthService(db).report(admin)

        assert report["notifications"]["status"] == HEALTH_DEGRADED
        assert report["notifications"]["failed"] == 1

    def test_a_queue_not_draining_is_degraded(
        self, db, tenant, make_user, monkeypatch
    ):
        """The dispatcher is alive but making no progress — which the job
        heartbeat alone would report as perfectly healthy."""
        from app.core.config import settings
        monkeypatch.setattr(settings, "SMTP_ENABLED", True, raising=False)

        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        _queued(db, tenant.id, admin["id"],
                minutes_ago=STUCK_MESSAGE_MINUTES + 10)

        report = SystemHealthService(db).report(admin)

        assert report["notifications"]["status"] == HEALTH_DEGRADED
        assert report["notifications"]["stuck"] == 1
        assert "not\nmaking progress" in report["notifications"]["detail"].replace(
            " ", "\n", 1
        ) or "making progress" in report["notifications"]["detail"]


class TestTheAiLayer:
    def test_schema_rejections_alone_are_not_a_fault(self, db, tenant, make_user):
        """A refused malformed response is the guard doing its job. Flagging it
        red would punish the system for being careful."""
        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        db.add(AIActionLog(
            id=uuid.uuid4(), tenant_id=tenant.id, action="duplicate_detection",
            status="failed_schema",
        ))
        db.flush()

        report = SystemHealthService(db).report(admin)

        assert report["ai"]["status"] == HEALTH_OK
        assert report["ai"]["schema_rejections"] == 1

    def test_errors_are_degraded(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        db.add(AIActionLog(
            id=uuid.uuid4(), tenant_id=tenant.id, action="nl_query",
            status="error",
        ))
        db.flush()

        report = SystemHealthService(db).report(admin)

        assert report["ai"]["status"] == HEALTH_DEGRADED
        assert report["ai"]["errors"] == 1


class TestTheOverallReading:
    def test_the_worst_component_sets_the_headline(self, db, tenant, make_user):
        """Somebody glancing at the console reads one word. It has to be the
        worst one, not an average that hides an outage behind three greens."""
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=0)
        _run(db, tenant.id, JOB_WORKFLOW_TIMERS, minutes_ago=6000)

        report = SystemHealthService(db).report(admin)

        assert report["status"] == HEALTH_DOWN

    def test_degraded_does_not_mask_down(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _run(db, tenant.id, JOB_NOTIFICATIONS, minutes_ago=0,
             status=STATUS_ERROR, error="boom")
        # timers never ran at all

        report = SystemHealthService(db).report(admin)

        assert report["status"] == HEALTH_DOWN


class TestAccess:
    def test_a_clerk_cannot_read_system_health(self, db, tenant, make_user):
        """It names configuration and failure detail — the same audience as the
        audit trail, and not everybody."""
        clerk = make_user(UserRole.AP_CLERK)

        with pytest.raises(PermissionError):
            SystemHealthService(db).report(clerk)

    def test_the_endpoint_serves_an_administrator(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        _heartbeat_all(db, tenant.id)
        db.commit()
        as_user(admin)

        response = client.get("/api/v1/system/health")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == HEALTH_OK
        assert {j["job"] for j in body["jobs"]} == {
            JOB_NOTIFICATIONS, JOB_WORKFLOW_TIMERS
        }

    def test_the_endpoint_refuses_a_clerk(
        self, db, tenant, client, as_user, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        as_user(clerk)

        assert client.get("/api/v1/system/health").status_code == 403
