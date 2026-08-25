"""The Integration Hub: a tenant's own accounting system, connected.

Build Book: "Integration connectors implemented with retries, idempotency,
dead-letter queues, and reconciliation records" — the last unbuilt item in the
Definition of Done. Confirmed unbuilt before this file: no reference to
"integration", "webhook" or "external_id" existed anywhere in the codebase
except the notification outbox's own docstring, which names itself as the
shape "a webhook sender would reuse."

QuickBooks Online is the first, and for now the only, provider — see
`app/services/finance_connectors/`. Xero and SAP are deferred deliberately: an
interface designed against three specs with zero working code is an interface
shaped by guesswork, which is the mistake this slice exists to avoid making
twice (the AI and OCR provider modules made a *different* mistake worth not
repeating either — see the note on `provider` below).

Two decisions run through every table here:

**A client's data is pulled once and refreshed on demand, never continuously
synced.** `IntegrationAccountSnapshot` and `IntegrationPartySnapshot` are
delete-and-replace wholesale copies, not a live two-way mirror. There is no
conflict resolution anywhere in this file because there is nothing to
reconcile — Sarmaya never edits a QuickBooks record, so the two copies can
never disagree about who changed what.

**Sarmaya pushes a fact after it happened, never a request before it did.**
`IntegrationJournalPost` exists only for events where money has already moved
(a payment released, a claim paid) — see the two call sites in
`payment_service.py` and `expense_service.py`. Nothing here posts an invoice
approval or a payroll change; both are out of scope for reasons recorded at
those call sites.
"""
from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

# --- connection status -------------------------------------------------------
STATUS_NOT_CONNECTED = "not_connected"
STATUS_CONNECTED = "connected"
#: The access token could not be refreshed and the connection needs a human to
#: reconnect. Distinct from a transient failure: nothing here will fix itself.
STATUS_EXPIRED = "expired"
STATUS_ERROR = "error"

CONNECTION_STATUSES = (
    STATUS_NOT_CONNECTED, STATUS_CONNECTED, STATUS_EXPIRED, STATUS_ERROR,
)

#: How long a pending OAuth handshake stays valid before the state token is
#: treated as stale. Long enough that a slow consent screen doesn't strand
#: someone, short enough that a state value is not usefully guessable-and-
#: reusable for long.
OAUTH_STATE_TTL_MINUTES = 10


class IntegrationConnection(BaseModel):
    """One tenant's link to one external accounting system.

    `provider` is a string, not an enum — the same reasoning `JobRun.job_name`
    already gives for the same choice: a second provider should not need a
    migration to become visible. This is *not* the `AIProvider`/`OCRProvider`
    pattern, and deliberately so: those are resolved from one global
    `settings.AI_PROVIDER` for the whole deployment. This has to be per
    tenant, per provider, connectable and disconnectable — so the model that
    matters here is this row, not a class hierarchy behind a settings flag.
    """
    __tablename__ = "integration_connections"

    OBJECT_TYPE = "integration_connection"
    REFERENCE_FIELD = "provider"
    #: No WORKFLOW_TYPE. There is no approval routing or SLA escalation on a
    #: connection — status is read by the scheduled health job, not by
    #: app/services/workflow.py's SLA engine. Declaring one without matching
    #: states in DEFAULT_WORKFLOWS is exactly the silent gap DR-048 exists to
    #: name; the fix here is not to make that mistake rather than to catch it
    #: afterward.

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    provider = Column(String(30), nullable=False, index=True)
    status = Column(
        String(20), nullable=False, default=STATUS_NOT_CONNECTED, index=True,
    )

    #: QuickBooks calls this the "realm id". Named generically because the
    #: column, not the concept, is what a second provider would reuse.
    external_company_id = Column(String(100), nullable=True)
    external_company_name = Column(String(255), nullable=True)

    #: Fernet ciphertext. Same key derivation as app/core/mfa.py's TOTP
    #: secret — from SECRET_KEY, via the same _fernet(), reused verbatim
    #: rather than a second scheme. A second configured encryption key would
    #: be a second way for a deployment to end up unprotected by omission,
    #: which is the exact trade mfa.py's own docstring already made once.
    access_token_encrypted = Column(String, nullable=True)
    refresh_token_encrypted = Column(String, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    #: QuickBooks refresh tokens expire roughly 100 days from issue and
    #: rotate on every use. Tracked so the connection can be flagged before
    #: the hard wall an access-token refresh alone cannot cross.
    refresh_token_expires_at = Column(DateTime, nullable=True)

    #: The pending OAuth CSRF nonce, alive only between initiating a connect
    #: and its callback landing. Stored rather than only signed: the callback
    #: request carries no bearer token to say which tenant it belongs to, so
    #: this row *is* the authentication for that request — a stored,
    #: single-use, expiring value is what makes both "this state was never
    #: issued" and "this state was already used" refusable.
    oauth_state = Column(String(128), nullable=True, index=True)
    oauth_state_expires_at = Column(DateTime, nullable=True)
    #: Who clicked "Connect". Becomes connected_by once the callback lands,
    #: because the callback request itself carries no user.
    oauth_initiated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    connected_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    connected_at = Column(DateTime, nullable=True)
    disconnected_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    disconnected_at = Column(DateTime, nullable=True)

    #: Which external accounts a posted journal entry debits and credits.
    #: Set once by the admin, from the pulled chart of accounts, after
    #: connecting — a journal entry cannot be built at all without them, and
    #: guessing at "Accounts Payable" or "Bank" by name would silently post
    #: to the wrong account in a chart that spells either differently. Left
    #: unset, JournalPostingService.enqueue treats the connection as not yet
    #: ready to post to, the same opportunistic no-op as no connection at all.
    default_liability_account_external_id = Column(String(100), nullable=True)
    default_bank_account_external_id = Column(String(100), nullable=True)

    #: Last successful reference-data pull, not last API call generally.
    last_synced_at = Column(DateTime, nullable=True)
    #: The last connect/refresh/health failure, human-readable. Never a
    #: token, never a header — this is read by the status page.
    last_error = Column(String, nullable=True)

    tenant = relationship("Tenant", backref="integration_connections")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", name="uq_integration_connections_tenant_provider"
        ),
    )


class IntegrationAccountSnapshot(BaseModel):
    """The tenant's chart of accounts, as of the last pull.

    A table rather than a JSON blob on the connection: the mapping screen
    needs to search and filter this list, and pushing that filtering into
    Python over a blob gets slower as the chart grows where an indexed table
    does not. Wholesale delete-and-replace on refresh keeps the "never
    incrementally edited" property regardless.
    """
    __tablename__ = "integration_account_snapshots"

    OBJECT_TYPE = "integration_account_snapshot"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    external_account_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    #: QuickBooks's AccountType / AccountSubType, e.g. "Expense", "Bank".
    #: Free text rather than an enum: the vocabulary belongs to the external
    #: provider, and a second provider's categories will not be QuickBooks's.
    account_type = Column(String(50), nullable=True, index=True)
    account_sub_type = Column(String(50), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    fetched_at = Column(DateTime, nullable=False)

    connection = relationship("IntegrationConnection", backref="account_snapshots")

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_account_id",
            name="uq_account_snapshot_connection_external",
        ),
    )


class IntegrationPartySnapshot(BaseModel):
    """The tenant's vendors and customers, as of the last pull.

    Read alongside `IntegrationVendorMapping` when deciding whether a Sarmaya
    vendor already exists in the client's book — the reason this is pulled at
    all rather than left to be discovered as duplicate entries later.
    """
    __tablename__ = "integration_party_snapshots"

    OBJECT_TYPE = "integration_party_snapshot"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    external_party_id = Column(String(100), nullable=False)
    party_type = Column(String(20), nullable=False, index=True)  # "vendor" | "customer"
    display_name = Column(String(255), nullable=False, index=True)
    email = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    fetched_at = Column(DateTime, nullable=False)

    connection = relationship("IntegrationConnection", backref="party_snapshots")

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_party_id", "party_type",
            name="uq_party_snapshot_connection_external",
        ),
    )


class IntegrationVendorMapping(BaseModel):
    """A Sarmaya vendor, linked to the external party it already is.

    A separate table rather than a column on `Vendor`: this feature has to
    stay fully removable without a migration touching a model that otherwise
    has nothing to do with QuickBooks. Disconnecting a provider, or dropping
    the integration entirely, means dropping this table — `vendors` is
    untouched either way.
    """
    __tablename__ = "integration_vendor_mappings"

    OBJECT_TYPE = "integration_vendor_mapping"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False, index=True,
    )
    external_party_id = Column(String(100), nullable=False)

    mapped_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    mapped_at = Column(DateTime, nullable=False)

    connection = relationship("IntegrationConnection", backref="vendor_mappings")
    vendor = relationship("Vendor")

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "vendor_id", name="uq_vendor_mapping_connection_vendor"
        ),
    )


# --- the outbound queue ------------------------------------------------------
POST_STATUS_PENDING = "pending"
POST_STATUS_POSTED = "posted"
#: Given up on after MAX_ATTEMPTS. Distinct from pending-with-attempts, the
#: same distinction notification_outbox draws for the same reason: something
#: has to be able to say "this was never delivered" without a human counting.
POST_STATUS_FAILED = "failed"

POST_STATUSES = (POST_STATUS_PENDING, POST_STATUS_POSTED, POST_STATUS_FAILED)

#: What can enqueue a post. Only events where money has already moved — see
#: the module docstring and the call sites in payment_service.py /
#: expense_service.py for why invoice approval and payroll changes are not
#: on this list.
SOURCE_PAYMENT = "payment"
SOURCE_EXPENSE_REIMBURSEMENT = "expense_reimbursement"

POST_SOURCE_TYPES = (SOURCE_PAYMENT, SOURCE_EXPENSE_REIMBURSEMENT)

#: After this many attempts a post stops being retried automatically and
#: waits for a human to look — copied from notification_outbox's own
#: MAX_ATTEMPTS, same reasoning: enough to ride out a working day of an
#: unreachable provider, not so many that a permanently broken post silently
#: consumes the queue forever.
MAX_ATTEMPTS = 5


class IntegrationJournalPost(BaseModel):
    """One fact, queued to be told to the tenant's own accounting system.

    Copies `NotificationOutbox`'s retry/backoff/give-up shape as its own
    table rather than reusing that one directly — a journal-entry post has a
    different payload and a different target (a connector's API, not a mail
    server or an in-app row) — but keeps its defining property: enqueued in
    the *same transaction* as the action it describes, so an approval that
    rolls back never enqueues a post, and one that commits always does.
    """
    __tablename__ = "integration_journal_posts"

    OBJECT_TYPE = "integration_journal_post"
    #: A queue row has no number of its own — it is identified by what it is
    #: posting. Composed from its own columns via the reference property
    #: below, so rendering a timeline never lazy-loads the source record.
    REFERENCE_FIELD = "reference"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    connection_id = Column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    source_type = Column(String(30), nullable=False, index=True)
    source_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    #: Inherited from the source record, so this row joins that record's
    #: story in an evidence pack rather than starting one of its own.
    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    #: The provider-neutral posting request (a JournalEntryRequest, see
    #: app/services/finance_connectors/base.py), captured once at enqueue
    #: time. A retry replays exactly what was decided then rather than
    #: recomputing it against records that may have changed since — the same
    #: reasoning inventory_adjustments snapshots total_value instead of
    #: recomputing it at approval time.
    payload = Column(JSON, nullable=False)

    status = Column(
        String(20), nullable=False, default=POST_STATUS_PENDING, index=True,
    )
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    posted_at = Column(DateTime, nullable=True)

    #: What the provider assigned it once posted — the reconciliation
    #: anchor, and what a human checks the client's own books against.
    external_transaction_id = Column(String(100), nullable=True)
    external_transaction_type = Column(String(50), nullable=True)  # "JournalEntry"

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    connection = relationship("IntegrationConnection", backref="journal_posts")

    __table_args__ = (
        # The DB-level idempotency floor: the same Sarmaya record can only
        # ever have one queued post per connection. Belt-and-braces alongside
        # the state guards on payment/expense release that make each of those
        # reachable only once per record — this is what turns a bug in one of
        # those guards into a refused insert instead of a duplicate post.
        UniqueConstraint(
            "connection_id", "source_type", "source_id",
            name="uq_journal_post_connection_source",
        ),
        Index(
            "ix_integration_journal_posts_due", "status", "next_attempt_at",
            postgresql_where=(status == POST_STATUS_PENDING),
        ),
    )

    @property
    def reference(self) -> str:
        """"payment 4f2a..." — what a person would say about this row."""
        return f"{self.source_type} {str(self.source_id)[:8]}"
