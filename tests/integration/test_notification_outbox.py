"""Notifications are queued, then delivered outside the request.

Email used to be sent synchronously inside the request that triggered it, with
the exception swallowed — so an approval waited on a mail server before the
user got a response, and when delivery failed nobody learned. The audit trail
recorded that an SLA breach had been escalated to the CFO while no message was
ever sent.

The reason this is a table and not a background thread is atomicity and
durability, and those are what the tests below are about: a rolled-back action
must queue nothing, a committed one must queue for certain, a failure must be
recorded and retried rather than lost, and a permanently bad address must stop
rather than consume the queue forever.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import InvoiceState, UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.notification_outbox import (
    NotificationOutbox, STATUS_PENDING, STATUS_SENT, STATUS_FAILED, MAX_ATTEMPTS,
)
from app.models.vendor import Vendor
from app.services.invoice_service import InvoiceService
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.notification_service import NotificationService
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _queued(db, **filters):
    query = db.query(NotificationOutbox)
    for field, value in filters.items():
        query = query.filter(getattr(NotificationOutbox, field) == value)
    return query.all()


def _message(db, tenant_id, **overrides):
    fields = dict(
        id=uuid.uuid4(), tenant_id=tenant_id, to_email="cfo@test.com",
        subject="SLA breached: invoice INV-1 awaiting action",
        body="It has been escalated to CFO.", category="sla_escalation",
        status=STATUS_PENDING, attempts=0, next_attempt_at=_now(),
    )
    fields.update(overrides)
    message = NotificationOutbox(**fields)
    db.add(message)
    db.flush()
    return message


class TestTheRequestDoesNotWaitOnMail:
    def test_sending_only_queues(self, db, tenant, make_user, monkeypatch):
        """The point of the change. Nothing touches SMTP during the action."""
        delivered = []
        monkeypatch.setattr(
            NotificationService, "_deliver",
            lambda self, *a, **k: delivered.append(a),
        )
        admin = make_user(UserRole.ADMIN)

        NotificationService(db)._send(
            tenant.id, ["cfo@test.com"], "Subject", "Body", category="test"
        )
        db.flush()

        assert not delivered, "the request talked to a mail server"
        assert len(_queued(db)) == 1

    def test_one_row_per_recipient(self, db, tenant):
        """So one bad address cannot take the others down with it, and each
        retries on its own schedule."""
        NotificationService(db)._send(
            tenant.id, ["a@test.com", "b@test.com"], "Subject", "Body"
        )
        db.flush()

        assert {m.to_email for m in _queued(db)} == {"a@test.com", "b@test.com"}

    def test_duplicate_recipients_are_collapsed(self, db, tenant):
        NotificationService(db)._send(
            tenant.id, ["a@test.com", "a@test.com"], "Subject", "Body"
        )
        db.flush()

        assert len(_queued(db)) == 1


class TestItSharesTheActionsTransaction:
    """The property a thread cannot give you."""

    def test_a_committed_action_queues_its_notification(
        self, db, tenant, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        make_user(UserRole.MANAGER, email="mgr@test.com")
        vendor = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="V",
            status=VendorStatus.ACTIVE,
        )
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, invoice_number="INV-Q",
            vendor_name="V", vendor_id=vendor.id, invoice_date=date(2026, 8, 1),
            total_amount=Decimal("100000"), current_state=InvoiceState.DRAFT,
            created_by=clerk["id"],
        )
        db.add(invoice)
        db.flush()

        service = InvoiceService(db)
        service.validate_invoice(invoice.id, clerk)
        service.submit_for_approval(invoice.id, clerk)

        assert "mgr@test.com" in {m.to_email for m in _queued(db)}

    def test_a_rolled_back_action_queues_nothing(self, db, tenant, make_user):
        """A thread started mid-action would have sent mail for work that never
        happened. The row goes back with everything else."""
        make_user(UserRole.ADMIN)

        NotificationService(db)._send(
            tenant.id, ["ghost@test.com"], "Never happened", "Body"
        )
        db.flush()
        assert len(_queued(db)) == 1

        db.rollback()

        assert _queued(db, to_email="ghost@test.com") == []


class TestDraining:
    """Delivery is exercised with SMTP switched on. `_deliver` is stubbed in
    each test, so nothing leaves the machine — the flag only gets the
    dispatcher past the "not configured" hold."""

    @pytest.fixture(autouse=True)
    def _smtp_on(self, monkeypatch):
        from app.core.config import settings
        monkeypatch.setattr(settings, "SMTP_ENABLED", True)

    def test_a_successful_send_is_marked_and_not_retried(
        self, db, tenant, make_user, monkeypatch
    ):
        delivered = []
        monkeypatch.setattr(
            NotificationService, "_deliver",
            lambda self, to, subject, body: delivered.append(to),
        )
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id)

        result = NotificationDispatcher(db).dispatch(admin)

        assert result["attempted"] == 1 and result["sent"] == 1
        assert delivered == ["cfo@test.com"]
        message = _queued(db)[0]
        assert message.status == STATUS_SENT
        assert message.sent_at is not None

        # A second run does not send it again.
        assert NotificationDispatcher(db).dispatch(admin)["attempted"] == 0
        assert delivered == ["cfo@test.com"]

    def test_a_failure_is_recorded_and_rescheduled(
        self, db, tenant, make_user, monkeypatch
    ):
        """The old code logged this and moved on, so a notification that never
        arrived left nothing anybody would find."""
        def boom(self, *a, **k):
            raise ConnectionRefusedError("mail server down")
        monkeypatch.setattr(NotificationService, "_deliver", boom)
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id)

        result = NotificationDispatcher(db).dispatch(admin)

        assert result["retrying"] == 1
        message = _queued(db)[0]
        assert message.status == STATUS_PENDING       # still queued
        assert message.attempts == 1
        assert "mail server down" in message.last_error
        assert message.next_attempt_at > _now()       # backed off

    def test_a_message_backing_off_is_not_picked_up_early(
        self, db, tenant, make_user, monkeypatch
    ):
        delivered = []
        monkeypatch.setattr(
            NotificationService, "_deliver",
            lambda self, to, subject, body: delivered.append(to),
        )
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id, next_attempt_at=_now() + timedelta(minutes=30))

        assert NotificationDispatcher(db).dispatch(admin)["attempted"] == 0
        assert delivered == []

    def test_it_gives_up_after_the_attempt_limit(
        self, db, tenant, make_user, monkeypatch
    ):
        """A permanently bad address must not consume the queue forever."""
        def boom(self, *a, **k):
            raise ConnectionRefusedError("nope")
        monkeypatch.setattr(NotificationService, "_deliver", boom)
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id, attempts=MAX_ATTEMPTS - 1)

        result = NotificationDispatcher(db).dispatch(admin)

        assert result["failed"] == 1
        message = _queued(db)[0]
        assert message.status == STATUS_FAILED
        assert message.next_attempt_at is None

    def test_a_failed_message_can_be_requeued_once_the_cause_is_fixed(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id, status=STATUS_FAILED,
                 attempts=MAX_ATTEMPTS, next_attempt_at=None)

        requeued = NotificationDispatcher(db).retry_failed(admin)

        assert requeued == 1
        message = _queued(db)[0]
        assert message.status == STATUS_PENDING
        assert message.attempts == 0

    def test_one_bad_address_does_not_stop_the_others(
        self, db, tenant, make_user, monkeypatch
    ):
        def selective(self, to, subject, body):
            if to == "bad@test.com":
                raise ConnectionRefusedError("no such mailbox")
        monkeypatch.setattr(NotificationService, "_deliver", selective)
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id, to_email="bad@test.com")
        _message(db, tenant.id, to_email="good@test.com")

        result = NotificationDispatcher(db).dispatch(admin)

        assert result["sent"] == 1 and result["retrying"] == 1
        by_email = {m.to_email: m.status for m in _queued(db)}
        assert by_email["good@test.com"] == STATUS_SENT
        assert by_email["bad@test.com"] == STATUS_PENDING


class TestWhoMayDrainIt:
    def test_an_ordinary_role_cannot(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            NotificationDispatcher(db).dispatch(clerk)

    def test_nor_read_the_queue(self, db, tenant, make_user):
        """Bodies quote the record and its amount, so the queue is as private
        as the records it describes."""
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            NotificationDispatcher(db).list_messages(clerk)


class TestSmtpStaysOptIn:
    """Not configured is not the same as failed to deliver."""

    def test_messages_are_held_untouched_rather_than_attempted(
        self, db, tenant, make_user
    ):
        """Found by running the scheduler against this deployment, which has no
        SMTP: each run burned an attempt, so five runs would have marked the
        whole queue permanently failed. Turning SMTP on afterwards would then
        find a backlog it refuses to send — the opposite of why the rows are
        kept."""
        from app.core.config import settings

        assert settings.SMTP_ENABLED is False
        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id)

        result = NotificationDispatcher(db).dispatch(admin)

        assert result["attempted"] == 0
        assert result["held"] == 1
        message = _queued(db)[0]
        assert message.status == STATUS_PENDING
        assert message.attempts == 0, "a disabled mailer consumed an attempt"

    def test_the_backlog_survives_repeated_runs(self, db, tenant, make_user):
        """The claim that turning SMTP on later sends the backlog, tested."""
        from app.models.notification_outbox import MAX_ATTEMPTS

        admin = make_user(UserRole.ADMIN)
        _message(db, tenant.id)

        for _ in range(MAX_ATTEMPTS + 2):
            NotificationDispatcher(db).dispatch(admin)

        message = _queued(db)[0]
        assert message.status == STATUS_PENDING
        assert message.attempts == 0
