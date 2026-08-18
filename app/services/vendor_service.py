from sqlalchemy.orm import Session
from typing import Optional, List, Tuple, Dict, Any
from uuid import UUID

from app.repositories.vendor_repository import VendorRepository
from app.models.vendor import Vendor
from app.schemas.vendor import VendorCreate, VendorUpdate
from app.core.enums import VendorStatus
from app.services.audit import log_audit
from app.services.soft_delete import withdraw
from app.services.watchlist_service import alert_master_data
from app.services import sod
from app.core.roles import has_permission, PERM_MANAGE_VENDORS, PERM_VIEW_VENDORS


class VendorService:
    """Service layer for vendor master business logic."""

    def __init__(self, db: Session):
        self.db = db
        self.repository = VendorRepository(db)

    # --- read ---------------------------------------------------------------

    def list_vendors(
        self,
        current_user: dict,
        status_filter: Optional[VendorStatus] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[Vendor], int]:
        self._require_view(current_user)
        return self.repository.list_vendors(
            status_filter=status_filter, search=search, limit=limit, offset=offset
        )

    def get_vendor(self, vendor_id: UUID, current_user: dict) -> Vendor:
        self._require_view(current_user)
        vendor = self.repository.get_by_id(vendor_id)
        if not vendor:
            raise ValueError("Vendor not found")
        return vendor

    def get_review_queue(
        self, current_user: dict, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Vendor-centric triage list for the governance gate: vendors awaiting
        verification (or blocked) together with the count and value of the
        pending-approval invoices each is holding up, highest-impact first."""
        self._require_view(current_user)
        return [
            {
                "id": vendor.id,
                "legal_name": vendor.legal_name,
                "display_name": vendor.display_name,
                "vendor_code": vendor.vendor_code,
                "email": vendor.email,
                "status": vendor.status,
                "created_at": vendor.created_at,
                "blocked_invoice_count": count,
                "blocked_total_amount": total,
            }
            for vendor, count, total in self.repository.get_review_queue(limit)
        ]

    # --- write --------------------------------------------------------------

    def create_vendor(self, data: VendorCreate, current_user: dict) -> Vendor:
        self._require_manage(current_user)

        if self.repository.get_by_legal_name(data.legal_name):
            raise ValueError("A vendor with this legal name already exists")

        if data.vendor_code and self.repository.get_by_vendor_code(data.vendor_code):
            raise ValueError("A vendor with this vendor code already exists")

        vendor = Vendor(
            tenant_id=current_user["tenant_id"],
            created_by=current_user["id"],
            **data.model_dump(),
        )
        vendor = self.repository.create(vendor)
        # Flush, do not commit: the audit entry below belongs to the same
        # transaction as the record it describes.
        self.db.flush()
        vendor = self.repository.refresh(vendor)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=vendor.id,
            action="created",
            after_value={"legal_name": vendor.legal_name, "status": vendor.status.value},
        )
        self.repository.commit()
        return vendor

    def update_vendor(
        self, vendor_id: UUID, data: VendorUpdate, current_user: dict
    ) -> Vendor:
        self._require_manage(current_user)

        vendor = self.repository.get_by_id(vendor_id)
        if not vendor:
            raise ValueError("Vendor not found")

        changes = data.model_dump(exclude_unset=True)

        # Bank details do not change here. They used to: this endpoint wrote
        # them directly behind vendors.manage, which the AP clerk holds, and
        # the audit entry below recorded only the legal name — so redirecting a
        # vendor's payments was both uncontrolled and invisible. They now go
        # through VendorBankService, where a second person approves and a
        # cooling period runs before any payment may use the new account.
        from app.services.vendor_bank_service import BANK_FIELDS

        attempted = [f for f in BANK_FIELDS if f in changes]
        if attempted:
            raise ValueError(
                "Bank details cannot be edited directly ("
                + ", ".join(attempted)
                + "). Raise a change request instead: POST /vendors/{id}"
                "/bank-change. It needs a second person's approval and a "
                "cooling period, because redirecting a vendor's payments is "
                "the most common invoice fraud there is."
            )

        # Guard uniqueness if identifying fields change.
        new_name = changes.get("legal_name")
        if new_name and new_name.lower() != (vendor.legal_name or "").lower():
            if self.repository.get_by_legal_name(new_name):
                raise ValueError("A vendor with this legal name already exists")

        new_code = changes.get("vendor_code")
        if new_code and new_code != vendor.vendor_code:
            if self.repository.get_by_vendor_code(new_code):
                raise ValueError("A vendor with this vendor code already exists")

        # Every changed field, not just the name: an audit entry that records
        # one field of an update tells a reader nothing about the others.
        before = {field: getattr(vendor, field, None) for field in changes}
        for field, value in changes.items():
            setattr(vendor, field, value)

        vendor = self.repository.update(vendor)
        self.db.flush()
        vendor = self.repository.refresh(vendor)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=vendor.id,
            action="updated",
            before_value=before,
            after_value={field: getattr(vendor, field, None) for field in changes},
        )
        alert_master_data(
            self.db, current_user, vendor, before,
            {field: getattr(vendor, field, None) for field in changes},
        )
        self.repository.commit()
        return vendor

    def set_status(
        self, vendor_id: UUID, new_status: VendorStatus, current_user: dict
    ) -> Vendor:
        self._require_manage(current_user)

        vendor = self.repository.get_by_id(vendor_id)
        if not vendor:
            raise ValueError("Vendor not found")

        # Segregation of duties: you cannot activate a vendor you created.
        if new_status == VendorStatus.ACTIVE and sod.violates_self_vendor_activation(vendor, current_user):
            log_audit(
                db=self.db,
                tenant_id=current_user["tenant_id"],
                user_id=current_user["id"],
                object_type="vendor",
                object_id=vendor.id,
                action="vendor_activation_blocked",
                comment="Blocked: sod_self_activation",
                after_value={"reason": "sod_self_activation"},
            )
            self.repository.commit()
            raise PermissionError(
                "Segregation of duties: you cannot activate a vendor you created."
            )

        old_status = vendor.status
        vendor.status = new_status
        vendor = self.repository.update(vendor)
        # Flush, do not commit: verifying a vendor is what unblocks payment
        # against it, so the change and the record of who made it must land
        # together.
        self.db.flush()
        vendor = self.repository.refresh(vendor)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=vendor.id,
            action="status_changed",
            before_value={"status": old_status.value if hasattr(old_status, "value") else old_status},
            after_value={"status": new_status.value},
        )
        self.repository.commit()
        return vendor

    def delete_vendor(self, vendor_id: UUID, current_user: dict, reason: str) -> None:
        """Withdraw a vendor. Blocked if any invoice references it — callers
        should deactivate (set_status INACTIVE) instead to preserve history.

        The row is kept. It used to be destroyed, which left the audit entry
        for the deletion pointing at an id nothing resolved, and orphaned every
        bank change recorded against the vendor — the history of who moved that
        account, surviving with nothing to attach it to.
        """
        self._require_manage(current_user)

        vendor = self.repository.get_by_id(vendor_id)
        if not vendor:
            raise ValueError("Vendor not found")

        if self.repository.count_linked_invoices(vendor_id) > 0:
            raise ValueError(
                "Cannot delete a vendor with linked invoices; deactivate it instead"
            )

        withdraw(
            self.db, vendor, current_user, reason,
            object_type="vendor",
            before_value={"legal_name": vendor.legal_name,
                          "status": getattr(vendor.status, "value", vendor.status)},
        )
        self.repository.commit()

    # --- permission helpers -------------------------------------------------

    @staticmethod
    def _require_manage(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_MANAGE_VENDORS):
            raise PermissionError("You do not have permission to manage vendors")

    @staticmethod
    def _require_view(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_VIEW_VENDORS):
            raise PermissionError("You do not have permission to view vendors")
