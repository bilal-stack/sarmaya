"""The item master and the places stock is kept.

Reference data, so this is deliberately thin — the interesting behaviour lives
in the ledger and the adjustment workflow, not here. Two things are worth more
than the CRUD around them:

**Items are withdrawn, never deleted.** A discontinued item still appears in
last year's receipts, adjustments and returns, and removing the row would leave
that history pointing at nothing. The soft-delete guard in the database refuses
a hard delete outright, so this is enforced rather than remembered.

**A SKU is how people refer to a thing out loud**, so two items sharing one
inside a tenant makes every conversation and every report ambiguous. Enforced
by a unique constraint rather than a check here, because a check here loses to
two concurrent requests.
"""
import logging
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_MANAGE_ITEMS, PERM_VIEW_INVENTORY,
)
from app.models.inventory import Item, StockLocation
from app.services.audit import log_audit

logger = logging.getLogger(__name__)


class ItemCatalogService:
    def __init__(self, db: Session):
        self.db = db

    # --- items ---------------------------------------------------------------

    def list_items(
        self, current_user: dict, active_only: bool = True,
        category: Optional[str] = None,
    ) -> List[Item]:
        self._require(current_user, PERM_VIEW_INVENTORY, "view items")

        query = self.db.query(Item)
        if active_only:
            query = query.filter(Item.is_active.is_(True))
        if category:
            query = query.filter(Item.category == category)
        return query.order_by(Item.sku).all()

    def create_item(
        self, current_user: dict, *, sku: str, name: str,
        uom: str = "each", category: Optional[str] = None,
        description: Optional[str] = None, is_stocked: bool = True,
        reorder_point: Optional[Decimal] = None,
        standard_cost: Optional[Decimal] = None,
    ) -> Item:
        self._require(current_user, PERM_MANAGE_ITEMS, "manage the item master")

        sku = (sku or "").strip()
        if not sku:
            raise ValueError("An item needs a SKU")
        if not (name or "").strip():
            raise ValueError("An item needs a name")

        if self.db.query(Item).filter(Item.sku == sku).first():
            raise ValueError(f"An item with SKU {sku!r} already exists")

        if reorder_point is not None and Decimal(reorder_point) < 0:
            raise ValueError("A reorder point cannot be negative")
        if standard_cost is not None and Decimal(standard_cost) < 0:
            raise ValueError("A standard cost cannot be negative")
        if reorder_point is not None and not is_stocked:
            raise ValueError(
                "A non-stocked item is never held, so a reorder point would "
                "never be reached and would read as configured when it is not"
            )

        item = Item(
            tenant_id=current_user["tenant_id"],
            sku=sku, name=name.strip(), uom=(uom or "each").strip(),
            category=category, description=description,
            is_stocked=is_stocked,
            reorder_point=reorder_point, standard_cost=standard_cost,
        )
        self.db.add(item)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="item", object_id=item.id,
            action="created",
            after_value={
                "sku": item.sku, "name": item.name,
                "is_stocked": item.is_stocked,
                "standard_cost": float(standard_cost) if standard_cost else None,
            },
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    def update_item(
        self, item_id: UUID, current_user: dict, **changes
    ) -> Item:
        """Change an item, with the before/after on the trail.

        Standard cost is the field worth watching: it decides the value of
        every adjustment, and therefore which of them need a second approver.
        The diff is recorded for that reason.
        """
        self._require(current_user, PERM_MANAGE_ITEMS, "manage the item master")

        item = self.db.query(Item).filter(Item.id == item_id).first()
        if not item:
            raise ValueError("Item not found")

        editable = {
            "name", "description", "category", "uom", "reorder_point",
            "standard_cost", "is_active",
        }
        before, after = {}, {}
        for field, value in changes.items():
            if field not in editable or value is None:
                continue
            current = getattr(item, field)
            if current == value:
                continue
            before[field] = float(current) if isinstance(current, Decimal) else current
            after[field] = float(value) if isinstance(value, Decimal) else value
            setattr(item, field, value)

        if not after:
            return item

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="item", object_id=item.id,
            action="updated", before_value=before, after_value=after,
        )
        self.db.commit()
        self.db.refresh(item)
        return item

    # --- locations -----------------------------------------------------------

    def list_locations(
        self, current_user: dict, active_only: bool = True
    ) -> List[StockLocation]:
        self._require(current_user, PERM_VIEW_INVENTORY, "view stock locations")

        query = self.db.query(StockLocation)
        if active_only:
            query = query.filter(StockLocation.is_active.is_(True))
        return query.order_by(StockLocation.code).all()

    def create_location(
        self, current_user: dict, *, code: str, name: str,
        org_unit_id: Optional[UUID] = None,
        is_receiving_bay: bool = False, is_quarantine: bool = False,
    ) -> StockLocation:
        self._require(current_user, PERM_MANAGE_ITEMS, "manage stock locations")

        code = (code or "").strip()
        if not code:
            raise ValueError("A location needs a code")
        if not (name or "").strip():
            raise ValueError("A location needs a name")
        if is_receiving_bay and is_quarantine:
            raise ValueError(
                "A location cannot be both the receiving bay and quarantine: "
                "goods would be quarantined into the place they arrive"
            )
        if self.db.query(StockLocation).filter(StockLocation.code == code).first():
            raise ValueError(f"A location with code {code!r} already exists")

        location = StockLocation(
            tenant_id=current_user["tenant_id"],
            code=code, name=name.strip(), org_unit_id=org_unit_id,
            is_receiving_bay=is_receiving_bay, is_quarantine=is_quarantine,
        )
        self.db.add(location)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="stock_location",
            object_id=location.id, action="created",
            after_value={
                "code": location.code, "name": location.name,
                "is_receiving_bay": is_receiving_bay,
                "is_quarantine": is_quarantine,
            },
        )
        self.db.commit()
        self.db.refresh(location)
        return location

    # --- helpers -------------------------------------------------------------

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
