"""Delegation of approval authority.

Build Book: "Delegation supports temporary assignment of approvals and tasks
with start and end dates."

A delegation lends the delegator's *role* authority to the delegate for a
bounded window. It never changes either user's own role, and it never widens
what the delegator could do — you cannot delegate authority you do not hold.

Two safety properties matter and are enforced here:

  * **Segregation of duties still binds the person acting.** A delegate cannot
    approve an invoice they created themselves; borrowed authority does not
    make you your own checker.
  * **The trail names both parties.** An action taken under delegation records
    who acted and whose authority they used, so an auditor never sees an
    approval that looks like it came from the wrong role.
"""
import logging
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.delegation import Delegation
from app.models.user import User
from app.core.roles import ADMIN, has_permission, PERM_MANAGE_USERS
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)


def _now_naive():
    """Comparable with the DB's tz-naive DateTime columns."""
    return make_naive(to_utc(utc_now()))


def active_delegations_to(db: Session, user_id) -> List[Delegation]:
    """Delegations currently lending authority to this user."""
    if not user_id:
        return []
    now = _now_naive()
    return (
        db.query(Delegation)
        .filter(
            Delegation.to_user_id == user_id,
            Delegation.revoked_at.is_(None),
            Delegation.starts_at <= now,
            Delegation.ends_at >= now,
        )
        .all()
    )


def delegated_roles(db: Session, user_id) -> List[Tuple[str, Delegation]]:
    """(role, delegation) pairs this user can currently borrow."""
    out: List[Tuple[str, Delegation]] = []
    for d in active_delegations_to(db, user_id):
        delegator = db.query(User).filter(User.id == d.from_user_id).first()
        if delegator and delegator.is_active:
            role = getattr(delegator.role, "value", delegator.role)
            if role:
                out.append((str(role).lower(), d))
    return out


def effective_roles(db: Session, current_user: dict) -> List[Tuple[str, Optional[Delegation]]]:
    """Every role this user can currently act in: their own first, then any
    borrowed by an active delegation. The paired delegation is None for their
    own role, so callers can tell direct authority from borrowed."""
    own = (current_user.get("role") or "").strip().lower()
    roles: List[Tuple[str, Optional[Delegation]]] = [(own, None)] if own else []
    roles.extend(delegated_roles(db, current_user.get("id")))
    return roles


def resolve_permission(db: Session, current_user: dict, permission: str):
    """Can this user exercise `permission`, directly or by delegation?
    Returns (allowed, delegation_or_None)."""
    for role, delegation in effective_roles(db, current_user):
        if has_permission(role, permission):
            return True, delegation
    return False, None


def resolve_authority(db: Session, current_user: dict, required_role: str):
    """Can this user act in `required_role`, directly or by delegation?

    Returns (allowed, delegation_or_None). The delegation is returned so the
    caller can record whose authority was used.
    """
    wanted = (required_role or "").strip().lower()
    for role, delegation in effective_roles(db, current_user):
        if role == ADMIN or not wanted or role == wanted:
            return True, delegation
    return False, None


class DelegationService:
    """Create, revoke and inspect delegations."""

    def __init__(self, db: Session):
        self.db = db

    def create(
        self, current_user: dict, to_user_id, starts_at, ends_at,
        from_user_id=None, reason: Optional[str] = None,
    ) -> Delegation:
        """Delegate authority. You may delegate your own; only a user manager
        may delegate on someone else's behalf."""
        actor_id = current_user.get("id")
        from_user_id = from_user_id or actor_id

        if str(from_user_id) != str(actor_id) and not has_permission(
            current_user["role"], PERM_MANAGE_USERS
        ):
            raise PermissionError("You may only delegate your own authority")

        if str(from_user_id) == str(to_user_id):
            raise ValueError("Cannot delegate to yourself")

        starts_at, ends_at = make_naive(to_utc(starts_at)), make_naive(to_utc(ends_at))
        if ends_at <= starts_at:
            raise ValueError("ends_at must be after starts_at")

        delegate = self.db.query(User).filter(
            User.id == to_user_id, User.tenant_id == current_user["tenant_id"]
        ).first()
        if not delegate or not delegate.is_active:
            raise ValueError("Delegate not found or inactive")

        if self._overlaps(from_user_id, to_user_id, starts_at, ends_at):
            raise ValueError("An overlapping delegation already exists for this pair")

        row = Delegation(
            tenant_id=current_user["tenant_id"],
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            created_by=actor_id,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def revoke(self, delegation_id, current_user: dict) -> Delegation:
        """Withdraw a delegation early. The delegator or a user manager may."""
        row = self.db.query(Delegation).filter(Delegation.id == delegation_id).first()
        if not row:
            raise ValueError("Delegation not found")
        if str(row.from_user_id) != str(current_user.get("id")) and not has_permission(
            current_user["role"], PERM_MANAGE_USERS
        ):
            raise PermissionError("You may only revoke delegations you granted")
        if row.revoked_at is None:
            row.revoked_at = _now_naive()
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def list_for_user(self, current_user: dict, include_inactive: bool = False) -> List[Delegation]:
        """Delegations granted by or to the caller. User managers see all."""
        query = self.db.query(Delegation)
        if not has_permission(current_user["role"], PERM_MANAGE_USERS):
            uid = current_user.get("id")
            query = query.filter(
                or_(Delegation.from_user_id == uid, Delegation.to_user_id == uid)
            )
        if not include_inactive:
            now = _now_naive()
            query = query.filter(
                Delegation.revoked_at.is_(None),
                Delegation.starts_at <= now,
                Delegation.ends_at >= now,
            )
        return query.order_by(Delegation.starts_at.desc()).all()

    def _overlaps(self, from_user_id, to_user_id, starts_at, ends_at) -> bool:
        return (
            self.db.query(Delegation)
            .filter(
                Delegation.from_user_id == from_user_id,
                Delegation.to_user_id == to_user_id,
                Delegation.revoked_at.is_(None),
                Delegation.starts_at <= ends_at,
                Delegation.ends_at >= starts_at,
            )
            .first()
            is not None
        )
