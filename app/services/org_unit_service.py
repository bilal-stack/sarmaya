"""Org units, and resolving what a person may see.

Build Book, Access Controls: *"RBAC with scopes: tenant, business unit,
location, cost center, project."*

The interesting part is `effective_scope`. A scope row grants a unit *and
everything beneath it*, because that is what people mean by "she runs the north
region" — nobody expects to enumerate every cost centre under it, and a scope
that had to be re-listed each time a site opened would be wrong within a month.
So the answer is the transitive closure of the assigned units.

Resolved per request rather than stored. Storing the closure would make adding
a child unit a data-migration problem: every user scoped to its parent would
need updating, and the one that was missed would silently stop seeing a site.
"""
import logging
from typing import Dict, List, Optional, Set
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import has_permission, PERM_MANAGE_USERS, PERM_VIEW_USERS
from app.models.org_unit import OrgUnit, UserOrgScope, UNIT_TYPES
from app.models.user import User
from app.services.audit import log_audit

logger = logging.getLogger(__name__)

OBJECT_TYPE = "org_unit"


class OrgUnitService:
    def __init__(self, db: Session):
        self.db = db

    # --- resolving a scope ---------------------------------------------------

    def effective_scope(self, user_id: UUID) -> Optional[List[UUID]]:
        """Every unit this user may act within, or None for unrestricted.

        None rather than an empty list, and the distinction is the whole
        design: no assignment means the whole tenant, an empty list would mean
        nothing at all. Returning the wrong one of those either removes a
        control or locks everybody out.
        """
        assigned = [
            row.org_unit_id for row in
            self.db.query(UserOrgScope).filter(UserOrgScope.user_id == user_id).all()
        ]
        if not assigned:
            return None

        return sorted(self._with_descendants(assigned), key=str)

    def _with_descendants(self, roots: List[UUID]) -> Set[UUID]:
        """The assigned units plus everything under them.

        Walked in Python over the tenant's units rather than with a recursive
        CTE: an org chart is tens of rows, not thousands, and the recursive
        query would have to bypass the ORM — and therefore the tenant filter —
        to run at all.
        """
        edges: Dict[Optional[UUID], List[UUID]] = {}
        for unit_id, parent_id in self.db.query(OrgUnit.id, OrgUnit.parent_id).all():
            edges.setdefault(parent_id, []).append(unit_id)

        seen: Set[UUID] = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            if current in seen:
                continue          # also guards a cycle, which the API refuses
            seen.add(current)
            stack.extend(edges.get(current, []))
        return seen

    # --- reading -------------------------------------------------------------

    def list_units(self, current_user: dict) -> List[OrgUnit]:
        self._require(current_user, PERM_VIEW_USERS, "view the org structure")
        return (
            self.db.query(OrgUnit)
            .order_by(OrgUnit.unit_type, OrgUnit.code)
            .all()
        )

    def scopes_for(self, user_id: UUID, current_user: dict) -> List[OrgUnit]:
        self._require(current_user, PERM_VIEW_USERS, "view user scopes")
        return [
            row.org_unit for row in
            self.db.query(UserOrgScope).filter(UserOrgScope.user_id == user_id).all()
        ]

    # --- writing -------------------------------------------------------------

    def create_unit(
        self, current_user: dict, *, code: str, name: str, unit_type: str,
        parent_id: Optional[UUID] = None,
    ) -> OrgUnit:
        self._require(current_user, PERM_MANAGE_USERS, "manage the org structure")

        if unit_type not in UNIT_TYPES:
            raise ValueError(
                f"{unit_type!r} is not an org unit type. One of: {', '.join(UNIT_TYPES)}"
            )
        if self.db.query(OrgUnit).filter(OrgUnit.code == code.strip()).first():
            raise ValueError(f"A unit with code {code!r} already exists")

        parent = None
        if parent_id:
            parent = self.db.query(OrgUnit).filter(OrgUnit.id == parent_id).first()
            if not parent:
                raise ValueError("Parent unit not found")

        unit = OrgUnit(
            tenant_id=current_user["tenant_id"],
            code=code.strip(), name=name.strip(),
            unit_type=unit_type, parent_id=parent_id,
        )
        self.db.add(unit)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE, object_id=unit.id,
            action="created",
            after_value={"code": unit.code, "name": unit.name,
                         "unit_type": unit.unit_type,
                         "parent": parent.code if parent else None},
        )
        self.db.commit()
        return unit

    def assign(self, user_id: UUID, org_unit_id: UUID, current_user: dict) -> None:
        """Give a user a scope.

        Audited under the administrator who did it: narrowing or widening what
        somebody can see is an access-control change, and those are exactly the
        events an auditor asks about.
        """
        self._require(current_user, PERM_MANAGE_USERS, "assign scopes")

        target = self.db.query(User).filter(User.id == user_id).first()
        if not target:
            raise ValueError("User not found")
        unit = self.db.query(OrgUnit).filter(OrgUnit.id == org_unit_id).first()
        if not unit:
            raise ValueError("Org unit not found")

        existing = (
            self.db.query(UserOrgScope)
            .filter(
                UserOrgScope.user_id == user_id,
                UserOrgScope.org_unit_id == org_unit_id,
            )
            .first()
        )
        if existing:
            return

        self.db.add(UserOrgScope(
            tenant_id=current_user["tenant_id"],
            user_id=user_id, org_unit_id=org_unit_id,
        ))
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="user", object_id=user_id,
            action="scope_granted",
            comment=f"{target.email} may now act within {unit.code} ({unit.name}) "
                    "and everything under it.",
            after_value={"org_unit": unit.code},
        )
        self.db.commit()

    def revoke(self, user_id: UUID, org_unit_id: UUID, current_user: dict) -> None:
        """Take a scope away.

        Removing somebody's last scope widens their access to the whole tenant
        rather than narrowing it to nothing — that is what "no scope means
        unrestricted" implies, and it surprises people, so the audit entry says
        so explicitly rather than leaving it to be discovered.
        """
        self._require(current_user, PERM_MANAGE_USERS, "revoke scopes")

        row = (
            self.db.query(UserOrgScope)
            .filter(
                UserOrgScope.user_id == user_id,
                UserOrgScope.org_unit_id == org_unit_id,
            )
            .first()
        )
        if not row:
            raise ValueError("That scope is not assigned")

        unit_code = row.org_unit.code if row.org_unit else str(org_unit_id)
        self.db.delete(row)
        self.db.flush()

        remaining = (
            self.db.query(UserOrgScope)
            .filter(UserOrgScope.user_id == user_id)
            .count()
        )
        note = f"Scope {unit_code} removed."
        if remaining == 0:
            note += (
                " This was their last scope, so they can now see the whole "
                "tenant. Assign another scope if that is not intended."
            )

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="user", object_id=user_id,
            action="scope_revoked", comment=note,
            before_value={"org_unit": unit_code},
        )
        self.db.commit()

    # --- helpers -------------------------------------------------------------

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
