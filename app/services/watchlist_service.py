"""Change watchlist: tell the oversight role when the rules or the destinations move.

Build Book differentiator: *vendor bank changes, master data edits, and policy
overrides trigger real-time alerts to a watchlist role.*

All three were already audited. The audit trail answers "what happened to this
record" for somebody who has already decided to look at that record — and none
of these three give anyone a reason to look. They share the property that makes
that dangerous: each changes where money goes, or who may authorise sending it,
without touching a single invoice. Somebody watching invoices sees nothing at
all until a payment lands somewhere new, and by then it has landed.

Recipients are resolved by permission, like every other notification here, and
the roles that hold it (admin, CFO, auditor) are deliberately not the ones who
make these changes — an alert delivered to its own author is a log line.
"""
import logging
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_RECEIVE_WATCHLIST, PERM_VIEW_WATCHLIST,
)
from app.models.watchlist_alert import (
    WatchlistAlert, CATEGORY_BANK_CHANGE, CATEGORY_MASTER_DATA, CATEGORY_POLICY,
    SEVERITY_HIGH, SEVERITY_MEDIUM,
)
from app.models.user import User
from app.services.audit import log_audit
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)


class WatchlistService:
    def __init__(self, db: Session):
        self.db = db

    # --- raising -------------------------------------------------------------

    def record(
        self,
        current_user: dict,
        *,
        category: str,
        object_type: str,
        object_id: UUID,
        summary: str,
        detail: Optional[dict] = None,
        severity: str = SEVERITY_MEDIUM,
    ) -> Optional[WatchlistAlert]:
        """Raise an alert and notify the watchers.

        Never raises. A watchlist alert is a parallel observation, not a step
        in the action it describes: a failure here must not roll back a bank
        change that was correctly approved, or leave the caller thinking the
        change failed when only the telling did. It is logged instead, and the
        underlying event is still in the audit trail either way.
        """
        try:
            alert = WatchlistAlert(
                tenant_id=current_user["tenant_id"],
                category=category,
                severity=severity,
                object_type=object_type,
                object_id=object_id,
                summary=summary,
                detail=detail or {},
                actor_id=current_user.get("id"),
            )
            self.db.add(alert)
            self.db.flush()
            self._notify(alert, current_user)
            return alert
        except Exception:
            logger.exception("Failed to raise watchlist alert for %s", object_id)
            return None

    def _notify(self, alert: WatchlistAlert, current_user: dict) -> None:
        """Email the watchers, excluding whoever caused it.

        Telling somebody about their own action is noise, and noise is what
        stops people reading alerts that matter.
        """
        from app.services.notification_service import NotificationService

        recipients = [
            u.email for u in self._watchers(alert.tenant_id)
            if u.email and str(u.id) != str(current_user.get("id"))
        ]
        if not recipients:
            return
        NotificationService(self.db)._send(
            alert.tenant_id,
            recipients,
            f"Watchlist: {alert.summary}",
            f"{alert.summary}\n\n"
            f"Category: {alert.category}\n"
            f"Object: {alert.object_type} {alert.object_id}\n\n"
            "Raised because this kind of change moves money or moves the rules "
            "without touching an invoice, so nothing else would surface it.",
            category="watchlist",
        )

    def _watchers(self, tenant_id: UUID) -> List[User]:
        return [
            u for u in self.db.query(User).filter(
                User.tenant_id == tenant_id, User.is_active.is_(True)
            ).all()
            if has_permission(u.role, PERM_RECEIVE_WATCHLIST)
        ]

    # --- reading -------------------------------------------------------------

    def list_alerts(
        self,
        current_user: dict,
        *,
        open_only: bool = False,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[WatchlistAlert]:
        self._require_view(current_user)
        query = self.db.query(WatchlistAlert)
        if open_only:
            query = query.filter(WatchlistAlert.acknowledged_at.is_(None))
        if category:
            query = query.filter(WatchlistAlert.category == category)
        return query.order_by(WatchlistAlert.created_at.desc()).limit(limit).all()

    def open_count(self, current_user: dict) -> int:
        self._require_view(current_user)
        return (
            self.db.query(WatchlistAlert)
            .filter(WatchlistAlert.acknowledged_at.is_(None))
            .count()
        )

    def acknowledge(
        self, alert_id: UUID, current_user: dict, note: Optional[str] = None
    ) -> WatchlistAlert:
        """Record that somebody looked, and what they concluded."""
        self._require_view(current_user)
        alert = (
            self.db.query(WatchlistAlert)
            .filter(WatchlistAlert.id == alert_id)
            .first()
        )
        if not alert:
            raise ValueError("Alert not found")
        if alert.acknowledged_at:
            raise ValueError("This alert has already been reviewed")

        # Whoever caused the change cannot sign it off. The alert exists to put
        # a second person in front of it, and self-acknowledgement would let the
        # one action the watchlist is for clear its own flag.
        if alert.actor_id and str(alert.actor_id) == str(current_user["id"]):
            raise PermissionError(
                "You raised this change, so somebody else reviews the alert. "
                "That second look is the entire purpose of the watchlist."
            )

        alert.acknowledged_by = current_user["id"]
        alert.acknowledged_at = make_naive(to_utc(utc_now()))
        alert.acknowledgement_note = (note or "").strip() or None
        self.db.add(alert)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=alert.object_type,
            object_id=alert.object_id,
            action="watchlist_alert_acknowledged",
            comment=alert.acknowledgement_note,
            after_value={"alert_id": str(alert.id), "category": alert.category},
        )
        self.db.commit()
        return alert

    # --- helpers -------------------------------------------------------------

    def _require_view(self, current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_VIEW_WATCHLIST):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "view the change watchlist"
            )


# --- convenience wrappers, so call sites read as what they are ---------------

def alert_bank_change(db: Session, current_user: dict, vendor, change, event: str):
    """A vendor's payment destination moved, or somebody asked for it to."""
    from app.utils.masking import mask_account

    WatchlistService(db).record(
        current_user,
        category=CATEGORY_BANK_CHANGE,
        object_type="vendor_bank_change",
        object_id=change.id,
        summary=f"Bank details for {vendor.legal_name} — {event}",
        # Masked even here: the alert goes to oversight roles, and the auditor
        # among them cannot see full account numbers anywhere else.
        detail={
            "event": event,
            "vendor": vendor.legal_name,
            "old_iban": mask_account(getattr(change, "old_iban", None)),
            "new_iban": mask_account(getattr(change, "new_iban", None)),
        },
        severity=SEVERITY_HIGH,
    )


def alert_master_data(db: Session, current_user: dict, vendor, before: dict, after: dict):
    """A vendor master record was edited. Bank fields cannot come through here
    — they have their own controlled path — so this covers name, code, tax id
    and the rest, any of which can quietly redirect or disguise a payee."""
    changed = sorted(set(before) | set(after))
    if not changed:
        return
    WatchlistService(db).record(
        current_user,
        category=CATEGORY_MASTER_DATA,
        object_type="vendor",
        object_id=vendor.id,
        summary=f"{vendor.legal_name}: {', '.join(changed)} changed",
        detail={"before": _stringify(before), "after": _stringify(after)},
    )


def alert_policy_change(db: Session, current_user: dict, policy_id, name: str,
                        event: str, before=None, after=None):
    """An approval policy moved. This is the rule deciding who may approve what
    — editing it is how an amount threshold quietly stops applying."""
    WatchlistService(db).record(
        current_user,
        category=CATEGORY_POLICY,
        object_type="approval_policy",
        object_id=policy_id,
        summary=f"Approval policy '{name}' {event}",
        detail={"event": event, "before": _stringify(before or {}),
                "after": _stringify(after or {})},
        severity=SEVERITY_HIGH,
    )


def _stringify(values: dict) -> dict:
    """JSONB will not take a UUID, a Decimal or an Enum, and a watchlist alert
    that fails to serialise is one nobody is told about."""
    return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
            for k, v in (values or {}).items()}
