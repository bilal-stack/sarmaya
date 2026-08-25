"""QuickBooks Online, as a `FinanceConnector`.

The first — and for this slice, only — implementation of the interface in
`base.py`. Three design choices worth stating up front:

**OAuth is hand-rolled with `httpx`, not a library.** QuickBooks's
authorization-code flow is three plain HTTP calls (build a URL, exchange a
code, refresh a token), no PKCE, no discovery document. `httpx` is already a
dependency; adding `authlib` for three calls would be a dependency for its
own sake.

**Posts a `JournalEntry`, never a `Bill`.** A QuickBooks `Bill` is an unpaid
liability — it carries due dates and shows up in the client's AP aging the
moment it exists. Sarmaya only enqueues a post *after* a payment has already
released or a claim has already been paid (see `app/models/integration.py`'s
module docstring and the two enqueue call sites), so telling QuickBooks "you
now owe this" would be false at the moment of posting. A `JournalEntry`
records a fact that already happened — a debit and a credit — which is what
"notify the external ledger" actually means here.

**Idempotency needs an extra check, because QuickBooks gives us nothing for
it.** There is no idempotency-key header on this API. `reference_number` (the
queue row's own id) is written into QuickBooks's `DocNumber` field on every
attempt including retries; before creating a new entry, this connector reads
back a `JournalEntry` with that `DocNumber` and treats a match as success
rather than posting a second one. This is the one place the queue's own
unique constraint (`uq_journal_post_connection_source`) doesn't help — that
constraint stops the *same Sarmaya record* being enqueued twice, but a lost
HTTP response after QuickBooks already wrote the entry needs this read-before-
write instead.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.finance_connectors.base import (
    ChartOfAccountsEntry, ConnectorAuthError, ConnectorUnavailable,
    ExternalParty, FinanceConnector, JournalEntryRequest, JournalEntryResult,
    OAuthTokens,
)

logger = logging.getLogger(__name__)

_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

#: QBO's DocNumber field caps at 21 characters. A UUID is 36 — truncated
#: rather than hashed, because the whole point is a human (or this connector,
#: reading it back) can eyeball-match it to the queue row later.
_DOC_NUMBER_MAX = 21

#: Requests this short-lived; a hung QuickBooks connection must not hold a
#: dispatcher worker (and the tenant behind it in the drain loop) hostage.
_TIMEOUT_SECONDS = 20.0


def _api_base(environment: str) -> str:
    if environment == "production":
        return "https://quickbooks.api.intuit.com"
    return "https://sandbox-quickbooks.api.intuit.com"


class QuickBooksConnector(FinanceConnector):
    provider_name = "quickbooks"

    def __init__(self):
        self._environment = settings.QBO_ENVIRONMENT
        self._client_id = settings.QBO_CLIENT_ID
        self._client_secret = settings.QBO_CLIENT_SECRET

    # --- OAuth -----------------------------------------------------------

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(
        self, code: str, redirect_uri: str, company_id: Optional[str]
    ) -> OAuthTokens:
        return self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        })

    def refresh(self, refresh_token: str) -> OAuthTokens:
        return self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })

    def _token_request(self, form: dict) -> OAuthTokens:
        try:
            response = httpx.post(
                _TOKEN_URL,
                data=form,
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(f"Could not reach QuickBooks: {exc}") from exc

        if response.status_code in (400, 401):
            # invalid_grant: the code or refresh token is dead. Not
            # transient — retrying the same call again will not help.
            raise ConnectorAuthError(
                f"QuickBooks refused the token request: {response.text[:300]}"
            )
        if response.status_code >= 500:
            raise ConnectorUnavailable(
                f"QuickBooks token endpoint returned {response.status_code}"
            )
        response.raise_for_status()

        body = response.json()
        now = datetime.now(timezone.utc)
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=now + timedelta(seconds=int(body.get("expires_in", 3600))),
            refresh_token_expires_at=(
                now + timedelta(seconds=int(body["x_refresh_token_expires_in"]))
                if "x_refresh_token_expires_in" in body else None
            ),
        )

    def revoke(self, refresh_token: str) -> None:
        """Best-effort. Called directly by IntegrationService.disconnect, not
        through the FinanceConnector interface — see base.py's docstring on
        why revoke is not an abstract method yet."""
        try:
            httpx.post(
                _REVOKE_URL,
                json={"token": refresh_token},
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            logger.warning(
                "QuickBooks token revoke failed; the local connection is "
                "still removed. The token will simply expire on its own."
            )

    # --- reference data ----------------------------------------------------

    def list_accounts(
        self, tokens: OAuthTokens, company_id: str
    ) -> List[ChartOfAccountsEntry]:
        rows = self._query(tokens, company_id, "SELECT * FROM Account")
        return [
            ChartOfAccountsEntry(
                external_id=row["Id"],
                name=row["Name"],
                account_type=row.get("AccountType"),
                account_sub_type=row.get("AccountSubType"),
                is_active=row.get("Active", True),
            )
            for row in rows
        ]

    def list_parties(
        self, tokens: OAuthTokens, company_id: str
    ) -> List[ExternalParty]:
        parties = []
        for entity, party_type in (("Vendor", "vendor"), ("Customer", "customer")):
            for row in self._query(tokens, company_id, f"SELECT * FROM {entity}"):
                parties.append(ExternalParty(
                    external_id=row["Id"],
                    party_type=party_type,
                    display_name=row.get("DisplayName", row.get("Name", "")),
                    email=(row.get("PrimaryEmailAddr") or {}).get("Address"),
                    is_active=row.get("Active", True),
                ))
        return parties

    def _query(self, tokens: OAuthTokens, company_id: str, query: str) -> List[dict]:
        """QBO's query endpoint, paginated.

        QBO returns at most 1000 rows a call and expects STARTPOSITION to
        page through more — a chart of accounts or vendor list this large is
        unlikely for the target client, but a silent 1000-row cap on a
        vendor list would be a strange thing to discover months later.
        """
        results: List[dict] = []
        start = 1
        page_size = 1000
        entity = query.split("FROM", 1)[1].strip().split()[0]

        while True:
            paged = f"{query} STARTPOSITION {start} MAXRESULTS {page_size}"
            body = self._get(
                tokens, company_id, "query", params={"query": paged},
            )
            page = body.get("QueryResponse", {}).get(entity, [])
            results.extend(page)
            if len(page) < page_size:
                break
            start += page_size

        return results

    # --- posting -------------------------------------------------------------

    def post_journal_entry(
        self, tokens: OAuthTokens, company_id: str, entry: JournalEntryRequest
    ) -> JournalEntryResult:
        doc_number = entry.reference_number[:_DOC_NUMBER_MAX]

        existing = self._find_by_doc_number(tokens, company_id, doc_number)
        if existing is not None:
            logger.info(
                "JournalEntry DocNumber=%s already exists in QuickBooks "
                "(id=%s); treating this attempt as already-succeeded rather "
                "than posting a duplicate.",
                doc_number, existing["Id"],
            )
            return JournalEntryResult(
                external_transaction_id=existing["Id"],
                external_transaction_type="JournalEntry",
            )

        payload = {
            "DocNumber": doc_number,
            "TxnDate": entry.entry_date.isoformat(),
            "PrivateNote": entry.memo[:4000],
            "Line": [
                {
                    "DetailType": "JournalEntryLineDetail",
                    "Amount": str(abs(line.amount)),
                    "Description": (line.description or "")[:4000] or None,
                    "JournalEntryLineDetail": {
                        "PostingType": "Debit" if line.direction == "debit" else "Credit",
                        "AccountRef": {"value": line.account_external_id},
                        **(
                            {"Entity": {
                                "Type": "Vendor",
                                "EntityRef": {"value": line.party_external_id},
                            }}
                            if line.party_external_id else {}
                        ),
                    },
                }
                for line in entry.lines
            ],
        }

        body = self._post(tokens, company_id, "journalentry", json=payload)
        created = body["JournalEntry"]
        return JournalEntryResult(
            external_transaction_id=created["Id"],
            external_transaction_type="JournalEntry",
        )

    def _find_by_doc_number(
        self, tokens: OAuthTokens, company_id: str, doc_number: str
    ) -> Optional[dict]:
        escaped = doc_number.replace("'", "''")
        rows = self._query(
            tokens, company_id,
            f"SELECT * FROM JournalEntry WHERE DocNumber = '{escaped}'",
        )
        return rows[0] if rows else None

    # --- health --------------------------------------------------------------

    def check_health(self, tokens: OAuthTokens, company_id: str) -> bool:
        try:
            self._get(tokens, company_id, f"companyinfo/{company_id}")
            return True
        except (ConnectorAuthError, ConnectorUnavailable):
            return False

    # --- HTTP plumbing ---------------------------------------------------

    def _get(self, tokens: OAuthTokens, company_id: str, path: str, params=None) -> dict:
        return self._request("GET", tokens, company_id, path, params=params)

    def _post(self, tokens: OAuthTokens, company_id: str, path: str, json=None) -> dict:
        return self._request("POST", tokens, company_id, path, json=json)

    def _request(
        self, method: str, tokens: OAuthTokens, company_id: str, path: str,
        params=None, json=None,
    ) -> dict:
        url = f"{_api_base(self._environment)}/v3/company/{company_id}/{path}"
        try:
            response = httpx.request(
                method, url, params=params, json=json,
                headers={
                    "Authorization": f"Bearer {tokens.access_token}",
                    "Accept": "application/json",
                },
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(f"Could not reach QuickBooks: {exc}") from exc

        if response.status_code == 401:
            raise ConnectorAuthError("QuickBooks rejected the access token")
        if response.status_code == 429:
            raise ConnectorUnavailable("QuickBooks rate-limited this request")
        if response.status_code >= 500:
            raise ConnectorUnavailable(
                f"QuickBooks returned {response.status_code}"
            )
        if response.status_code >= 400:
            raise ConnectorUnavailable(
                f"QuickBooks returned {response.status_code}: {response.text[:300]}"
            )

        return response.json()
