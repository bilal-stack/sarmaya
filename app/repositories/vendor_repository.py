from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List, Tuple
from uuid import UUID

from app.models.vendor import Vendor
from app.models.invoice import Invoice
from app.core.enums import VendorStatus


class VendorRepository:
    """Repository for Vendor database operations.

    Tenant isolation is enforced by Postgres RLS (see get_db_session), so
    queries here intentionally carry no tenant_id filter.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, vendor_id: UUID) -> Optional[Vendor]:
        return self.db.query(Vendor).filter(Vendor.id == vendor_id).first()

    def get_by_legal_name(self, legal_name: str) -> Optional[Vendor]:
        """Case-insensitive exact match on legal_name (for duplicate detection)."""
        return self.db.query(Vendor).filter(
            func.lower(Vendor.legal_name) == legal_name.strip().lower()
        ).first()

    def get_by_vendor_code(self, vendor_code: str) -> Optional[Vendor]:
        return self.db.query(Vendor).filter(
            Vendor.vendor_code == vendor_code
        ).first()

    def list_vendors(
        self,
        status_filter: Optional[VendorStatus] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Vendor], int]:
        """List vendors with optional status filter and name search.
        Returns (vendors, total_count)."""
        query = self.db.query(Vendor)

        if status_filter:
            query = query.filter(Vendor.status == status_filter)

        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                Vendor.legal_name.ilike(term) | Vendor.display_name.ilike(term)
            )

        total = query.count()
        vendors = (
            query.order_by(Vendor.legal_name.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return vendors, total

    def count_linked_invoices(self, vendor_id: UUID) -> int:
        """Number of invoices referencing this vendor (guards hard delete)."""
        return self.db.query(func.count(Invoice.id)).filter(
            Invoice.vendor_id == vendor_id
        ).scalar() or 0

    def create(self, vendor: Vendor) -> Vendor:
        self.db.add(vendor)
        self.db.flush()
        return vendor

    def update(self, vendor: Vendor) -> Vendor:
        self.db.add(vendor)
        self.db.flush()
        return vendor

    def delete(self, vendor: Vendor) -> None:
        self.db.delete(vendor)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, vendor: Vendor) -> Vendor:
        self.db.refresh(vendor)
        return vendor
