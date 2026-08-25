"""The Integration Hub: a tenant's own accounting system, told what happened.

Build Book: "Integration connectors implemented with retries, idempotency,
dead-letter queues, and reconciliation records."

Three properties carry the weight here, and each is a different kind of
damage when it breaks:

  * **Credentials.** This feature is the only place Sarmaya holds a
    credential to somebody else's financial system. A token readable from
    the database, visible to a second tenant, or copied into an audit entry
    is not a Sarmaya bug — it is a compromise of the client's books.
  * **Money is described once.** A journal entry posted twice overstates a
    client's expenses in their own ledger, and nobody looking at Sarmaya
    would see anything wrong. The queue's unique constraint, the connector's
    DocNumber check, and the enqueue path's savepoint are three separate
    layers of that one guarantee, and the last of them is tested here
    because getting it wrong silently discards the *payment*, not the post.
  * **Failure stays visible.** A dead connection must stop and say so rather
    than retry forever or fail quietly — the whole reason the outbox is a
    table and not a background thread.

The connector is faked throughout. Nothing in this file reaches Intuit; what
is under test is Sarmaya's half of the contract, which is the half that can
lose a client's money.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.enums import InvoiceState, UserRole, VendorStatus
from app.core.mfa import decrypt_secret, encrypt_secret
from app.models.audit_log import AuditLog
from app.models.integration import (
    IntegrationAccountSnapshot, IntegrationConnection, IntegrationJournalPost,
    IntegrationPartySnapshot, IntegrationVendorMapping, MAX_ATTEMPTS,
    POST_STATUS_FAILED, POST_STATUS_PENDING, POST_STATUS_POSTED,
    SOURCE_EXPENSE_REIMBURSEMENT, SOURCE_PAYMENT, STATUS_CONNECTED,
    STATUS_EXPIRED, STATUS_NOT_CONNECTED,
)
from app.models.invoice import Invoice
from app.models.tenant import Tenant
from app.models.vendor import Vendor
from app.schemas.payment import PaymentCreate
from app.services import integration_service as integration_module
from app.services.config_provisioning import ConfigProvisioningService
from app.services.expense_service import ExpenseService
from app.services.finance_connectors.base import (
    ChartOfAccountsEntry, ConnectorAuthError, ConnectorUnavailable,
    ExternalParty, FinanceConnector, JournalEntryResult, OAuthTokens,
)
from app.services.integration_posting_service import JournalPostingService
from app.services.integration_service import IntegrationService
from app.services.payment_service import PaymentService
from app.utils.datetime_helpers import make_naive, to_utc, utc_now

pytestmark = pytest.mark.integration

ACCESS_TOKEN = "qbo-access-tok-Nf83jdKQ"
REFRESH_TOKEN = "qbo-refresh-tok-Zx01pLmB"
LIABILITY_ACCOUNT = "33"
BANK_ACCOUNT = "35"


def _now():
    return make_naive(to_utc(utc_now()))


# --- the provider, faked ------------------------------------------------------

class FakeQuickBooks(FinanceConnector):
    """Stands in for Intuit.

    State is held on the class, not the instance, because `connector_for`
    constructs a fresh connector for every call by design (see
    FinanceConnector's docstring on statelessness) — an instance attribute
    would be thrown away between the call that set it and the assertion.
    """

    provider_name = "quickbooks"

    posted: List = []
    revoked: List[str] = []
    refresh_calls: int = 0
    #: Raised by post_journal_entry when set. The two exception types are the
    #: whole behavioural fork in the drain: transient backs off, auth stops.
    post_raises: Optional[Exception] = None
    refresh_raises: Optional[Exception] = None

    @classmethod
    def reset(cls):
        cls.posted = []
        cls.revoked = []
        cls.refresh_calls = 0
        cls.post_raises = None
        cls.refresh_raises = None

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://fake-intuit.test/connect?state={state}"

    def exchange_code(self, code, redirect_uri, company_id) -> OAuthTokens:
        now = datetime.now(timezone.utc)
        return OAuthTokens(
            access_token=ACCESS_TOKEN, refresh_token=REFRESH_TOKEN,
            expires_at=now + timedelta(hours=1),
            refresh_token_expires_at=now + timedelta(days=100),
        )

    def refresh(self, refresh_token: str) -> OAuthTokens:
        type(self).refresh_calls += 1
        if type(self).refresh_raises:
            raise type(self).refresh_raises
        now = datetime.now(timezone.utc)
        return OAuthTokens(
            access_token=ACCESS_TOKEN + "-rotated",
            refresh_token=REFRESH_TOKEN + "-rotated",
            expires_at=now + timedelta(hours=1),
            refresh_token_expires_at=now + timedelta(days=100),
        )

    def revoke(self, refresh_token: str) -> None:
        type(self).revoked.append(refresh_token)

    def list_accounts(self, tokens, company_id) -> List[ChartOfAccountsEntry]:
        return [
            ChartOfAccountsEntry(
                external_id=LIABILITY_ACCOUNT, name="Accounts Payable",
                account_type="Accounts Payable",
            ),
            ChartOfAccountsEntry(
                external_id=BANK_ACCOUNT, name="Business Current",
                account_type="Bank",
            ),
        ]

    def list_parties(self, tokens, company_id) -> List[ExternalParty]:
        return [
            ExternalParty(
                external_id="56", party_type="vendor", display_name="Payable Vendor",
            ),
            ExternalParty(
                external_id="77", party_type="customer", display_name="A Customer",
            ),
        ]

    def post_journal_entry(self, tokens, company_id, entry) -> JournalEntryResult:
        if type(self).post_raises:
            raise type(self).post_raises
        type(self).posted.append(entry)
        return JournalEntryResult(
            external_transaction_id=f"JE-{len(type(self).posted)}",
            external_transaction_type="JournalEntry",
        )

    def check_health(self, tokens, company_id) -> bool:
        return True


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    """Every test in this module talks to the fake, not Intuit.

    Patched into the registry rather than onto the service, so the resolution
    path under test is the real one — `connection.provider` -> `_CONNECTORS`
    -> a connector — including inside `JournalPostingService`, which imports
    `connector_for` from this same module.
    """
    FakeQuickBooks.reset()
    monkeypatch.setitem(
        integration_module._CONNECTORS, "quickbooks", FakeQuickBooks
    )
    yield FakeQuickBooks
    FakeQuickBooks.reset()


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def setup(db, tenant, make_user):
    ConfigProvisioningService(db).initialize_defaults(make_user(UserRole.ADMIN))
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Payable Vendor",
        status=VendorStatus.ACTIVE, bank_account_name="Payable Vendor Ltd",
        bank_account_number="0123456789", bank_name="Demo Bank",
        iban="PK36SCBL0000001123456702", swift_code="SCBLPKKX",
    )
    db.add(vendor)
    db.flush()
    return {
        "tenant": tenant,
        "vendor": vendor,
        "clerk": make_user(UserRole.AP_CLERK),
        "cfo": make_user(UserRole.CFO),
        "admin": make_user(UserRole.ADMIN),
        "auditor": make_user(UserRole.AUDITOR),
    }


def _connection(db, tenant, *, status=STATUS_CONNECTED, with_accounts=True,
                expires_in_minutes=60, **overrides):
    """A connection in whatever state the test needs, without an OAuth dance."""
    fields = dict(
        id=uuid.uuid4(), tenant_id=tenant.id, provider="quickbooks",
        status=status,
        external_company_id="4620816365", external_company_name="Client Books",
        access_token_encrypted=encrypt_secret(ACCESS_TOKEN),
        refresh_token_encrypted=encrypt_secret(REFRESH_TOKEN),
        token_expires_at=_now() + timedelta(minutes=expires_in_minutes),
        refresh_token_expires_at=_now() + timedelta(days=100),
        connected_at=_now(),
    )
    if with_accounts:
        fields["default_liability_account_external_id"] = LIABILITY_ACCOUNT
        fields["default_bank_account_external_id"] = BANK_ACCOUNT
    fields.update(overrides)
    connection = IntegrationConnection(**fields)
    db.add(connection)
    db.flush()
    return connection


def _snapshots(db, tenant, connection):
    """What a reference-data pull would have left behind."""
    for entry in FakeQuickBooks().list_accounts(None, ""):
        db.add(IntegrationAccountSnapshot(
            id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id,
            external_account_id=entry.external_id, name=entry.name,
            account_type=entry.account_type, is_active=True, fetched_at=_now(),
        ))
    for party in FakeQuickBooks().list_parties(None, ""):
        db.add(IntegrationPartySnapshot(
            id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id,
            external_party_id=party.external_id, party_type=party.party_type,
            display_name=party.display_name, is_active=True, fetched_at=_now(),
        ))
    db.flush()


def _approved_invoice(db, setup, amount="1000"):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=setup["tenant"].id,
        vendor_id=setup["vendor"].id, vendor_name=setup["vendor"].legal_name,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        invoice_date=date(2026, 8, 1), total_amount=Decimal(amount),
        current_state=InvoiceState.APPROVED, created_by=setup["clerk"]["id"],
    )
    db.add(invoice)
    db.flush()
    return invoice


def _released_payment(db, setup, amount="1000"):
    """A payment run taken all the way through release — the real call site."""
    invoice = _approved_invoice(db, setup, amount)
    service = PaymentService(db)
    payment = service.prepare_payment(
        [invoice.id], PaymentCreate(invoice_ids=[invoice.id]), setup["clerk"],
    )
    service.submit_for_release(payment.id, setup["clerk"])
    return service.release_payment(payment.id, setup["cfo"])


def _paid_claim(db, tenant, make_user, amount="500"):
    """An expense claim taken through to paid — the other real call site."""
    from app.models.employee import Employee

    manager = make_user(UserRole.MANAGER)
    cfo = make_user(UserRole.CFO, email=f"cfo-{uuid.uuid4().hex[:6]}@test.com")
    employee = Employee(
        id=uuid.uuid4(), tenant_id=tenant.id,
        employee_number=f"E-{uuid.uuid4().hex[:4]}", full_name="Sam Staff",
        job_title="Analyst", start_date=date(2026, 9, 1),
        user_id=manager["id"], status="active", correlation_id=uuid.uuid4(),
    )
    db.add(employee)
    db.flush()

    service = ExpenseService(db)
    claim = service.create(
        manager, employee_id=employee.id, category="meals",
        total_amount=Decimal(amount), incurred_date=date(2026, 8, 1),
    )
    service.submit(claim.id, manager)
    service.approve(claim.id, cfo)
    return service.mark_paid(claim.id, cfo)


def _posts(db, **filters):
    query = db.query(IntegrationJournalPost)
    for field, value in filters.items():
        query = query.filter(getattr(IntegrationJournalPost, field) == value)
    return query.all()


def _queued_post(db, tenant, connection, **overrides):
    """A queue row placed directly, for the drain tests that do not care how
    it got there."""
    fields = dict(
        id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id,
        source_type=SOURCE_PAYMENT, source_id=uuid.uuid4(),
        payload={
            "reference_number": str(uuid.uuid4()),
            "entry_date": "2026-08-01",
            "memo": "Sarmaya payment PAY-1",
            "lines": [
                {
                    "account_external_id": LIABILITY_ACCOUNT, "amount": "1000.00",
                    "direction": "debit", "party_external_id": None,
                    "description": "PAY-1: Payable Vendor",
                },
                {
                    "account_external_id": BANK_ACCOUNT, "amount": "1000.00",
                    "direction": "credit", "party_external_id": None,
                    "description": "Payment run PAY-1",
                },
            ],
        },
        status=POST_STATUS_PENDING, attempts=0, next_attempt_at=_now(),
    )
    fields.update(overrides)
    post = IntegrationJournalPost(**fields)
    db.add(post)
    db.flush()
    return post


# --- credentials --------------------------------------------------------------

class TestTokensAreNotStoredInTheClear:
    """The one credential Sarmaya holds to somebody else's financial system.

    Every test here connects through `IntegrationService` rather than the
    `_connection` helper. The helper encrypts on its own, so a test built on
    it would pass just as happily against a service that wrote the token
    straight into the column — which is the failure worth catching.
    """

    def _connect(self, db, setup) -> IntegrationConnection:
        service = IntegrationService(db)
        service.begin_connect("quickbooks", setup["admin"], "https://app.test/cb")
        state = db.query(IntegrationConnection).first().oauth_state
        return service.complete_connect(
            "quickbooks", state=state, code="code", company_id="4620816365",
            redirect_uri="https://app.test/cb",
        )

    def test_the_stored_column_is_not_the_token(self, db, setup):
        connection = self._connect(db, setup)

        stored = connection.access_token_encrypted
        assert stored != ACCESS_TOKEN
        assert ACCESS_TOKEN not in stored
        # Not merely reordered or encoded: no run of the token survives.
        assert "qbo-access" not in stored
        assert connection.refresh_token_encrypted != REFRESH_TOKEN
        assert REFRESH_TOKEN not in connection.refresh_token_encrypted

    def test_it_is_not_in_the_database_either(self, db, setup):
        """Read back through SQL rather than the ORM, so this is about what is
        on disk and not what the object happens to hold in memory."""
        connection = self._connect(db, setup)

        row = db.execute(
            text("SELECT access_token_encrypted, refresh_token_encrypted "
                 "FROM integration_connections WHERE id = :id"),
            {"id": str(connection.id)},
        ).fetchone()

        assert ACCESS_TOKEN not in row[0]
        assert REFRESH_TOKEN not in row[1]

    def test_it_round_trips(self, db, setup):
        """Encrypted is only useful if it is also readable — the failure this
        rules out is a connection that stores fine and can never be used."""
        connection = self._connect(db, setup)

        assert decrypt_secret(connection.access_token_encrypted) == ACCESS_TOKEN
        assert decrypt_secret(connection.refresh_token_encrypted) == REFRESH_TOKEN

    def test_the_audit_trail_never_carries_a_token(self, db, tenant, setup):
        """An audit entry is the one record deliberately readable by people who
        cannot see the connection itself. A token in `after_value` would hand
        the client's accounting credentials to every auditor."""
        service = IntegrationService(db)
        service.begin_connect("quickbooks", setup["admin"], "https://app.test/cb")
        state = db.query(IntegrationConnection).first().oauth_state

        service.complete_connect(
            "quickbooks", state=state, code="auth-code-123",
            company_id="4620816365", redirect_uri="https://app.test/cb",
        )

        entries = db.query(AuditLog).filter(
            AuditLog.tenant_id == tenant.id
        ).all()
        assert entries, "connecting recorded nothing at all"
        blob = json.dumps(
            [
                {
                    "action": e.action,
                    "before": e.before_value,
                    "after": e.after_value,
                    "comment": e.comment,
                }
                for e in entries
            ],
            default=str,
        )
        for secret in (ACCESS_TOKEN, REFRESH_TOKEN, "auth-code-123"):
            assert secret not in blob, f"{secret!r} leaked into the audit trail"


# --- the OAuth handshake ------------------------------------------------------

class TestTheHandshakeCannotBeForged:
    """The callback route carries no bearer token — this `state` value is the
    only thing standing between a stranger's browser and a tenant's
    connection row. See integration_service.py's module docstring."""

    def _begin(self, db, setup) -> str:
        IntegrationService(db).begin_connect(
            "quickbooks", setup["admin"], "https://app.test/cb"
        )
        return db.query(IntegrationConnection).first().oauth_state

    def test_a_valid_state_connects(self, db, tenant, setup):
        state = self._begin(db, setup)

        connection = IntegrationService(db).complete_connect(
            "quickbooks", state=state, code="code", company_id="4620816365",
            redirect_uri="https://app.test/cb",
        )

        assert connection.status == STATUS_CONNECTED
        assert connection.connected_at is not None
        assert connection.connected_by is not None, "nobody is recorded as connecting it"
        # Cleared, so the same value cannot be replayed.
        assert connection.oauth_state is None

    def test_the_reference_data_is_pulled_straight_away(self, db, tenant, setup):
        """Otherwise the admin lands back on a page with an empty account list
        and no reason to think clicking refresh is their next move."""
        state = self._begin(db, setup)

        IntegrationService(db).complete_connect(
            "quickbooks", state=state, code="code", company_id="4620816365",
            redirect_uri="https://app.test/cb",
        )

        assert db.query(IntegrationAccountSnapshot).count() == 2
        assert db.query(IntegrationPartySnapshot).count() == 2

    def test_a_forged_state_is_refused(self, db, tenant, setup):
        self._begin(db, setup)
        forged = f"{tenant.id}:{uuid.uuid4().hex}"

        with pytest.raises(ValueError, match="expired or was already used"):
            IntegrationService(db).complete_connect(
                "quickbooks", state=forged, code="code", company_id="1",
                redirect_uri="https://app.test/cb",
            )

    def test_a_state_naming_a_tenant_that_does_not_exist_is_refused(self, db, setup):
        """Refused before any tenant is bound, so a made-up id cannot be used
        to probe which tenants exist."""
        self._begin(db, setup)

        with pytest.raises(ValueError, match="not valid"):
            IntegrationService(db).complete_connect(
                "quickbooks", state=f"{uuid.uuid4()}:tok", code="code",
                company_id="1", redirect_uri="https://app.test/cb",
            )

    def test_a_malformed_state_is_refused_rather_than_crashing(self, db, setup):
        """The callback is reachable by anyone with the URL; a 500 here would
        be a blank browser tab and a stack trace in the logs."""
        self._begin(db, setup)

        with pytest.raises(ValueError, match="not valid"):
            IntegrationService(db).complete_connect(
                "quickbooks", state="not-a-state", code="code", company_id="1",
                redirect_uri="https://app.test/cb",
            )

    def test_a_state_is_single_use(self, db, tenant, setup):
        state = self._begin(db, setup)
        service = IntegrationService(db)
        service.complete_connect(
            "quickbooks", state=state, code="code", company_id="4620816365",
            redirect_uri="https://app.test/cb",
        )

        with pytest.raises(ValueError, match="expired or was already used"):
            service.complete_connect(
                "quickbooks", state=state, code="code", company_id="4620816365",
                redirect_uri="https://app.test/cb",
            )

    def test_an_expired_state_is_refused(self, db, tenant, setup):
        state = self._begin(db, setup)
        connection = db.query(IntegrationConnection).first()
        connection.oauth_state_expires_at = _now() - timedelta(minutes=1)
        db.flush()

        with pytest.raises(ValueError, match="expired or was already used"):
            IntegrationService(db).complete_connect(
                "quickbooks", state=state, code="code", company_id="1",
                redirect_uri="https://app.test/cb",
            )


class TestDisconnecting:
    def test_it_wipes_both_tokens(self, db, tenant, setup):
        connection = _connection(db, tenant)

        IntegrationService(db).disconnect("quickbooks", setup["admin"])

        db.refresh(connection)
        assert connection.access_token_encrypted is None
        assert connection.refresh_token_encrypted is None
        assert connection.status == STATUS_NOT_CONNECTED
        assert connection.disconnected_by is not None

    def test_it_tells_the_provider_too(self, db, tenant, setup):
        """Wiping our copy leaves a token that still works in Intuit's hands.
        Best-effort, but attempted."""
        _connection(db, tenant)

        IntegrationService(db).disconnect("quickbooks", setup["admin"])

        assert FakeQuickBooks.revoked == [REFRESH_TOKEN]

    def test_a_stale_token_cannot_be_used_afterwards(self, db, tenant, setup):
        """The property that makes disconnect mean something: holding a
        reference to the connection object from before does not get you a
        working call after."""
        connection = _connection(db, tenant)
        IntegrationService(db).disconnect("quickbooks", setup["admin"])
        db.refresh(connection)

        with pytest.raises(ConnectorAuthError):
            IntegrationService(db).refresh_tokens_if_needed(connection)

    def test_a_disconnected_provider_refuses_reads(self, db, tenant, setup):
        _connection(db, tenant)
        IntegrationService(db).disconnect("quickbooks", setup["admin"])

        with pytest.raises(ValueError, match="Not connected"):
            IntegrationService(db).list_accounts("quickbooks", setup["admin"])


class TestKeepingTheTokenAlive:
    def test_a_live_token_is_not_refreshed(self, db, tenant):
        """A refresh per call would rotate the refresh token constantly and
        multiply the chances of losing the rotation."""
        connection = _connection(db, tenant, expires_in_minutes=60)

        IntegrationService(db).refresh_tokens_if_needed(connection)

        assert FakeQuickBooks.refresh_calls == 0

    def test_an_expiring_token_is_refreshed_and_both_halves_stored(self, db, tenant):
        """QuickBooks rotates the refresh token on every use. Storing only the
        new access token leaves this row holding a refresh token that already
        stopped working, and the connection dies silently at the next refresh."""
        connection = _connection(db, tenant, expires_in_minutes=1)

        tokens = IntegrationService(db).refresh_tokens_if_needed(connection)

        assert FakeQuickBooks.refresh_calls == 1
        assert tokens.access_token == ACCESS_TOKEN + "-rotated"
        db.refresh(connection)
        assert decrypt_secret(connection.access_token_encrypted) == ACCESS_TOKEN + "-rotated"
        assert decrypt_secret(connection.refresh_token_encrypted) == REFRESH_TOKEN + "-rotated"

    def test_a_dead_refresh_token_marks_the_connection_expired(self, db, tenant):
        """Nothing here fixes itself — a human has to reconnect, so the status
        has to say so rather than the queue retrying forever."""
        connection = _connection(db, tenant, expires_in_minutes=1)
        FakeQuickBooks.refresh_raises = ConnectorAuthError("invalid_grant")

        with pytest.raises(ConnectorAuthError):
            IntegrationService(db).refresh_tokens_if_needed(connection)

        db.refresh(connection)
        assert connection.status == STATUS_EXPIRED
        assert "invalid_grant" in connection.last_error


# --- configuration ------------------------------------------------------------

class TestConfiguringWhereEntriesPost:
    def test_accounts_must_exist_in_the_last_pull(self, db, tenant, setup):
        """A typo'd id would otherwise fail silently, days later, at the first
        post — when whoever typed it has long moved on."""
        connection = _connection(db, tenant, with_accounts=False)
        _snapshots(db, tenant, connection)

        with pytest.raises(ValueError, match="was not found in the last pull"):
            IntegrationService(db).set_default_accounts(
                "quickbooks", liability_account_external_id="999",
                bank_account_external_id=BANK_ACCOUNT,
                current_user=setup["admin"],
            )

    def test_setting_them_makes_the_connection_ready_to_post(self, db, tenant, setup):
        connection = _connection(db, tenant, with_accounts=False)
        _snapshots(db, tenant, connection)

        updated = IntegrationService(db).set_default_accounts(
            "quickbooks", liability_account_external_id=LIABILITY_ACCOUNT,
            bank_account_external_id=BANK_ACCOUNT, current_user=setup["admin"],
        )

        assert updated.default_liability_account_external_id == LIABILITY_ACCOUNT
        assert updated.default_bank_account_external_id == BANK_ACCOUNT

    def test_a_vendor_maps_to_a_party_from_the_pull(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)

        mapping = IntegrationService(db).map_vendor(
            "quickbooks", setup["vendor"].id, "56", setup["admin"],
        )

        assert mapping.external_party_id == "56"
        assert mapping.mapped_by is not None

    def test_an_unknown_party_is_refused(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)

        with pytest.raises(ValueError, match="was not found in the last pull"):
            IntegrationService(db).map_vendor(
                "quickbooks", setup["vendor"].id, "does-not-exist", setup["admin"],
            )

    def test_a_customer_is_not_a_vendor(self, db, tenant, setup):
        """The party list holds both; mapping a Sarmaya vendor onto a
        QuickBooks *customer* would tag every posted line with the wrong side
        of the ledger."""
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)

        with pytest.raises(ValueError, match="was not found in the last pull"):
            IntegrationService(db).map_vendor(
                "quickbooks", setup["vendor"].id, "77", setup["admin"],
            )

    def test_remapping_replaces_rather_than_duplicates(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)
        service = IntegrationService(db)
        service.map_vendor("quickbooks", setup["vendor"].id, "56", setup["admin"])

        db.add(IntegrationPartySnapshot(
            id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id,
            external_party_id="99", party_type="vendor",
            display_name="Payable Vendor (merged)", is_active=True,
            fetched_at=_now(),
        ))
        db.flush()
        service.map_vendor("quickbooks", setup["vendor"].id, "99", setup["admin"])

        mappings = db.query(IntegrationVendorMapping).all()
        assert len(mappings) == 1
        assert mappings[0].external_party_id == "99"


class TestReferenceDataIsReplacedNotMerged:
    def test_a_refresh_drops_what_the_provider_no_longer_has(self, db, tenant, setup):
        """The snapshot's whole claim is that it matches the provider. A merge
        would leave deleted accounts selectable forever."""
        connection = _connection(db, tenant)
        db.add(IntegrationAccountSnapshot(
            id=uuid.uuid4(), tenant_id=tenant.id, connection_id=connection.id,
            external_account_id="deleted-in-qbo", name="Old Account",
            is_active=True, fetched_at=_now(),
        ))
        db.flush()

        result = IntegrationService(db).refresh_reference_data(
            "quickbooks", setup["admin"]
        )

        ids = {a.external_account_id for a in db.query(IntegrationAccountSnapshot).all()}
        assert "deleted-in-qbo" not in ids
        assert ids == {LIABILITY_ACCOUNT, BANK_ACCOUNT}
        assert result == {"accounts": 2, "vendors": 1, "customers": 1}
        db.refresh(connection)
        assert connection.last_synced_at is not None


# --- enqueueing ---------------------------------------------------------------

class TestQueueingIsOpportunistic:
    """Posting must never block, warn, or slow a payment — today every tenant
    has no accounting system connected, and that has to stay a silent no-op."""

    def test_no_connection_queues_nothing_and_releases_anyway(self, db, setup):
        payment = _released_payment(db, setup)

        assert payment.released_at is not None
        assert _posts(db) == []

    def test_a_connection_without_posting_accounts_queues_nothing(
        self, db, tenant, setup
    ):
        """Half-configured is not ready. Queueing here would build an entry
        with no account to debit and fail on every retry."""
        _connection(db, tenant, with_accounts=False)

        payment = _released_payment(db, setup)

        assert payment.released_at is not None
        assert _posts(db) == []

    def test_a_disconnected_connection_queues_nothing(self, db, tenant, setup):
        _connection(db, tenant, status=STATUS_NOT_CONNECTED)

        _released_payment(db, setup)

        assert _posts(db) == []


class TestQueueingSharesTheActionsTransaction:
    """The property a background thread cannot give you, and the actual point
    of an outbox."""

    def test_releasing_a_payment_queues_exactly_one_post(self, db, tenant, setup):
        _connection(db, tenant)

        payment = _released_payment(db, setup, "1000")

        posts = _posts(db)
        assert len(posts) == 1
        assert posts[0].source_type == SOURCE_PAYMENT
        assert posts[0].source_id == payment.id
        assert posts[0].status == POST_STATUS_PENDING

    def test_paying_an_expense_claim_queues_one_too(self, db, tenant, make_user):
        _connection(db, tenant)

        claim = _paid_claim(db, tenant, make_user, "500")

        posts = _posts(db, source_type=SOURCE_EXPENSE_REIMBURSEMENT)
        assert len(posts) == 1
        assert posts[0].source_id == claim.id

    def test_the_post_joins_the_source_records_story(self, db, tenant, setup):
        """Inherited rather than freshly minted, so an evidence pack for the
        payment shows the posting attempt as part of the same chain."""
        _connection(db, tenant)

        payment = _released_payment(db, setup)

        assert _posts(db)[0].correlation_id == payment.correlation_id

    def test_a_rolled_back_action_queues_nothing(self, db, tenant, setup):
        """A thread started mid-action would have told QuickBooks about a
        payment that never happened. The row goes back with everything else."""
        _connection(db, tenant)
        payment_id = uuid.uuid4()

        class _FakePayment:
            id = payment_id
            payment_number = "PAY-GHOST"
            payment_date = date(2026, 8, 1)
            total_amount = Decimal("100")
            correlation_id = None
            lines = []

        JournalPostingService(db).enqueue(
            SOURCE_PAYMENT, _FakePayment(), setup["admin"]
        )
        db.flush()
        assert len(_posts(db, source_id=payment_id)) == 1

        db.rollback()

        assert _posts(db, source_id=payment_id) == []

    def test_the_payload_is_captured_at_enqueue_time(self, db, tenant, setup):
        """A retry replays what was decided then, not a recomputation against
        records that may have changed since."""
        _connection(db, tenant)

        _released_payment(db, setup, "1000")

        payload = _posts(db)[0].payload
        amounts = {line["amount"] for line in payload["lines"]}
        assert amounts == {"1000.00"}
        accounts = {line["account_external_id"] for line in payload["lines"]}
        assert accounts == {LIABILITY_ACCOUNT, BANK_ACCOUNT}
        directions = [line["direction"] for line in payload["lines"]]
        assert sorted(directions) == ["credit", "debit"]

    def test_a_mapped_vendor_is_tagged_on_the_line(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)
        IntegrationService(db).map_vendor(
            "quickbooks", setup["vendor"].id, "56", setup["admin"]
        )

        _released_payment(db, setup)

        debit = [
            line for line in _posts(db)[0].payload["lines"]
            if line["direction"] == "debit"
        ][0]
        assert debit["party_external_id"] == "56"

    def test_an_unmapped_vendor_still_posts(self, db, tenant, setup):
        """A missing mapping makes QuickBooks's AP aging less readable. It must
        not stop Sarmaya telling it anything at all."""
        _connection(db, tenant)

        _released_payment(db, setup)

        debit = [
            line for line in _posts(db)[0].payload["lines"]
            if line["direction"] == "debit"
        ][0]
        assert debit["party_external_id"] is None


class TestADuplicateQueueRowCannotDiscardThePayment:
    """The bug this class exists for: `enqueue` runs inside
    `release_payment`'s own transaction, before its final commit. A bare
    `rollback()` on the duplicate-post conflict would have discarded the
    caller's entire transaction — the state change, the audit entries, the
    settled invoices — over a problem that has nothing to do with the payment.
    The insert is wrapped in a SAVEPOINT so only the insert unwinds.
    """

    def test_a_second_enqueue_is_refused_without_a_second_row(
        self, db, tenant, setup
    ):
        _connection(db, tenant)
        payment = _released_payment(db, setup)
        assert len(_posts(db)) == 1

        again = JournalPostingService(db).enqueue(
            SOURCE_PAYMENT, payment, setup["admin"]
        )

        assert again is None
        assert len(_posts(db)) == 1

    def test_the_callers_own_work_survives_the_conflict(self, db, tenant, setup):
        """Asserted on work done *before* the conflicting enqueue: if the
        savepoint were a plain rollback, this vendor would be gone."""
        _connection(db, tenant)
        payment = _released_payment(db, setup)

        marker = Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="Mid-transaction",
            status=VendorStatus.ACTIVE,
        )
        db.add(marker)
        db.flush()

        JournalPostingService(db).enqueue(SOURCE_PAYMENT, payment, setup["admin"])

        assert db.query(Vendor).filter(Vendor.id == marker.id).first() is not None
        # And the session is still usable rather than in an aborted transaction.
        db.add(Vendor(
            id=uuid.uuid4(), tenant_id=tenant.id, legal_name="After the conflict",
            status=VendorStatus.ACTIVE,
        ))
        db.flush()


# --- draining -----------------------------------------------------------------

class TestDraining:
    def test_a_successful_post_is_marked_and_not_retried(self, db, tenant, setup):
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection)

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["attempted"] == 1 and result["posted"] == 1
        db.refresh(post)
        assert post.status == POST_STATUS_POSTED
        assert post.posted_at is not None
        assert post.external_transaction_id == "JE-1"
        assert post.external_transaction_type == "JournalEntry"

        # A second run does not tell QuickBooks the same thing twice.
        assert JournalPostingService(db).drain(setup["admin"])["attempted"] == 0
        assert len(FakeQuickBooks.posted) == 1

    def test_a_transient_failure_is_recorded_and_rescheduled(
        self, db, tenant, setup
    ):
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection)
        FakeQuickBooks.post_raises = ConnectorUnavailable("QuickBooks returned 503")

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["retrying"] == 1
        db.refresh(post)
        assert post.status == POST_STATUS_PENDING
        assert post.attempts == 1
        assert "503" in post.last_error
        assert post.next_attempt_at > _now()

    def test_a_post_backing_off_is_not_picked_up_early(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _queued_post(db, tenant, connection,
                     next_attempt_at=_now() + timedelta(minutes=30))

        assert JournalPostingService(db).drain(setup["admin"])["attempted"] == 0
        assert FakeQuickBooks.posted == []

    def test_it_gives_up_after_the_attempt_limit(self, db, tenant, setup):
        """A permanently broken post must not consume the queue forever — it
        becomes a dead letter somebody can see and retry by hand."""
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection, attempts=MAX_ATTEMPTS - 1)
        FakeQuickBooks.post_raises = ConnectorUnavailable("still down")

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["failed"] == 1
        db.refresh(post)
        assert post.status == POST_STATUS_FAILED
        assert post.attempts == MAX_ATTEMPTS

    def test_an_unexpected_error_backs_off_rather_than_crashing_the_run(
        self, db, tenant, setup
    ):
        """One provider's malformed response must not stop every other
        tenant's posts in the same scheduled run."""
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection)
        FakeQuickBooks.post_raises = KeyError("Id")

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["retrying"] == 1
        db.refresh(post)
        assert post.status == POST_STATUS_PENDING
        assert "KeyError" in post.last_error

    def test_a_post_whose_connection_was_removed_waits_rather_than_failing(
        self, db, tenant, setup
    ):
        """Disconnecting is not the post's fault. It stays pending, visible,
        and picked back up if the tenant reconnects — rather than burning
        five attempts against nothing and being marked permanently failed."""
        connection = _connection(db, tenant, status=STATUS_NOT_CONNECTED)
        post = _queued_post(db, tenant, connection)

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["skipped"] == 1
        assert result["retrying"] == 0, "a skipped post was reported as a failure"
        db.refresh(post)
        assert post.status == POST_STATUS_PENDING
        assert post.attempts == 0


class TestADeadConnectionStopsTheRun:
    def test_the_connection_is_marked_expired(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _queued_post(db, tenant, connection)
        FakeQuickBooks.post_raises = ConnectorAuthError("token revoked at Intuit")

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["connections_marked_expired"] == 1
        db.refresh(connection)
        assert connection.status == STATUS_EXPIRED
        assert "revoked" in connection.last_error

    def test_the_rest_of_its_queue_is_left_untouched(self, db, tenant, setup):
        """Their `next_attempt_at` is not moved and their attempts are not
        spent: retrying cannot fix a revoked token, so burning each post's
        budget on it would leave a queue permanently failed by the time
        somebody reconnects."""
        connection = _connection(db, tenant)
        posts = [_queued_post(db, tenant, connection) for _ in range(4)]
        FakeQuickBooks.post_raises = ConnectorAuthError("token revoked")

        result = JournalPostingService(db).drain(setup["admin"])

        assert result["attempted"] == 1, "the drain kept hammering a dead connection"
        for post in posts:
            db.refresh(post)
        assert sorted(p.attempts for p in posts) == [0, 0, 0, 1]
        assert all(p.status == POST_STATUS_PENDING for p in posts)

    def test_another_tenants_connection_is_unaffected(self, db, tenant, setup):
        """The dead-connection rule is per connection, not per run. One
        tenant's revoked token must not hold up everybody else's books."""
        healthy = _connection(db, tenant)
        dead = _connection(
            db, tenant, provider="xero-shaped", external_company_id="other",
        )
        _queued_post(db, tenant, dead)
        _queued_post(db, tenant, healthy)

        # Only the dead connection's provider raises.
        original = FakeQuickBooks.post_journal_entry

        def selective(self, tokens, company_id, entry):
            if company_id == "other":
                raise ConnectorAuthError("token revoked")
            return original(self, tokens, company_id, entry)

        FakeQuickBooks.post_journal_entry = selective
        try:
            integration_module._CONNECTORS["xero-shaped"] = FakeQuickBooks
            result = JournalPostingService(db).drain(setup["admin"])
        finally:
            FakeQuickBooks.post_journal_entry = original
            integration_module._CONNECTORS.pop("xero-shaped", None)

        assert result["posted"] == 1
        assert result["connections_marked_expired"] == 1


class TestRetryingByHand:
    def test_it_preserves_the_attempt_history(self, db, tenant, setup):
        """An auditor asking "how many times did this fail before anybody
        looked" must still get an answer after the retry succeeds."""
        connection = _connection(db, tenant)
        post = _queued_post(
            db, tenant, connection, status=POST_STATUS_FAILED,
            attempts=MAX_ATTEMPTS, next_attempt_at=None,
            last_error="QuickBooks returned 503",
        )

        retried = JournalPostingService(db).retry(post.id, setup["admin"])

        assert retried.status == POST_STATUS_PENDING
        assert retried.attempts == MAX_ATTEMPTS, "the failure history was erased"
        assert retried.next_attempt_at is not None

    def test_a_retried_post_is_then_drained(self, db, tenant, setup):
        connection = _connection(db, tenant)
        post = _queued_post(
            db, tenant, connection, status=POST_STATUS_FAILED,
            attempts=MAX_ATTEMPTS, next_attempt_at=None,
        )
        service = JournalPostingService(db)
        service.retry(post.id, setup["admin"])

        # attempts is already at the limit, so this proves the retry genuinely
        # re-opens the post rather than the drain re-failing it immediately.
        result = service.drain(setup["admin"])

        assert result["posted"] == 1

    def test_only_a_failed_post_can_be_retried(self, db, tenant, setup):
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection, status=POST_STATUS_POSTED)

        with pytest.raises(ValueError, match="Only a failed post"):
            JournalPostingService(db).retry(post.id, setup["admin"])


# --- who may do any of this ---------------------------------------------------

class TestPermissions:
    """Connecting touches OAuth credentials for the tenant's own accounting
    system, so managing it is admin-only. Reading is wider: a CFO or auditor
    needs to see whether the books are actually being told anything."""

    def test_an_ordinary_role_cannot_connect(self, db, setup):
        with pytest.raises(PermissionError):
            IntegrationService(db).begin_connect(
                "quickbooks", setup["clerk"], "https://app.test/cb"
            )

    def test_an_ordinary_role_cannot_disconnect(self, db, tenant, setup):
        _connection(db, tenant)
        with pytest.raises(PermissionError):
            IntegrationService(db).disconnect("quickbooks", setup["clerk"])

    def test_a_cfo_cannot_connect_either(self, db, setup):
        """View, not manage — mirrors how bank-statement reconciliation and
        hr.view_compensation are already scoped."""
        with pytest.raises(PermissionError):
            IntegrationService(db).begin_connect(
                "quickbooks", setup["cfo"], "https://app.test/cb"
            )

    def test_a_cfo_cannot_change_where_entries_post(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)
        with pytest.raises(PermissionError):
            IntegrationService(db).set_default_accounts(
                "quickbooks", liability_account_external_id=LIABILITY_ACCOUNT,
                bank_account_external_id=BANK_ACCOUNT, current_user=setup["cfo"],
            )

    def test_a_cfo_can_read_the_status(self, db, tenant, setup):
        _connection(db, tenant)
        status = IntegrationService(db).get_status("quickbooks", setup["cfo"])
        assert status.status == STATUS_CONNECTED

    def test_an_auditor_can_read_the_queue(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _queued_post(db, tenant, connection)
        assert len(JournalPostingService(db).list_posts(setup["auditor"])) == 1

    def test_the_queue_is_filtered_to_the_provider_asked_for(self, db, tenant, setup):
        """The route is /integrations/{provider}/posts. Unfiltered, a tenant on
        two providers would see one list under both — each quietly claiming the
        other's failures as its own."""
        quickbooks = _connection(db, tenant)
        other = _connection(
            db, tenant, provider="second-provider", external_company_id="other",
        )
        _queued_post(db, tenant, quickbooks)
        _queued_post(db, tenant, other)

        service = JournalPostingService(db)

        assert len(service.list_posts(setup["auditor"], provider="quickbooks")) == 1
        assert len(service.list_posts(setup["auditor"], provider="second-provider")) == 1
        # Unfiltered still means everything, for the drain and the job monitor.
        assert len(service.list_posts(setup["auditor"])) == 2

    def test_an_ordinary_role_cannot_read_the_queue(self, db, tenant, setup):
        """Queue rows carry the amounts and vendors of released payments, so
        they are as private as the payments they describe."""
        connection = _connection(db, tenant)
        _queued_post(db, tenant, connection)
        with pytest.raises(PermissionError):
            JournalPostingService(db).list_posts(setup["clerk"])

    def test_an_ordinary_role_cannot_drain_it(self, db, setup):
        with pytest.raises(PermissionError):
            JournalPostingService(db).drain(setup["clerk"])

    def test_an_ordinary_role_cannot_retry_a_post(self, db, tenant, setup):
        connection = _connection(db, tenant)
        post = _queued_post(db, tenant, connection, status=POST_STATUS_FAILED)
        with pytest.raises(PermissionError):
            JournalPostingService(db).retry(post.id, setup["clerk"])

    def test_an_ordinary_role_cannot_map_vendors(self, db, tenant, setup):
        connection = _connection(db, tenant)
        _snapshots(db, tenant, connection)
        with pytest.raises(PermissionError):
            IntegrationService(db).map_vendor(
                "quickbooks", setup["vendor"].id, "56", setup["clerk"],
            )


# --- tenant isolation, under the role that cannot bypass it -------------------

def _swap_to_test_db(url: str) -> str:
    """Point the least-privilege role's URL at whichever database the suite is
    running against — same helper, same reasoning, as test_rls_isolation.py."""
    head, _, db = url.rpartition("/")
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        db = override.rpartition("/")[2]
    else:
        db = f"{db}_test"
    return f"{head}/{db}"


def _ensure_integration_rls(admin_conn) -> None:
    """create_all() builds the schema but not RLS; mirror migration 042 on
    integration_connections so this exercises the real policies."""
    admin_conn.execute(text(
        "ALTER TABLE integration_connections ENABLE ROW LEVEL SECURITY"
    ))
    admin_conn.execute(text(
        "ALTER TABLE integration_connections FORCE ROW LEVEL SECURITY"
    ))
    for policy in ("integration_connections_tenant_isolation",
                   "integration_connections_tenant_insert"):
        admin_conn.execute(text(
            f"DROP POLICY IF EXISTS {policy} ON integration_connections"
        ))
    admin_conn.execute(text(
        "CREATE POLICY integration_connections_tenant_isolation ON "
        "integration_connections USING "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    admin_conn.execute(text(
        "CREATE POLICY integration_connections_tenant_insert ON "
        "integration_connections FOR INSERT WITH CHECK "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    admin_conn.execute(text(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON integration_connections TO os_app"
    ))


@pytest.fixture
def rls(db_engine):
    """Two tenants, each with a connected accounting system, seeded through the
    admin connection; yields an os_app-bound session factory."""
    app_engine = create_engine(
        _swap_to_test_db(settings.DATABASE_URL), pool_pre_ping=True
    )

    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    conn_a, conn_b = uuid.uuid4(), uuid.uuid4()

    AdminSession = sessionmaker(bind=db_engine, autoflush=False)
    setup = AdminSession()
    try:
        _ensure_integration_rls(setup.connection())
        setup.add_all([
            Tenant(id=tenant_a, name="Tenant A", slug=f"a-{tenant_a.hex[:8]}"),
            Tenant(id=tenant_b, name="Tenant B", slug=f"b-{tenant_b.hex[:8]}"),
        ])
        setup.flush()
        setup.add_all([
            IntegrationConnection(
                id=conn_a, tenant_id=tenant_a, provider="quickbooks",
                status=STATUS_CONNECTED,
                access_token_encrypted=encrypt_secret("tenant-a-token"),
            ),
            IntegrationConnection(
                id=conn_b, tenant_id=tenant_b, provider="quickbooks",
                status=STATUS_CONNECTED,
                access_token_encrypted=encrypt_secret("tenant-b-token"),
            ),
        ])
        setup.commit()
    except Exception:
        setup.rollback()
        setup.close()
        app_engine.dispose()
        raise

    try:
        yield {
            "AppSession": sessionmaker(bind=app_engine, autoflush=False),
            "tenant_a": tenant_a, "tenant_b": tenant_b,
            "conn_a": conn_a, "conn_b": conn_b,
        }
    finally:
        setup.execute(
            text("DELETE FROM integration_connections WHERE id IN (:a, :b)"),
            {"a": str(conn_a), "b": str(conn_b)},
        )
        setup.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                      {"a": str(tenant_a), "b": str(tenant_b)})
        setup.commit()
        setup.close()
        app_engine.dispose()


class TestOneTenantCannotReachAnothersCredentials:
    """The rest of this module runs as the privileged role and so bypasses
    RLS — it tests service logic. These connect as `os_app`, which cannot,
    so the policies from migration 042 are actually enforced. Worth its own
    setup here rather than trusting the vendors table's proof: this is the
    table holding other people's accounting credentials."""

    def test_a_tenant_sees_only_its_own_connection(self, rls):
        session = rls["AppSession"]()
        try:
            set_tenant_context(session, str(rls["tenant_a"]))
            ids = {c.id for c in session.query(IntegrationConnection).all()}
            assert rls["conn_a"] in ids
            assert rls["conn_b"] not in ids
        finally:
            session.close()

    def test_no_tenant_context_sees_nothing(self, rls):
        """Not "sees everything" — the property `complete_connect` depends on
        when it resolves the tenant from `state` before querying anything."""
        session = rls["AppSession"]()
        try:
            visible = session.query(IntegrationConnection).filter(
                IntegrationConnection.id.in_([rls["conn_a"], rls["conn_b"]])
            ).count()
            assert visible == 0
        finally:
            session.close()

    def test_a_connection_cannot_be_planted_on_another_tenant(self, rls):
        session = rls["AppSession"]()
        try:
            set_tenant_context(session, str(rls["tenant_a"]))
            session.add(IntegrationConnection(
                id=uuid.uuid4(), tenant_id=rls["tenant_b"], provider="quickbooks",
                status=STATUS_CONNECTED,
                access_token_encrypted=encrypt_secret("smuggled"),
            ))
            with pytest.raises(DBAPIError):
                session.flush()
        finally:
            session.rollback()
            session.close()
