"""Data access for purchase orders.

Queries are tenant-scoped by the do_orm_execute listener in core.database
whenever a tenant is bound, so filters here concern the query itself rather
than isolation.
"""
from typing import List, Optional
from uuid import UUID

from sqlalchemy import desc
from sqlalchemy.orm import Session, selectinload

from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine


class PurchaseOrderRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, po_id: UUID) -> Optional[PurchaseOrder]:
        return (
            self.db.query(PurchaseOrder)
            .options(selectinload(PurchaseOrder.lines))
            .filter(PurchaseOrder.id == po_id)
            .first()
        )

    def get_by_number(self, po_number: str) -> Optional[PurchaseOrder]:
        return (
            self.db.query(PurchaseOrder)
            .filter(PurchaseOrder.po_number == po_number)
            .first()
        )

    def list_orders(
        self,
        state: Optional[str] = None,
        vendor_id: Optional[UUID] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[PurchaseOrder]:
        query = self.db.query(PurchaseOrder).options(selectinload(PurchaseOrder.lines))
        if state:
            query = query.filter(PurchaseOrder.current_state == state)
        if vendor_id:
            query = query.filter(PurchaseOrder.vendor_id == vendor_id)
        return (
            query.order_by(desc(PurchaseOrder.created_at))
            .offset(offset)
            .limit(min(limit, 200))
            .all()
        )

    def create(self, order: PurchaseOrder) -> PurchaseOrder:
        self.db.add(order)
        return order

    def add_line(self, line: PurchaseOrderLine) -> PurchaseOrderLine:
        self.db.add(line)
        return line

    def update(self, order: PurchaseOrder) -> PurchaseOrder:
        self.db.add(order)
        return order

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, order: PurchaseOrder) -> PurchaseOrder:
        self.db.refresh(order)
        return order
