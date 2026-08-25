"""The connector interface: what any accounting system has to be able to do.

Build Book: Integration Hub. One provider is implemented against this —
`quickbooks.py` — deliberately the only one for now. An interface designed
against three vendors' documentation with zero working code behind any of
them is an interface shaped by guesswork; this one is shaped by a real
integration instead, and Xero/SAP are deferred until each can do the same.

Two things this interface does NOT promise, on purpose:

**No `revoke` method.** QuickBooks has a revoke endpoint; `disconnect` in
`integration_service.py` calls it directly on the concrete `QuickBooksConnector`
as a best-effort extra, not through the ABC. A method only earns a place here
once a second provider needs the same shape — adding it speculatively now
would be designing for Xero before Xero exists.

**No pull beyond a snapshot.** `list_accounts` and `list_parties` return
whatever the provider has *right now*; nothing here subscribes to changes or
promises the result will still be accurate five minutes later. The caller
(`IntegrationService.refresh_reference_data`) treats every call as a wholesale
replace, never a merge — see that module's docstring for why there is nothing
to reconcile.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional


@dataclass
class OAuthTokens:
    """What a token exchange or refresh hands back.

    `refresh_token` is always present even on a refresh response — QuickBooks
    rotates it on every use, and a caller that only reads the new access
    token and keeps the old refresh token will find the connection dead at
    the next refresh, having no warning it was ever wrong.
    """
    access_token: str
    refresh_token: str
    expires_at: datetime
    refresh_token_expires_at: Optional[datetime] = None


@dataclass
class ChartOfAccountsEntry:
    external_id: str
    name: str
    #: The provider's own vocabulary (QuickBooks: "Expense", "Bank", ...).
    #: Free text rather than a shared enum — a second provider's categories
    #: will not be QuickBooks's, and translating them into one vocabulary is
    #: a mapping problem for whoever builds that provider, not for this type.
    account_type: Optional[str] = None
    account_sub_type: Optional[str] = None
    is_active: bool = True


@dataclass
class ExternalParty:
    external_id: str
    party_type: str  # "vendor" | "customer"
    display_name: str
    email: Optional[str] = None
    is_active: bool = True


@dataclass
class JournalEntryLine:
    account_external_id: str
    amount: Decimal
    direction: str  # "debit" | "credit"
    party_external_id: Optional[str] = None
    description: Optional[str] = None


@dataclass
class JournalEntryRequest:
    """A provider-neutral posting request.

    `reference_number` is the queue row's own id, truncated to whatever limit
    the provider imposes on its own document-number field. It is what makes a
    retry after a lost response detectable rather than a duplicate — see
    `QuickBooksConnector.post_journal_entry` for the check this enables.
    """
    reference_number: str
    entry_date: date
    memo: str
    lines: List[JournalEntryLine] = field(default_factory=list)


@dataclass
class JournalEntryResult:
    external_transaction_id: str
    external_transaction_type: str


class ConnectorAuthError(Exception):
    """The token is dead and refreshing it did not fix that.

    Not transient — retrying the same call again will not help. The caller's
    job on seeing this is to mark the connection `expired` and stop, not to
    back off and try again.
    """


class ConnectorUnavailable(Exception):
    """The provider could not be reached, or asked to be tried again later
    (rate limiting, a 5xx, a timeout). Transient — worth a retry with
    backoff, which is exactly what `IntegrationJournalPost`'s queue does."""


class FinanceConnector(ABC):
    """One accounting system, from Sarmaya's side of the connection.

    Every method takes the tokens and any provider-specific identifiers it
    needs explicitly, rather than holding connection state on the instance —
    the same reasoning `StockService` is one writer for many callers rather
    than one instance per caller: a connector is stateless and safe to
    construct fresh per call, so nothing about *which* tenant is calling can
    leak between requests by accident.
    """

    provider_name: str

    @abstractmethod
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        """Where to send the browser to ask the tenant's admin for consent."""

    @abstractmethod
    def exchange_code(
        self, code: str, redirect_uri: str, company_id: Optional[str]
    ) -> OAuthTokens:
        """Trade the authorization code the callback received for tokens."""

    @abstractmethod
    def refresh(self, refresh_token: str) -> OAuthTokens:
        """A new access token, and — see OAuthTokens — a new refresh token
        the caller must store in place of the old one."""

    @abstractmethod
    def list_accounts(
        self, tokens: OAuthTokens, company_id: str
    ) -> List[ChartOfAccountsEntry]:
        """The chart of accounts, right now. A snapshot, not a subscription."""

    @abstractmethod
    def list_parties(
        self, tokens: OAuthTokens, company_id: str
    ) -> List[ExternalParty]:
        """Vendors and customers, right now."""

    @abstractmethod
    def post_journal_entry(
        self, tokens: OAuthTokens, company_id: str, entry: JournalEntryRequest
    ) -> JournalEntryResult:
        """Tell the provider a fact that already happened in Sarmaya.

        Must be safe to call twice with the same `entry.reference_number` —
        the queue's retry path guarantees at-least-once delivery of this
        call, not exactly-once, so the implementation is the layer that has
        to make a repeat harmless.
        """

    @abstractmethod
    def check_health(self, tokens: OAuthTokens, company_id: str) -> bool:
        """A cheap, side-effect-free read that only succeeds if the token
        actually still works — not merely well-formed."""
