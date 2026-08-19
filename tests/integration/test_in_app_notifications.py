"""In-app notifications, and the reminders that precede an escalation.

Email is where a notification goes to be missed. The people who approve things
are not sitting in a shared AP mailbox all day, so "the approver was told" meant
one channel, unread, with nothing visible on their next login.

Both channels are one table on purpose: "was this person told, and how" should
be a single query. The tests below cover the two halves that are easy to get
wrong — that an in-app row is never delivered anywhere (it *is* the
notification), and that the feed can only ever return your own.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import RequisitionState, UserRole, VendorStatus
from app.models.audit_log import AuditLog
from app.models.notification_outbox import (
    NotificationOutbox, CHANNEL_EMAIL, CHANNEL_IN_APP,
    STATUS_PENDING, STATUS_SENT,
)
from app.models.requisition import PurchaseRequisition, PurchaseRequisitionLine
from app.models.workflow_state import WorkflowState
from app.services.config_provisioning import ConfigProvisioningService
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.notification_feed import NotificationFeedService
from app.services.notification_service import NotificationService
from app.services.requisition_service import RequisitionService
from app.services.sla_service import SlaService
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


def _rows(db, channel=None):
    query = db.query(NotificationOutbox)
    if channel:
        query = query.filter(NotificationOutbox.channel == channel)
    return query.all()


def _requisition(db, tenant_id, created_by, number="REQ-BELL"):
    requisition = PurchaseRequisition(
        id=uuid.uuid4(), tenant_id=tenant_id, requisition_number=number,
        title="Laptops", justification="Four engineers start on the 1st.",
        requested_date=date(2026, 9, 1), estimated_amount=Decimal("1000"),
        current_state=RequisitionState.DRAFT, created_by=created_by,
    )
    db.add(requisition)
    db.flush()
    db.add(PurchaseRequisitionLine(
        id=uuid.uuid4(), tenant_id=tenant_id, requisition_id=requisition.id,
        line_number=1, description="Laptop", quantity=Decimal("4"),
        estimated_unit_price=Decimal("1000"), estimated_amount=Decimal("1000"),
    ))
    db.flush()
    return requisition


class TestBothChannelsAreProduced:
    def test_a_notification_lands_as_email_and_in_app(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER, email="mgr@test.com")

        NotificationService(db)._send(
            tenant.id, ["mgr@test.com"], "Subject", "Body",
            category="test", link="/ai-tools/inbox",
        )
        db.flush()

        assert len(_rows(db, CHANNEL_EMAIL)) == 1
        in_app = _rows(db, CHANNEL_IN_APP)
        assert len(in_app) == 1
        assert str(in_app[0].user_id) == str(manager["id"])
        assert in_app[0].link == "/ai-tools/inbox"

    def test_the_in_app_row_needs_no_delivery(self, db, tenant, make_user):
        """It is not sent anywhere — it is the row the reader opens. Created
        already sent, so the drain never picks it up and a mail outage cannot
        stop it appearing."""
        make_user(UserRole.MANAGER, email="mgr@test.com")

        NotificationService(db)._send(tenant.id, ["mgr@test.com"], "Subject", "Body")
        db.flush()

        in_app = _rows(db, CHANNEL_IN_APP)[0]
        assert in_app.status == STATUS_SENT
        assert in_app.sent_at is not None
        assert in_app.to_email is None

    def test_the_drain_ignores_in_app_rows(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN, email="admin@test.com")

        NotificationService(db)._send(tenant.id, ["admin@test.com"], "Subject", "Body")
        db.flush()

        # SMTP is off, so the email is held; the in-app row is not even counted.
        result = NotificationDispatcher(db).dispatch(admin)
        assert result["attempted"] == 0
        assert result["held"] == 1  # the email only

    def test_an_address_with_no_user_still_gets_the_email(self, db, tenant):
        """Recipients are resolved from addresses, and an address need not
        belong to a current user. The email is the record; the in-app row is a
        convenience for people who are still here."""
        NotificationService(db)._send(
            tenant.id, ["someone@external.example"], "Subject", "Body"
        )
        db.flush()

        assert len(_rows(db, CHANNEL_EMAIL)) == 1
        assert _rows(db, CHANNEL_IN_APP) == []

    def test_a_real_workflow_action_produces_both(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
        requisition = _requisition(db, tenant.id, clerk["id"])

        RequisitionService(db).submit_requisition(requisition.id, clerk)

        assert _rows(db, CHANNEL_EMAIL)
        recipients = {str(r.user_id) for r in _rows(db, CHANNEL_IN_APP)}
        assert str(manager["id"]) in recipients


class TestTheFeedIsYoursOnly:
    def _notify(self, db, tenant, email):
        NotificationService(db)._send(tenant.id, [email], "Subject", "Body")
        db.flush()

    def test_you_see_your_own(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER, email="mgr@test.com")
        self._notify(db, tenant, "mgr@test.com")

        feed = NotificationFeedService(db)
        assert feed.unread_count(manager) == 1
        assert len(feed.list(manager)) == 1

    def test_you_do_not_see_anybody_elses(self, db, tenant, make_user):
        """There is no role that grants reading another person's
        notifications, so the filter is the authorisation."""
        make_user(UserRole.MANAGER, email="mgr@test.com")
        cfo = make_user(UserRole.CFO, email="cfo@test.com")
        self._notify(db, tenant, "mgr@test.com")

        feed = NotificationFeedService(db)
        assert feed.unread_count(cfo) == 0
        assert feed.list(cfo) == []

    def test_you_cannot_mark_somebody_elses_read(self, db, tenant, make_user):
        make_user(UserRole.MANAGER, email="mgr@test.com")
        cfo = make_user(UserRole.CFO, email="cfo@test.com")
        self._notify(db, tenant, "mgr@test.com")
        theirs = _rows(db, CHANNEL_IN_APP)[0]

        with pytest.raises(ValueError, match="not found"):
            NotificationFeedService(db).mark_read(theirs.id, cfo)

    def test_reading_clears_it_from_the_bell(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER, email="mgr@test.com")
        self._notify(db, tenant, "mgr@test.com")
        feed = NotificationFeedService(db)
        mine = feed.list(manager)[0]

        feed.mark_read(mine.id, manager)

        assert feed.unread_count(manager) == 0
        assert len(feed.list(manager)) == 1          # still there, just read
        assert feed.list(manager, unread_only=True) == []

    def test_mark_all_read_clears_only_yours(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER, email="mgr@test.com")
        cfo = make_user(UserRole.CFO, email="cfo@test.com")
        self._notify(db, tenant, "mgr@test.com")
        self._notify(db, tenant, "cfo@test.com")

        marked = NotificationFeedService(db).mark_all_read(manager)

        assert marked == 1
        assert NotificationFeedService(db).unread_count(cfo) == 1

    def test_the_api_returns_the_feed(self, db, tenant, client, as_user, make_user):
        manager = make_user(UserRole.MANAGER, email="mgr@test.com")
        self._notify(db, tenant, "mgr@test.com")
        as_user(manager)

        response = client.get("/api/v1/notifications/mine")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unread"] == 1
        assert body["items"][0]["subject"] == "Subject"


class TestReminders:
    """A reminder is not an escalation. It goes to the people who could have
    acted all along, before the deadline, so the alarm never has to sound."""

    @pytest.fixture
    def setup(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        ConfigProvisioningService(db).initialize_defaults(admin)
        return {
            "tenant": tenant, "admin": admin,
            "clerk": make_user(UserRole.AP_CLERK),
            "manager": make_user(UserRole.MANAGER, email="mgr@test.com"),
        }

    def _waiting_requisition(self, db, setup, hours_ago):
        requisition = _requisition(
            db, setup["tenant"].id, setup["clerk"]["id"], f"REQ-{hours_ago}H"
        )
        requisition.current_state = RequisitionState.PENDING_APPROVAL
        requisition.state_entered_at = utc_now() - timedelta(hours=hours_ago)
        db.flush()
        return requisition

    def test_something_waiting_a_long_time_produces_a_nudge(self, db, setup):
        # The requisition SLA is 24h, so 20h is waiting but not yet late.
        self._waiting_requisition(db, setup, hours_ago=20)

        result = SlaService(db).run_reminders(setup["admin"])

        assert result["reminded_count"] == 1
        assert "mgr@test.com" in {r.to_email for r in _rows(db, CHANNEL_EMAIL)}

    def test_something_fresh_is_left_alone(self, db, setup):
        self._waiting_requisition(db, setup, hours_ago=2)

        assert SlaService(db).run_reminders(setup["admin"])["reminded_count"] == 0

    def test_something_already_late_is_left_to_escalation(self, db, setup):
        """Two messages about the same lateness is how both get ignored."""
        self._waiting_requisition(db, setup, hours_ago=48)   # past the 24h SLA

        assert SlaService(db).run_reminders(setup["admin"])["reminded_count"] == 0

    def test_it_nudges_once_per_interval(self, db, setup):
        requisition = self._waiting_requisition(db, setup, hours_ago=20)

        assert SlaService(db).run_reminders(setup["admin"])["reminded_count"] == 1
        assert SlaService(db).run_reminders(setup["admin"])["reminded_count"] == 0

        events = db.query(AuditLog).filter(
            AuditLog.object_id == requisition.id,
            AuditLog.action == "reminder_sent",
        ).all()
        assert len(events) == 1

    def test_the_nudge_is_recorded_against_the_record(self, db, setup):
        """So "we did chase this" is answerable later, not a claim."""
        requisition = self._waiting_requisition(db, setup, hours_ago=20)

        SlaService(db).run_reminders(setup["admin"])

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == requisition.id,
            AuditLog.action == "reminder_sent",
        ).first()
        assert entry is not None
        assert "Still waiting" in entry.comment

    def test_an_ordinary_role_cannot_run_them(self, db, setup, make_user):
        with pytest.raises(PermissionError):
            SlaService(db).run_reminders(make_user(UserRole.AP_CLERK))
