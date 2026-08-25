"""A heartbeat for the scheduled jobs.

The admin console needs to answer "is anything silently not running", and that
question cannot be answered from the work queues alone. A notification outbox
with nothing pending looks identical whether the dispatcher ran a second ago or
died last Tuesday — the difference only becomes visible once somebody is
waiting on a message that will never arrive.

So each scheduled run records that it happened. That turns the most dangerous
class of failure here, a job that stopped, from an invisible one into a stale
timestamp somebody can see.

One row per tenant per run, because the jobs iterate tenants and bind each in
turn; a tenant-scoped row keeps the isolation boundary intact and lets an
administrator see the health of their own tenant rather than the estate.
"""
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import BaseModel

#: Job identifiers. Strings rather than an enum: a new scheduled job should not
#: need a migration to become visible to the monitor.
JOB_NOTIFICATIONS = "dispatch_notifications"
JOB_WORKFLOW_TIMERS = "run_workflow_timers"
JOB_INTEGRATION_POSTS = "dispatch_integration_posts"

#: How often each job is expected to run, in minutes. The monitor compares the
#: last run against this to decide whether a job is late, so the cadence in the
#: scripts' docstrings lives here as a number rather than as prose. Generous
#: multiples are applied at the point of comparison — a single missed minute is
#: not an incident.
EXPECTED_INTERVAL_MINUTES = {
    JOB_NOTIFICATIONS: 1,
    JOB_WORKFLOW_TIMERS: 60,
    #: Not an SLA escalation racing a clock like the two above, but "notify
    #: the external ledger" implies soon rather than eventually — 5 minutes
    #: is short enough that a stalled queue is caught the same working day.
    JOB_INTEGRATION_POSTS: 5,
}

STATUS_OK = "ok"
STATUS_ERROR = "error"

#: Kept for a week. Long enough to see a pattern in the failures, short enough
#: that a per-minute job does not accumulate forever — 1,440 rows a day per
#: tenant is not a table anybody should have to think about later.
RETENTION_DAYS = 7


class JobRun(BaseModel):
    __tablename__ = "job_runs"

    OBJECT_TYPE = "job_run"
    REFERENCE_FIELD = "job_name"

    #: Never filtered by org scope: a scheduled job belongs to the tenant, not
    #: to a business unit, and hiding it from a scoped administrator would make
    #: the monitor lie about the system being healthy.
    ORG_SCOPE_EXEMPT = True

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    job_name = Column(String(50), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=STATUS_OK)

    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    #: What the run actually did. A job that runs every minute and processes
    #: nothing for a day is a different situation from one that is failing, and
    #: both differ from one that is quietly working.
    items_processed = Column(Integer, nullable=False, default=0)

    #: Truncated at the recorder. A stack trace belongs in the logs; this is
    #: what an administrator reads on a dashboard.
    error = Column(String(500), nullable=True)
