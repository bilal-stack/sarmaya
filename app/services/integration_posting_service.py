"""Telling a tenant's own accounting system that money moved.

Build Book: Integration Hub — the outbound half. `IntegrationService` handles
connecting and pulling; this is enqueueing and draining.

Mirrors `NotificationDispatcher` deliberately, down to the backoff table —
this is the same shape (write in the source transaction, drain on a
schedule, back off, give up after `MAX_ATTEMPTS`) applied to a different
payload. One thing is different, and it matters: a dead *connection* should
stop an entire drain run from hammering it, not just the one post that
discovered it was dead — see `drain`'s handling of `ConnectorAuthError`.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_MANAGE_INTEGRATIONS, PERM_VIEW_INTEGRATIONS,
)
from app.models.integration import (
    IntegrationConnection, IntegrationJournalPost, IntegrationVendorMapping,
    MAX_ATTEMPTS, POST_STATUS_FAILED, POST_STATUS_PENDING, POST_STATUS_POSTED,
    SOURCE_EXPENSE_REIMBURSEMENT, SOURCE_PAYMENT, STATUS_CONNECTED, STATUS_EXPIRED,
)
from app.services.audit import log_audit
from app.services.finance_connectors.base import (
    ConnectorAuthError, ConnectorUnavailable, JournalEntryLine,
    JournalEntryRequest,
)
from app.services.integration_service import IntegrationService, connector_for
from app.utils.datetime_helpers import make_naive, to_utc, utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "integration_journal_post"

#: Minutes to wait before attempt n+1 — copied verbatim from
#: notification_dispatcher.py's own table, same reasoning: short at first
#: because most failures are the provider being briefly unreachable, longer
#: after because a post still failing on the fourth try is not going to
#: succeed on the fifth either.
BACKOFF_MINUTES = {1: 1, 2: 5, 3: 15, 4: 60}


def _now():
    return make_naive(to_utc(utc_now()))


class JournalPostingService:
    def __init__(self, db: Session):
        self.db = db

    # --- enqueueing --------------------------------------------------------

    def enqueue(
        self, source_type: str, source_record, current_user: dict
    ) -> Optional[IntegrationJournalPost]:
        """Queue a post for a record whose money has already moved.

        Called inside the caller's own transaction — no commit here. If the
        approval this is part of rolls back, nothing was ever queued; if it
        commits, this row commits with it. That property, not any particular
        retry behaviour, is the actual point of an outbox.

        Returns None, quietly, whenever posting is not currently possible:
        no connection, a connection that is not `connected`, or one that has
        not had its posting accounts configured yet. A tenant who never
        connected QuickBooks — which today is every tenant — must not have
        their payments blocked, warned, or even slowed by this.
        """
        connection = (
            self.db.query(IntegrationConnection)
            .filter(
                IntegrationConnection.provider == "quickbooks",
                IntegrationConnection.status == STATUS_CONNECTED,
            )
            .first()
        )
        if connection is None:
            return None
        if not (
            connection.default_liability_account_external_id
            and connection.default_bank_account_external_id
        ):
            return None

        request = self._build_request(source_type, source_record, connection)
        if request is None:
            return None

        post = IntegrationJournalPost(
            tenant_id=current_user["tenant_id"],
            connection_id=connection.id,
            source_type=source_type,
            source_id=source_record.id,
            correlation_id=getattr(source_record, "correlation_id", None),
            payload=_request_to_json(request),
            status=POST_STATUS_PENDING,
            next_attempt_at=_now(),
            created_by=current_user.get("id"),
        )
        # A SAVEPOINT, not the session's own transaction: enqueue() runs
        # inside release_payment's/mark_paid's transaction, before their
        # final commit. A bare self.db.rollback() here would discard
        # everything the caller had already done this transaction — the
        # state change, the audit entries, the settled invoices — over a
        # problem that has nothing to do with the payment. Only the nested
        # savepoint needs to unwind when the insert conflicts.
        try:
            with self.db.begin_nested():
                self.db.add(post)
                self.db.flush()
        except Exception:
            # The unique constraint on (connection_id, source_type,
            # source_id) is the belt-and-braces floor described in
            # app/models/integration.py — this record already has a queued
            # post, most likely because the state guard that is supposed to
            # make release_payment/mark_paid reachable once per record had a
            # bug. Either way, a second post must not be created, and the
            # caller's transaction carries on rather than being aborted.
            logger.warning(
                "A journal post for %s %s already exists; not enqueueing "
                "a second one.", source_type, source_record.id,
            )
            return None

        return post

    def _build_request(
        self, source_type: str, source_record, connection: IntegrationConnection,
    ) -> Optional[JournalEntryRequest]:
        if source_type == SOURCE_PAYMENT:
            return self._payment_request(source_record, connection)
        if source_type == SOURCE_EXPENSE_REIMBURSEMENT:
            return self._expense_request(source_record, connection)
        raise ValueError(f"{source_type!r} is not a postable source type")

    def _payment_request(self, payment, connection) -> JournalEntryRequest:
        lines = []
        for line in payment.lines:
            party_id = self._mapped_party(connection, getattr(line, "vendor_id", None))
            lines.append(JournalEntryLine(
                account_external_id=connection.default_liability_account_external_id,
                amount=Decimal(line.amount),
                direction="debit",
                party_external_id=party_id,
                description=f"{payment.payment_number}: {line.vendor_name}",
            ))
        lines.append(JournalEntryLine(
            account_external_id=connection.default_bank_account_external_id,
            amount=Decimal(payment.total_amount),
            direction="credit",
            description=f"Payment run {payment.payment_number}",
        ))
        return JournalEntryRequest(
            reference_number=str(payment.id),
            entry_date=payment.payment_date or date.today(),
            memo=f"Sarmaya payment {payment.payment_number}",
            lines=lines,
        )

    def _expense_request(self, claim, connection) -> JournalEntryRequest:
        return JournalEntryRequest(
            reference_number=str(claim.id),
            entry_date=claim.incurred_date or date.today(),
            memo=f"Sarmaya expense claim {claim.claim_number}: {claim.category}",
            lines=[
                JournalEntryLine(
                    account_external_id=connection.default_liability_account_external_id,
                    amount=Decimal(claim.total_amount),
                    direction="debit",
                    description=claim.category,
                ),
                JournalEntryLine(
                    account_external_id=connection.default_bank_account_external_id,
                    amount=Decimal(claim.total_amount),
                    direction="credit",
                    description=f"Reimbursement {claim.claim_number}",
                ),
            ],
        )

    def _mapped_party(self, connection, vendor_id) -> Optional[str]:
        if vendor_id is None:
            return None
        mapping = (
            self.db.query(IntegrationVendorMapping)
            .filter(
                IntegrationVendorMapping.connection_id == connection.id,
                IntegrationVendorMapping.vendor_id == vendor_id,
            )
            .first()
        )
        # A missing mapping is fine — the line posts against the liability
        # account with no Entity tag rather than blocking the post. The
        # vendor mapping exists to make QuickBooks's own AP aging readable
        # per vendor, not to gate whether Sarmaya can tell it anything at all.
        return mapping.external_party_id if mapping else None

    # --- draining ------------------------------------------------------------

    def drain(self, current_user: dict, limit: int = 100) -> Dict:
        """Attempt every post that is due. Mirrors
        NotificationDispatcher.dispatch, with one addition: a connection that
        turns out to be dead stops the rest of *its* rows for this run rather
        than burning each of their attempt budgets on a problem retrying
        cannot fix.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_INTEGRATIONS):
            raise PermissionError(
                "You do not have permission to dispatch integration posts"
            )

        now = _now()
        due = (
            self.db.query(IntegrationJournalPost)
            .filter(
                IntegrationJournalPost.status == POST_STATUS_PENDING,
                IntegrationJournalPost.next_attempt_at <= now,
            )
            .order_by(IntegrationJournalPost.created_at.asc())
            .limit(limit)
            .all()
        )

        attempted = posted = failed = retrying = skipped = 0
        dead_connections: set = set()

        for post in due:
            if post.connection_id in dead_connections:
                # This connection already failed auth once this run. Their
                # next_attempt_at is left untouched rather than burning an
                # attempt on a problem retrying cannot fix — see the module
                # docstring.
                continue

            attempted += 1
            outcome = self._attempt(post)
            if outcome == "auth_error":
                dead_connections.add(post.connection_id)
                self._mark_expired(post.connection_id, post.last_error or "")
            elif outcome == POST_STATUS_POSTED:
                posted += 1
            elif outcome == POST_STATUS_FAILED:
                failed += 1
            elif outcome == "skipped":
                # Counted apart from `retrying` on purpose: nothing was tried
                # and nothing failed. Folded together, a tenant who
                # disconnected while posts were queued would log
                # "retrying=100" every five minutes — which reads as a
                # hundred failures to whoever is looking at the health page.
                skipped += 1
            else:
                retrying += 1

        self.db.commit()
        return {
            "attempted": attempted,
            "posted": posted,
            "failed": failed,
            "retrying": retrying,
            "skipped": skipped,
            "connections_marked_expired": len(dead_connections),
        }

    def _mark_expired(self, connection_id: UUID, error: str) -> None:
        connection = (
            self.db.query(IntegrationConnection)
            .filter(IntegrationConnection.id == connection_id)
            .first()
        )
        if connection is None or connection.status == STATUS_EXPIRED:
            return
        connection.status = STATUS_EXPIRED
        connection.last_error = (
            error or "The connection's token could not be refreshed."
        )[:500]
        log_audit(
            db=self.db, tenant_id=connection.tenant_id, user_id=None,
            object_type="integration_connection", object_id=connection.id,
            action="token_refresh_failed",
            comment="Marked expired during the posting drain; reconnect to resume.",
        )

    def _attempt(self, post: IntegrationJournalPost) -> str:
        connection = (
            self.db.query(IntegrationConnection)
            .filter(IntegrationConnection.id == post.connection_id)
            .first()
        )
        if connection is None or connection.status != STATUS_CONNECTED:
            # Disconnected since this was queued. Not a failure of the post
            # itself — leave it pending; if the tenant reconnects, drain
            # picks it back up. If they never do, it simply waits, visible
            # in the sync-failures-adjacent "pending" view rather than
            # silently discarded.
            return "skipped"

        request = _request_from_json(post.payload)
        connector = connector_for(connection.provider)

        try:
            tokens = IntegrationService(self.db).refresh_tokens_if_needed(connection)
        except ConnectorAuthError as exc:
            post.last_error = str(exc)[:500]
            return "auth_error"

        post.attempts += 1
        post.last_attempt_at = _now()

        try:
            result = connector.post_journal_entry(
                tokens, connection.external_company_id or "", request,
            )
        except ConnectorAuthError as exc:
            post.last_error = str(exc)[:500]
            return "auth_error"
        except ConnectorUnavailable as exc:
            return self._reschedule(post, str(exc))
        except Exception as exc:  # noqa: BLE001 - a provider bug must not crash the drain
            return self._reschedule(post, f"{type(exc).__name__}: {exc}")

        post.status = POST_STATUS_POSTED
        post.posted_at = _now()
        post.external_transaction_id = result.external_transaction_id
        post.external_transaction_type = result.external_transaction_type

        log_audit(
            db=self.db, tenant_id=post.tenant_id, user_id=None,
            object_type=OBJECT_TYPE, object_id=post.id, action="posted",
            after_value={
                "external_transaction_id": result.external_transaction_id,
                "source_type": post.source_type,
            },
        )
        return POST_STATUS_POSTED

    def _reschedule(self, post: IntegrationJournalPost, error: str) -> str:
        post.last_error = error[:500]
        if post.attempts >= MAX_ATTEMPTS:
            post.status = POST_STATUS_FAILED
            log_audit(
                db=self.db, tenant_id=post.tenant_id, user_id=None,
                object_type=OBJECT_TYPE, object_id=post.id, action="post_failed",
                comment=f"Gave up after {post.attempts} attempts: {error[:300]}",
            )
            return POST_STATUS_FAILED

        post.next_attempt_at = _now() + timedelta(
            minutes=BACKOFF_MINUTES.get(post.attempts, 60)
        )
        return "retrying"

    def retry(self, post_id: UUID, current_user: dict) -> IntegrationJournalPost:
        """Put a given-up post back in front of the next drain.

        `attempts` is not reset — the historical count survives, so an
        auditor asking "how many times did this fail before somebody looked"
        can still be answered after the retry succeeds.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_INTEGRATIONS):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "retry integration posts"
            )

        post = (
            self.db.query(IntegrationJournalPost)
            .filter(IntegrationJournalPost.id == post_id)
            .first()
        )
        if not post:
            raise ValueError("Post not found")
        if post.status != POST_STATUS_FAILED:
            raise ValueError(
                f"Only a failed post can be retried; this is {post.status}"
            )

        post.status = POST_STATUS_PENDING
        post.next_attempt_at = _now()
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=post.id, action="post_retried",
        )
        self.db.commit()
        self.db.refresh(post)
        return post

    # --- reading -------------------------------------------------------------

    def list_posts(
        self, current_user: dict, status: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        if not has_permission(current_user["role"], PERM_VIEW_INTEGRATIONS):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view integration posts"
            )
        query = self.db.query(IntegrationJournalPost)
        if status:
            query = query.filter(IntegrationJournalPost.status == status)
        if provider:
            # The route is /integrations/{provider}/posts, so the answer has to
            # actually be that provider's. Unfiltered, a tenant on two
            # providers would see the same list under both — each one quietly
            # claiming the other's failures as its own.
            query = query.join(
                IntegrationConnection,
                IntegrationJournalPost.connection_id == IntegrationConnection.id,
            ).filter(IntegrationConnection.provider == provider)
        return query.order_by(IntegrationJournalPost.created_at.desc()).all()


def _request_to_json(request: JournalEntryRequest) -> dict:
    return {
        "reference_number": request.reference_number,
        "entry_date": request.entry_date.isoformat(),
        "memo": request.memo,
        "lines": [
            {
                "account_external_id": line.account_external_id,
                "amount": str(line.amount),
                "direction": line.direction,
                "party_external_id": line.party_external_id,
                "description": line.description,
            }
            for line in request.lines
        ],
    }


def _request_from_json(payload: dict) -> JournalEntryRequest:
    return JournalEntryRequest(
        reference_number=payload["reference_number"],
        entry_date=date.fromisoformat(payload["entry_date"]),
        memo=payload["memo"],
        lines=[
            JournalEntryLine(
                account_external_id=line["account_external_id"],
                amount=Decimal(line["amount"]),
                direction=line["direction"],
                party_external_id=line.get("party_external_id"),
                description=line.get("description"),
            )
            for line in payload["lines"]
        ],
    )
