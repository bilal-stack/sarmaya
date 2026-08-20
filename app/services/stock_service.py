"""The stock ledger, and the balance derived from it.

Every change to stock in this system goes through `post_movement`. Receipts,
adjustments, returns, transfers — one writer, so there is exactly one place
where the rules about stock are enforced and exactly one thing to read to know
how a balance came to be what it is.

Two rules live here, and both are the kind that are catastrophic when absent
and invisible when working:

**Stock cannot go negative.** A physical shelf cannot hold minus five things,
so a movement that would drive a balance below zero is a data error in every
case — a return of ten when three arrived, an issue of stock nobody has. Left
alone it does not fail: it produces a negative balance that quietly poisons
every valuation, reorder calculation and stock-accuracy figure downstream, and
it is discovered during a stock count months later. Refusing at the point of
posting turns a silent corruption into an error message next to the mistake.

**The balance row is locked before it is changed.** Two receipts of the same
item at the same moment both read 10, both write 15, and five units vanish
with the ledger showing both movements. A row lock makes the read-modify-write
sequential. The unique constraint on (item, location) is the second half of
that: without it, concurrent inserts create two rows that each hold part of
the stock and every later read silently picks one.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.roles import has_permission, PERM_VIEW_INVENTORY
from app.models.inventory import (
    Item, StockBalance, StockLocation, StockMovement,
    MOVEMENT_TYPES,
)
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)


class InsufficientStock(ValueError):
    """A movement would drive a balance below zero.

    Its own type rather than a bare ValueError because callers need to tell it
    apart: an adjustment posting this is a mistake to show the user, while a
    receipt posting it means something is wrong with the receipt itself.
    """


def _now():
    return make_naive(to_utc(utc_now()))


def _as_decimal(value) -> Decimal:
    """Quantities are Numeric, and mixing float into them loses precision
    silently — 0.1 + 0.2 in a stock ledger eventually shows as a discrepancy
    nobody can explain."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


class StockService:
    def __init__(self, db: Session):
        self.db = db

    # --- the single writer ---------------------------------------------------

    def post_movement(
        self,
        *,
        tenant_id: UUID,
        item_id: UUID,
        location_id: UUID,
        quantity,
        movement_type: str,
        current_user: Optional[dict] = None,
        reason_code: Optional[str] = None,
        source_type: Optional[str] = None,
        source_id: Optional[UUID] = None,
        note: Optional[str] = None,
        correlation_id: Optional[UUID] = None,
    ) -> StockMovement:
        """Record one change and update the balance it affects.

        Does not commit. Callers own their transaction, so a receipt that posts
        three movements either posts all three or none — a partially applied
        delivery is worse than a rejected one.
        """
        if movement_type not in MOVEMENT_TYPES:
            raise ValueError(
                f"{movement_type!r} is not a stock movement type. "
                f"One of: {', '.join(MOVEMENT_TYPES)}"
            )

        quantity = _as_decimal(quantity)
        if quantity == 0:
            # Not an error worth raising, but not worth a row either: a zero
            # movement is noise in a ledger people read to understand history.
            raise ValueError("A stock movement of zero changes nothing")

        item = self.db.query(Item).filter(Item.id == item_id).first()
        if not item:
            raise ValueError("Item not found")
        if not item.is_stocked:
            raise ValueError(
                f"{item.sku} is not a stocked item, so it has no balance to "
                "move. Non-stocked items can be ordered and received but are "
                "never held."
            )

        location = (
            self.db.query(StockLocation)
            .filter(StockLocation.id == location_id)
            .first()
        )
        if not location:
            raise ValueError("Stock location not found")

        balance = self._locked_balance(tenant_id, item_id, location_id)
        new_quantity = _as_decimal(balance.quantity) + quantity

        if new_quantity < 0:
            raise InsufficientStock(
                f"{item.sku} at {location.code} would go to {new_quantity}. "
                f"There are {balance.quantity} on hand and this movement is "
                f"{quantity}. Stock cannot be negative — a physical shelf "
                "cannot hold less than nothing, so this is a data error rather "
                "than a state to record."
            )

        movement = StockMovement(
            tenant_id=tenant_id,
            item_id=item_id,
            location_id=location_id,
            quantity=quantity,
            movement_type=movement_type,
            reason_code=reason_code,
            source_type=source_type,
            source_id=source_id,
            note=note,
            created_by=(current_user or {}).get("id"),
            correlation_id=correlation_id,
        )
        self.db.add(movement)

        balance.quantity = new_quantity
        balance.last_movement_at = _now()
        self.db.flush()

        return movement

    def _locked_balance(
        self, tenant_id: UUID, item_id: UUID, location_id: UUID
    ) -> StockBalance:
        """The balance row for this pair, locked for update.

        `with_for_update` is what makes the read-modify-write above safe under
        concurrency. Creating the row when it does not exist is racy by nature
        — two callers can both find nothing — which the unique constraint
        settles: the loser's insert fails rather than producing a second row.
        """
        balance = (
            self.db.query(StockBalance)
            .filter(
                StockBalance.item_id == item_id,
                StockBalance.location_id == location_id,
            )
            .with_for_update()
            .first()
        )
        if balance:
            return balance

        balance = StockBalance(
            tenant_id=tenant_id, item_id=item_id, location_id=location_id,
            quantity=Decimal("0"),
        )
        self.db.add(balance)
        self.db.flush()
        return balance

    # --- reading -------------------------------------------------------------

    def on_hand(self, item_id: UUID, location_id: Optional[UUID] = None) -> Decimal:
        """What is available. Across every location unless one is named."""
        query = self.db.query(
            func.coalesce(func.sum(StockBalance.quantity), 0)
        ).filter(StockBalance.item_id == item_id)
        if location_id:
            query = query.filter(StockBalance.location_id == location_id)
        return _as_decimal(query.scalar() or 0)

    def balances(
        self, current_user: dict, location_id: Optional[UUID] = None,
        include_zero: bool = False,
    ) -> List[Dict]:
        self._require_view(current_user)

        query = (
            self.db.query(StockBalance, Item, StockLocation)
            .join(Item, Item.id == StockBalance.item_id)
            .join(StockLocation, StockLocation.id == StockBalance.location_id)
        )
        if location_id:
            query = query.filter(StockBalance.location_id == location_id)
        if not include_zero:
            query = query.filter(StockBalance.quantity != 0)

        return [
            {
                "item_id": balance.item_id,
                "sku": item.sku,
                "name": item.name,
                "uom": item.uom,
                "location_id": balance.location_id,
                "location": location.code,
                "quantity": float(balance.quantity),
                "reorder_point": (
                    float(item.reorder_point)
                    if item.reorder_point is not None else None
                ),
                "below_reorder_point": (
                    item.reorder_point is not None
                    and _as_decimal(balance.quantity) < _as_decimal(item.reorder_point)
                ),
                "value": (
                    float(_as_decimal(balance.quantity) * _as_decimal(item.standard_cost))
                    if item.standard_cost is not None else None
                ),
                "last_movement_at": balance.last_movement_at,
            }
            for balance, item, location in query
                .order_by(Item.sku, StockLocation.code)
                .all()
        ]

    def movements(
        self, current_user: dict, item_id: Optional[UUID] = None,
        location_id: Optional[UUID] = None, limit: int = 100,
    ) -> List[StockMovement]:
        """The history. This is the answer to "why is the balance what it is",
        which is the question a stored quantity can never answer."""
        self._require_view(current_user)

        query = self.db.query(StockMovement)
        if item_id:
            query = query.filter(StockMovement.item_id == item_id)
        if location_id:
            query = query.filter(StockMovement.location_id == location_id)
        return (
            query.order_by(StockMovement.created_at.desc())
            .limit(min(limit, 500))
            .all()
        )

    # --- proving the aggregate ----------------------------------------------

    def ledger_totals(self) -> Dict[tuple, Decimal]:
        """Balances computed straight from the ledger.

        The point of comparison for `reconcile_balances`. Kept separate so the
        check reads as "sum the movements" rather than trusting the same code
        path that maintains the cache.
        """
        rows = (
            self.db.query(
                StockMovement.item_id,
                StockMovement.location_id,
                func.sum(StockMovement.quantity),
            )
            .group_by(StockMovement.item_id, StockMovement.location_id)
            .all()
        )
        return {(item_id, loc_id): _as_decimal(total) for item_id, loc_id, total in rows}

    def reconcile_balances(self) -> List[Dict]:
        """Every place the stored balance disagrees with the ledger.

        A maintained aggregate that can drift from its source without anybody
        noticing is worse than no aggregate at all, so the drift is made
        checkable. Returns the disagreements rather than fixing them: a
        mismatch means something wrote a balance without a movement, and
        silently correcting the number would erase the evidence of that.
        """
        ledger = self.ledger_totals()
        stored = {
            (b.item_id, b.location_id): _as_decimal(b.quantity)
            for b in self.db.query(StockBalance).all()
        }

        discrepancies = []
        for key in set(ledger) | set(stored):
            from_ledger = ledger.get(key, Decimal("0"))
            from_balance = stored.get(key, Decimal("0"))
            if from_ledger != from_balance:
                discrepancies.append({
                    "item_id": key[0],
                    "location_id": key[1],
                    "ledger": float(from_ledger),
                    "balance": float(from_balance),
                    "difference": float(from_balance - from_ledger),
                })
        return discrepancies

    # --- helpers -------------------------------------------------------------

    def _require_view(self, current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_VIEW_INVENTORY):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view inventory"
            )
