"""Connecting, and disconnecting, a tenant's own accounting system.

Build Book: Integration Hub. This is the connect/disconnect/pull half; the
outbound posting queue is `integration_posting_service.py`.

**Resolution is per tenant, per call — never a global setting.** This is the
specific thing `app/services/ai/__init__.py`'s `get_ai_provider()` gets wrong
for this feature's purposes (one `settings.AI_PROVIDER` for the whole
deployment): `_connector_for` reads `connection.provider`, a column on a row
scoped to one tenant, so two tenants can be on different providers — or no
provider at all — without touching a setting.

**The OAuth callback route has no tenant.** It is an unauthenticated redirect
from Intuit's own servers, carrying only what Sarmaya put into the `state`
value when the flow began. Every other unauthenticated-then-becomes-tenant-
scoped code path in this app (`get_current_user` reading a JWT's `tenant_id`
claim, `/auth/login` reading a `tenant` slug query param) resolves the tenant
from something the caller supplies *before* touching any RLS-protected table
— reading `integration_connections` (or `users`, or anything else
tenant-scoped) with no tenant bound returns zero rows under this app's RLS
policies, not every tenant's rows, because the non-BYPASSRLS `os_app` role has
no rows to fall back to (see `get_current_user`'s own comment on exactly this).
So `state` here is `"{tenant_id}:{random_token}"` — self-describing on
purpose, so the tenant can be bound *before* the one query that needs it,
rather than needing a privileged bypass connection to look it up blind. The
random half still does the actual CSRF-prevention work; the tenant id in
front of it is not a secret and grants nothing by itself.
"""
import logging
import secrets
from datetime import timedelta
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.database import set_tenant_context
from app.core.mfa import decrypt_secret, encrypt_secret
from app.core.roles import has_permission, PERM_MANAGE_INTEGRATIONS, PERM_VIEW_INTEGRATIONS
from app.models.integration import (
    IntegrationAccountSnapshot, IntegrationConnection, IntegrationPartySnapshot,
    IntegrationVendorMapping, OAUTH_STATE_TTL_MINUTES, STATUS_CONNECTED,
    STATUS_EXPIRED, STATUS_NOT_CONNECTED,
)
from app.models.tenant import Tenant
from app.models.vendor import Vendor
from app.services.audit import log_audit
from app.services.finance_connectors.base import (
    ConnectorAuthError, FinanceConnector, OAuthTokens,
)
from app.services.finance_connectors.quickbooks import QuickBooksConnector
from app.utils.datetime_helpers import make_naive, to_utc, utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "integration_connection"

#: Every provider this build knows how to talk to. Adding a second means
#: adding a value here and a class beside quickbooks.py — nothing else in
#: this file changes, which is the point of resolving per-connection instead
#: of from a setting.
_CONNECTORS: Dict[str, type] = {
    "quickbooks": QuickBooksConnector,
}


def _now():
    """Naive UTC, matching the columns. See DR-011."""
    return make_naive(to_utc(utc_now()))


def connector_for(provider: str) -> FinanceConnector:
    if provider not in _CONNECTORS:
        raise ValueError(
            f"{provider!r} is not a supported provider. One of: "
            f"{', '.join(_CONNECTORS)}"
        )
    return _CONNECTORS[provider]()


class IntegrationService:
    def __init__(self, db: Session):
        self.db = db

    # --- connecting ------------------------------------------------------

    def begin_connect(
        self, provider: str, current_user: dict, redirect_uri: str
    ) -> str:
        """Start the OAuth handshake. Returns the URL to send the browser to.

        Reuses the tenant's existing connection row for this provider if one
        exists (a previous, abandoned attempt, or a reconnect after
        disconnecting) rather than creating a second one — the unique
        constraint on (tenant_id, provider) means a second row is not
        possible anyway, so finding-or-creating here is what turns that
        constraint into a normal flow instead of an error on every reconnect.
        """
        self._require(current_user, PERM_MANAGE_INTEGRATIONS, "connect an accounting system")
        connector = connector_for(provider)

        connection = self._find(provider)
        if connection is None:
            connection = IntegrationConnection(
                tenant_id=current_user["tenant_id"], provider=provider,
                status=STATUS_NOT_CONNECTED,
            )
            self.db.add(connection)
            self.db.flush()

        token = secrets.token_urlsafe(32)
        # Not a secret by itself — see the module docstring. It only lets the
        # callback bind the right tenant before its one query; the random
        # half is what actually prevents CSRF.
        state = f"{current_user['tenant_id']}:{token}"

        connection.oauth_state = state
        connection.oauth_state_expires_at = _now() + timedelta(
            minutes=OAUTH_STATE_TTL_MINUTES
        )
        connection.oauth_initiated_by = current_user["id"]
        self.db.commit()

        return connector.authorization_url(state, redirect_uri)

    def complete_connect(
        self, provider: str, state: str, code: str,
        company_id: Optional[str], redirect_uri: str,
    ) -> IntegrationConnection:
        """Finish the handshake. Called from the unauthenticated callback —
        `current_user` does not exist yet; the tenant comes entirely from
        `state`, and `oauth_initiated_by` (captured in `begin_connect`)
        stands in for it in the audit trail.
        """
        tenant_id = self._tenant_from_state(state)
        set_tenant_context(self.db, str(tenant_id))

        connection = self._find(provider)
        if (
            connection is None
            or connection.oauth_state != state
            or connection.oauth_state_expires_at is None
            or connection.oauth_state_expires_at < _now()
        ):
            raise ValueError(
                "This connection request has expired or was already used. "
                "Start connecting again."
            )

        connector = connector_for(provider)
        try:
            tokens = connector.exchange_code(code, redirect_uri, company_id)
        except ConnectorAuthError as exc:
            connection.last_error = str(exc)[:500]
            self.db.commit()
            raise

        initiated_by = connection.oauth_initiated_by

        connection.access_token_encrypted = encrypt_secret(tokens.access_token)
        connection.refresh_token_encrypted = encrypt_secret(tokens.refresh_token)
        connection.token_expires_at = make_naive(tokens.expires_at)
        connection.refresh_token_expires_at = (
            make_naive(tokens.refresh_token_expires_at)
            if tokens.refresh_token_expires_at else None
        )
        connection.external_company_id = company_id
        connection.status = STATUS_CONNECTED
        connection.connected_by = initiated_by
        connection.connected_at = _now()
        connection.last_error = None
        # Single-use: cleared immediately, so a replayed callback with the
        # same state finds nothing to match against.
        connection.oauth_state = None
        connection.oauth_state_expires_at = None

        self.db.flush()
        self._fetch_company_name(connection, connector, tokens, company_id)

        log_audit(
            db=self.db, tenant_id=tenant_id, user_id=initiated_by,
            object_type=OBJECT_TYPE, object_id=connection.id, action="connected",
            # The company name, never a token — see the module-level note this
            # whole file follows on what an audit entry may carry.
            after_value={"provider": provider, "company": connection.external_company_name},
        )
        self.db.commit()

        # Populate the reference data immediately, so the admin sees accounts
        # and vendors the moment they land back on the page rather than
        # having to know to click refresh first.
        try:
            self.refresh_reference_data(provider, current_user=None, _connection=connection)
        except Exception:
            logger.exception(
                "Initial reference-data pull failed after connecting %s for "
                "tenant %s; the connection itself is still live.",
                provider, tenant_id,
            )

        self.db.refresh(connection)
        return connection

    def _fetch_company_name(self, connection, connector, tokens, company_id) -> None:
        """Best-effort; a missing company name never blocks a connection."""
        try:
            healthy = connector.check_health(tokens, company_id or "")
            if healthy and not connection.external_company_name:
                connection.external_company_name = company_id
        except Exception:
            pass

    def _tenant_from_state(self, state: str) -> UUID:
        try:
            raw_tenant_id, _token = state.split(":", 1)
            tenant_id = UUID(raw_tenant_id)
        except (ValueError, AttributeError):
            raise ValueError("This connection request is not valid.")

        # tenants carries no tenant_id column of its own (it IS the tenant),
        # so it has no RLS policy to satisfy — reading it needs no bound
        # context yet, same as /auth/login reading a tenant slug before it
        # can bind anything.
        if not self.db.query(Tenant).filter(Tenant.id == tenant_id).first():
            raise ValueError("This connection request is not valid.")
        return tenant_id

    def disconnect(self, provider: str, current_user: dict) -> IntegrationConnection:
        self._require(current_user, PERM_MANAGE_INTEGRATIONS, "disconnect an accounting system")
        connection = self._require_connection(provider)

        if connection.refresh_token_encrypted:
            refresh_token = decrypt_secret(connection.refresh_token_encrypted)
            if refresh_token and hasattr(connector_for(provider), "revoke"):
                # Best-effort: a QuickBooks outage must not trap the tenant
                # into a connection they can no longer remove locally.
                connector_for(provider).revoke(refresh_token)

        connection.access_token_encrypted = None
        connection.refresh_token_encrypted = None
        connection.token_expires_at = None
        connection.refresh_token_expires_at = None
        connection.status = STATUS_NOT_CONNECTED
        connection.disconnected_by = current_user["id"]
        connection.disconnected_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=connection.id, action="disconnected",
            after_value={"provider": provider},
        )
        self.db.commit()
        self.db.refresh(connection)
        return connection

    # --- keeping tokens alive ---------------------------------------------

    def refresh_tokens_if_needed(self, connection: IntegrationConnection) -> OAuthTokens:
        """The live tokens for this connection, refreshing first if the
        access token is due to expire within 5 minutes.

        Raises ConnectorAuthError, and marks the connection expired, if the
        refresh itself fails — a dead refresh token needs a human to
        reconnect, not another retry.
        """
        if connection.access_token_encrypted is None:
            raise ConnectorAuthError("This connection has no live token.")

        access_token = decrypt_secret(connection.access_token_encrypted)
        refresh_token = decrypt_secret(connection.refresh_token_encrypted)
        if access_token is None or refresh_token is None:
            # Same cause mfa.py's decrypt_secret documents: SECRET_KEY was
            # rotated since these were written. Reconnecting is the only fix.
            connection.status = STATUS_EXPIRED
            connection.last_error = "Stored credentials could not be read."
            self.db.commit()
            raise ConnectorAuthError("Stored credentials could not be read.")

        expires_soon = (
            connection.token_expires_at is None
            or connection.token_expires_at <= _now() + timedelta(minutes=5)
        )
        if not expires_soon:
            return OAuthTokens(
                access_token=access_token, refresh_token=refresh_token,
                expires_at=connection.token_expires_at,
                refresh_token_expires_at=connection.refresh_token_expires_at,
            )

        connector = connector_for(connection.provider)
        try:
            tokens = connector.refresh(refresh_token)
        except ConnectorAuthError as exc:
            connection.status = STATUS_EXPIRED
            connection.last_error = str(exc)[:500]
            log_audit(
                db=self.db, tenant_id=connection.tenant_id, user_id=None,
                object_type=OBJECT_TYPE, object_id=connection.id,
                action="token_refresh_failed",
                comment="The connection needs to be reconnected.",
            )
            self.db.commit()
            raise

        # QuickBooks rotates the refresh token on every use. Storing only the
        # new access token would leave this row holding a refresh token that
        # already stopped working, and the connection would die silently at
        # the next refresh with no warning it was ever wrong.
        connection.access_token_encrypted = encrypt_secret(tokens.access_token)
        connection.refresh_token_encrypted = encrypt_secret(tokens.refresh_token)
        connection.token_expires_at = make_naive(tokens.expires_at)
        if tokens.refresh_token_expires_at:
            connection.refresh_token_expires_at = make_naive(
                tokens.refresh_token_expires_at
            )
        self.db.commit()
        return tokens

    # --- reference data ----------------------------------------------------

    def refresh_reference_data(
        self, provider: str, current_user: Optional[dict],
        _connection: Optional[IntegrationConnection] = None,
    ) -> Dict:
        """Delete-and-replace pull of the chart of accounts and party list.

        `current_user=None` is only valid via the internal `_connection`
        path `complete_connect` uses right after connecting — every other
        caller must supply a real user and PERM_MANAGE_INTEGRATIONS.
        """
        if current_user is not None:
            self._require(current_user, PERM_MANAGE_INTEGRATIONS, "refresh account data")
            connection = self._require_connection(provider)
        else:
            connection = _connection
            if connection is None:
                raise ValueError("No connection given for an internal refresh")

        connector = connector_for(provider)
        tokens = self.refresh_tokens_if_needed(connection)
        company_id = connection.external_company_id or ""

        accounts = connector.list_accounts(tokens, company_id)
        parties = connector.list_parties(tokens, company_id)
        fetched_at = _now()

        self.db.query(IntegrationAccountSnapshot).filter(
            IntegrationAccountSnapshot.connection_id == connection.id
        ).delete(synchronize_session=False)
        self.db.query(IntegrationPartySnapshot).filter(
            IntegrationPartySnapshot.connection_id == connection.id
        ).delete(synchronize_session=False)

        for entry in accounts:
            self.db.add(IntegrationAccountSnapshot(
                tenant_id=connection.tenant_id, connection_id=connection.id,
                external_account_id=entry.external_id, name=entry.name,
                account_type=entry.account_type,
                account_sub_type=entry.account_sub_type,
                is_active=entry.is_active, fetched_at=fetched_at,
            ))
        for party in parties:
            self.db.add(IntegrationPartySnapshot(
                tenant_id=connection.tenant_id, connection_id=connection.id,
                external_party_id=party.external_id, party_type=party.party_type,
                display_name=party.display_name, email=party.email,
                is_active=party.is_active, fetched_at=fetched_at,
            ))

        connection.last_synced_at = fetched_at
        self.db.commit()

        return {
            "accounts": len(accounts),
            "vendors": sum(1 for p in parties if p.party_type == "vendor"),
            "customers": sum(1 for p in parties if p.party_type == "customer"),
        }

    def set_default_accounts(
        self, provider: str, *, liability_account_external_id: str,
        bank_account_external_id: str, current_user: dict,
    ) -> IntegrationConnection:
        """Which pulled accounts a posted entry debits and credits.

        Required before anything can actually post — see the module
        docstring on `default_liability_account_external_id`. Validated
        against the snapshot for the same reason `map_vendor` is: a typo'd
        id would otherwise fail silently, days later, at the first post.
        """
        self._require(current_user, PERM_MANAGE_INTEGRATIONS, "configure posting accounts")
        connection = self._require_connection(provider)

        for external_id in (liability_account_external_id, bank_account_external_id):
            exists = (
                self.db.query(IntegrationAccountSnapshot)
                .filter(
                    IntegrationAccountSnapshot.connection_id == connection.id,
                    IntegrationAccountSnapshot.external_account_id == external_id,
                )
                .first()
            )
            if not exists:
                raise ValueError(
                    f"Account {external_id!r} was not found in the last pull "
                    f"from {provider}. Refresh the account data and try again."
                )

        connection.default_liability_account_external_id = liability_account_external_id
        connection.default_bank_account_external_id = bank_account_external_id

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=connection.id, action="posting_accounts_set",
            after_value={
                "liability_account": liability_account_external_id,
                "bank_account": bank_account_external_id,
            },
        )
        self.db.commit()
        self.db.refresh(connection)
        return connection

    def map_vendor(
        self, provider: str, vendor_id: UUID, external_party_id: str,
        current_user: dict,
    ) -> IntegrationVendorMapping:
        self._require(current_user, PERM_MANAGE_INTEGRATIONS, "map vendors")
        connection = self._require_connection(provider)

        vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
        if not vendor:
            raise ValueError("Vendor not found")

        # Validated against the snapshot rather than trusted: a stale or
        # typo'd external id would otherwise fail silently, days later, at
        # post time — when the person who typed it has moved on.
        exists = (
            self.db.query(IntegrationPartySnapshot)
            .filter(
                IntegrationPartySnapshot.connection_id == connection.id,
                IntegrationPartySnapshot.external_party_id == external_party_id,
                IntegrationPartySnapshot.party_type == "vendor",
            )
            .first()
        )
        if not exists:
            raise ValueError(
                "That vendor id was not found in the last pull from "
                f"{provider}. Refresh the account data and try again."
            )

        mapping = (
            self.db.query(IntegrationVendorMapping)
            .filter(
                IntegrationVendorMapping.connection_id == connection.id,
                IntegrationVendorMapping.vendor_id == vendor_id,
            )
            .first()
        )
        if mapping:
            mapping.external_party_id = external_party_id
        else:
            mapping = IntegrationVendorMapping(
                tenant_id=current_user["tenant_id"], connection_id=connection.id,
                vendor_id=vendor_id, external_party_id=external_party_id,
                mapped_by=current_user["id"], mapped_at=_now(),
            )
            self.db.add(mapping)

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="integration_vendor_mapping",
            object_id=mapping.id if mapping.id else vendor_id, action="mapped",
            after_value={"vendor": vendor.legal_name, "external_party_id": external_party_id},
        )
        self.db.commit()
        self.db.refresh(mapping)
        return mapping

    # --- reading -------------------------------------------------------------

    def get_status(self, provider: str, current_user: dict) -> IntegrationConnection:
        self._require(current_user, PERM_VIEW_INTEGRATIONS, "view integration status")
        connection = self._find(provider)
        if connection is None:
            connection = IntegrationConnection(
                tenant_id=current_user["tenant_id"], provider=provider,
                status=STATUS_NOT_CONNECTED,
            )
        return connection

    def list_accounts(
        self, provider: str, current_user: dict
    ) -> List[IntegrationAccountSnapshot]:
        self._require(current_user, PERM_VIEW_INTEGRATIONS, "view chart of accounts")
        connection = self._require_connection(provider)
        return (
            self.db.query(IntegrationAccountSnapshot)
            .filter(IntegrationAccountSnapshot.connection_id == connection.id)
            .order_by(IntegrationAccountSnapshot.name)
            .all()
        )

    def list_parties(
        self, provider: str, current_user: dict, party_type: Optional[str] = None,
    ) -> List[IntegrationPartySnapshot]:
        self._require(current_user, PERM_VIEW_INTEGRATIONS, "view vendors and customers")
        connection = self._require_connection(provider)
        query = self.db.query(IntegrationPartySnapshot).filter(
            IntegrationPartySnapshot.connection_id == connection.id
        )
        if party_type:
            query = query.filter(IntegrationPartySnapshot.party_type == party_type)
        return query.order_by(IntegrationPartySnapshot.display_name).all()

    # --- helpers ---------------------------------------------------------

    def _find(self, provider: str) -> Optional[IntegrationConnection]:
        return (
            self.db.query(IntegrationConnection)
            .filter(IntegrationConnection.provider == provider)
            .first()
        )

    def _require_connection(self, provider: str) -> IntegrationConnection:
        connection = self._find(provider)
        if connection is None or connection.status == STATUS_NOT_CONNECTED:
            raise ValueError(f"Not connected to {provider}.")
        return connection

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
