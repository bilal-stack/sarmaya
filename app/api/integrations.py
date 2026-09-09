"""The Integration Hub: connecting, and telling, a tenant's own accounting
system.

Build Book: Integration Hub. Everything here is thin — validation and
permission checks live in the services (`IntegrationService`,
`JournalPostingService`); this file's job is routing and mapping domain
exceptions to HTTP status codes.

One route is unlike every other endpoint in this codebase: the OAuth
callback carries no `Authorization` header at all, because its caller is
Intuit's own redirect, not Sarmaya's frontend. See its docstring for what
that changes.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.core.config import settings
from app.core.database import get_db
from app.services.finance_connectors.base import ConnectorAuthError, ConnectorUnavailable
from app.services.integration_posting_service import JournalPostingService
from app.services.integration_service import IntegrationService

router = APIRouter(prefix="/integrations", tags=["Integrations"])


def _raise_for(exc: Exception):
    """Refusal is 403; a dead token is 424 (Failed Dependency — the request
    failed because something it depends on, the provider's own auth, failed);
    the provider being unreachable is 502; everything else is 400. Same idiom
    InsufficientStock -> 409 already uses in app/api/inventory.py: a domain
    failure mode earns its own status rather than a generic one."""
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ConnectorAuthError):
        raise HTTPException(status_code=424, detail=str(exc))
    if isinstance(exc, ConnectorUnavailable):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --- schemas -----------------------------------------------------------

class ConnectIn(BaseModel):
    redirect_uri: Optional[str] = None


class MapVendorIn(BaseModel):
    external_party_id: str


class DefaultAccountsIn(BaseModel):
    liability_account_external_id: str
    bank_account_external_id: str


# --- connecting ----------------------------------------------------------

@router.post("/{provider}/connect")
def connect(
    provider: str,
    payload: ConnectIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Start the OAuth handshake. Returns the URL to send the browser to —
    not a redirect itself, so the frontend can navigate there with
    `window.location.href` rather than following a redirect inside a fetch,
    which the browser would just silently swallow."""
    try:
        url = IntegrationService(db).begin_connect(
            provider, current_user,
            payload.redirect_uri or settings.QBO_REDIRECT_URI,
        )
        return {"authorization_url": url}
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{provider}/callback")
def callback(
    provider: str,
    state: str = Query(...),
    code: str = Query(...),
    realmId: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Where Intuit sends the browser back to.

    No `get_current_user` dependency — this request carries no bearer token
    at all; its caller is a browser mid-navigation from Intuit's own consent
    screen. The tenant comes entirely from `state`, which
    `IntegrationService.complete_connect` parses and binds before it touches
    anything RLS-protected — see that method's docstring.

    Answers with a redirect, not JSON, for the same reason: the caller here
    cannot read a JSON body, only follow a Location header.
    """
    frontend_target = f"{settings.FRONTEND_URL}/ai-tools/system/integrations"
    try:
        IntegrationService(db).complete_connect(
            provider, state=state, code=code, company_id=realmId,
            redirect_uri=settings.QBO_REDIRECT_URI,
        )
        return RedirectResponse(f"{frontend_target}?connected={provider}")
    except Exception as exc:  # noqa: BLE001 - this route must never 500 into a blank browser tab
        message = str(exc)[:200]
        return RedirectResponse(f"{frontend_target}?error={message}")


@router.post("/{provider}/disconnect")
def disconnect(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        connection = IntegrationService(db).disconnect(provider, current_user)
        return _connection_dict(connection)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{provider}/refresh")
def refresh_reference_data(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Pull the chart of accounts and vendor/customer list again, right now.
    A wholesale replace, not a merge — see app/models/integration.py."""
    try:
        return IntegrationService(db).refresh_reference_data(provider, current_user)
    except (ValueError, PermissionError, ConnectorAuthError, ConnectorUnavailable) as e:
        _raise_for(e)


@router.get("/{provider}/status")
def get_status(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _connection_dict(IntegrationService(db).get_status(provider, current_user))
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{provider}/default-accounts")
def set_default_accounts(
    provider: str,
    payload: DefaultAccountsIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Which pulled accounts a posted entry debits and credits. Required
    before anything can post — see app/models/integration.py's note on
    default_liability_account_external_id."""
    try:
        connection = IntegrationService(db).set_default_accounts(
            provider,
            liability_account_external_id=payload.liability_account_external_id,
            bank_account_external_id=payload.bank_account_external_id,
            current_user=current_user,
        )
        return _connection_dict(connection)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- reference data --------------------------------------------------------

@router.get("/{provider}/accounts")
def list_accounts(
    provider: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            {
                "external_account_id": a.external_account_id,
                "name": a.name,
                "account_type": a.account_type,
                "account_sub_type": a.account_sub_type,
                "is_active": a.is_active,
            }
            for a in IntegrationService(db).list_accounts(provider, current_user)
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/{provider}/parties")
def list_parties(
    provider: str,
    type: Optional[str] = Query(None, alias="type"),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            {
                "external_party_id": p.external_party_id,
                "party_type": p.party_type,
                "display_name": p.display_name,
                "email": p.email,
                "is_active": p.is_active,
            }
            for p in IntegrationService(db).list_parties(provider, current_user, type)
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{provider}/vendors/{vendor_id}/map")
def map_vendor(
    provider: str,
    vendor_id: UUID,
    payload: MapVendorIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        mapping = IntegrationService(db).map_vendor(
            provider, vendor_id, payload.external_party_id, current_user,
        )
        return {
            "vendor_id": mapping.vendor_id,
            "external_party_id": mapping.external_party_id,
        }
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- the outbound queue ----------------------------------------------------

@router.get("/{provider}/posts")
def list_posts(
    provider: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [_post_dict(p) for p in JournalPostingService(db).list_posts(
            current_user, status_filter, provider, limit,
        )]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/{provider}/posts/{post_id}/retry")
def retry_post(
    provider: str,
    post_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _post_dict(JournalPostingService(db).retry(post_id, current_user))
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- rendering ---------------------------------------------------------

def _connection_dict(connection) -> dict:
    return {
        "provider": connection.provider,
        "status": connection.status,
        "external_company_id": connection.external_company_id,
        "external_company_name": connection.external_company_name,
        "connected_at": connection.connected_at,
        "last_synced_at": connection.last_synced_at,
        "last_error": connection.last_error,
        "default_liability_account_external_id": connection.default_liability_account_external_id,
        "default_bank_account_external_id": connection.default_bank_account_external_id,
        "ready_to_post": bool(
            connection.default_liability_account_external_id
            and connection.default_bank_account_external_id
        ),
    }


def _post_dict(post) -> dict:
    return {
        "id": post.id,
        "source_type": post.source_type,
        "source_id": post.source_id,
        "status": post.status,
        "attempts": post.attempts,
        "last_error": post.last_error,
        "next_attempt_at": post.next_attempt_at,
        "posted_at": post.posted_at,
        "external_transaction_id": post.external_transaction_id,
        "correlation_id": post.correlation_id,
    }
