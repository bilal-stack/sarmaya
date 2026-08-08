"""Purchase order business logic.

The point of this module is how little of it is new. Approval routing,
segregation of duties, delegated authority, transition guards, the hash-chained
audit trail, policy-evaluation snapshots and correlation chains are all reused
from the invoice module rather than reimplemented — a PO is simply another
record that declares WORKFLOW_TYPE and carries tenant_id, correlation_id and
current_state.

Two things are specific to buying rather than paying:

  * The permissions are separate (purchase_orders.* rather than invoices.*).
    Committing to spend and settling a bill are different authorities, and
    letting one imply the other would undo segregation of duties across the
    two modules.
  * A PO starts a correlation chain. The receipts against it and the invoice
    that settles it join that chain, which is what lets an auditor pull the
    whole story — order, delivery, bill — from one id.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.vendor import Vendor
from app.repositories.purchase_order_repository import PurchaseOrderRepository
from app.repositories.vendor_repository import VendorRepository
from app.schemas.purchase_order import PurchaseOrderCreate, PurchaseOrderUpdate
from app.core.enums import PurchaseOrderState, VendorStatus
from app.core.roles import (
    has_permission, PERM_CREATE_PO, PERM_VIEW_PO, PERM_UPDATE_PO, PERM_APPROVE_PO,
)
from app.services.workflow import transition_state
from app.services.policy import explain_approval_routing
from app.services.policy_eval import record_approval_routing_eval
from app.services.correlation import new_correlation_id
from app.services.audit import log_audit
from app.services import sod
from app.services.delegation import resolve_authority, resolve_permission
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "purchase_order"


class PurchaseOrderService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = PurchaseOrderRepository(db)
        self.vendor_repository = VendorRepository(db)

    # --- reads ---------------------------------------------------------------

    def get_order(self, po_id: UUID, current_user: dict) -> PurchaseOrder:
        self._require(current_user, PERM_VIEW_PO, "view purchase orders")
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")
        return order

    def list_orders(self, current_user: dict, **filters) -> List[PurchaseOrder]:
        self._require(current_user, PERM_VIEW_PO, "view purchase orders")
        return self.repository.list_orders(**filters)

    # --- writes --------------------------------------------------------------

    def create_order(self, data: PurchaseOrderCreate, current_user: dict) -> PurchaseOrder:
        self._require(current_user, PERM_CREATE_PO, "create purchase orders")

        vendor_id, vendor_name = self._resolve_vendor(data.vendor_id, data.vendor_name)

        order = PurchaseOrder(
            tenant_id=current_user["tenant_id"],
            po_number=self._next_po_number(),
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            order_date=data.order_date or utc_now().date(),
            expected_date=data.expected_date,
            currency=data.currency,
            description=data.description,
            current_state=PurchaseOrderState.DRAFT,
            # This order opens the transaction story its receipts and invoice
            # will join.
            correlation_id=new_correlation_id(),
            created_by=current_user["id"],
            # Totals are set before the first flush because total_amount is
            # NOT NULL; the lines below recompute them from what was ordered.
            tax_amount=data.tax_amount or Decimal("0"),
            subtotal_amount=Decimal("0"),
            total_amount=Decimal("0"),
        )
        self.repository.create(order)
        self.db.flush()

        self._replace_lines(order, data.lines, tenant_id=current_user["tenant_id"])
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="created",
            workflow_type=OBJECT_TYPE,
            after_value={
                "po_number": order.po_number,
                "vendor_name": order.vendor_name,
                "total_amount": str(order.total_amount),
                "lines": len(order.lines),
            },
        )
        self.repository.commit()
        return self.repository.refresh(order)

    def update_order(
        self, po_id: UUID, data: PurchaseOrderUpdate, current_user: dict
    ) -> PurchaseOrder:
        """Edit a draft. Once submitted the order is what was approved, so it
        is no longer editable — a changed order would make the approval, the
        receipt and the invoice refer to different things."""
        self._require(current_user, PERM_UPDATE_PO, "update purchase orders")
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")
        if self._state(order) != PurchaseOrderState.DRAFT.value:
            raise ValueError(
                f"Cannot edit a purchase order in {self._state(order)} state; "
                "only drafts can be changed."
            )

        before = {
            "vendor_name": order.vendor_name,
            "total_amount": str(order.total_amount),
        }

        if data.vendor_id is not None or data.vendor_name is not None:
            order.vendor_id, order.vendor_name = self._resolve_vendor(
                data.vendor_id or order.vendor_id, data.vendor_name
            )
        if data.expected_date is not None:
            order.expected_date = data.expected_date
        if data.description is not None:
            order.description = data.description
        if data.tax_amount is not None:
            order.tax_amount = data.tax_amount
        if data.lines is not None:
            self._replace_lines(order, data.lines, tenant_id=order.tenant_id)

        self.repository.update(order)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="updated",
            workflow_type=OBJECT_TYPE,
            before_value=before,
            after_value={
                "vendor_name": order.vendor_name,
                "total_amount": str(order.total_amount),
            },
        )
        self.repository.commit()
        return self.repository.refresh(order)

    def submit_for_approval(self, po_id: UUID, current_user: dict):
        """Send a draft for approval, snapshotting who must approve it."""
        self._require(current_user, PERM_CREATE_PO, "submit purchase orders")
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")

        if not transition_state(
            self.db, order, PurchaseOrderState.PENDING_APPROVAL.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        routing = explain_approval_routing(
            self.db, current_user["tenant_id"], float(order.total_amount or 0)
        )
        required_role = routing["required_role"]

        # The same approval matrix that routes invoices routes orders, so the
        # thresholds a tenant configures apply to both without a second matrix
        # to keep in step.
        record_approval_routing_eval(
            self.db, current_user["tenant_id"], routing,
            float(order.total_amount or 0), OBJECT_TYPE, order.id, current_user["id"],
        )

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="submitted_for_approval",
            workflow_step=self._state(order),
            workflow_type=OBJECT_TYPE,
            after_value={
                "required_role": required_role,
                "policy_name": routing["policy_name"],
                "routing_reason": routing["reason"],
            },
        )
        self.repository.commit()
        return self.repository.refresh(order), required_role

    def approve_order(self, po_id: UUID, current_user: dict) -> PurchaseOrder:
        can_approve, perm_delegation = resolve_permission(
            self.db, current_user, PERM_APPROVE_PO
        )
        if not can_approve:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "approve purchase orders"
            )

        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")

        # Segregation of duties binds the person acting, not the role they
        # borrowed — the same rule the invoice module applies.
        if sod.violates_self_approval(order, current_user):
            self._audit_block(order, current_user, "sod_self_approval")
            raise PermissionError(
                "Segregation of duties: you cannot approve a purchase order you raised."
            )

        routing = explain_approval_routing(
            self.db, current_user["tenant_id"], float(order.total_amount or 0)
        )
        required_role = routing["required_role"]
        record_approval_routing_eval(
            self.db, current_user["tenant_id"], routing,
            float(order.total_amount or 0), OBJECT_TYPE, order.id, current_user["id"],
        )

        authorised, role_delegation = resolve_authority(self.db, current_user, required_role)
        if not authorised:
            raise PermissionError(
                f"{required_role.upper()} role required to approve this purchase order"
            )
        acting_delegation = role_delegation or perm_delegation

        if not transition_state(
            self.db, order, PurchaseOrderState.APPROVED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")

        order.approved_by = current_user["id"]
        order.approved_at = utc_now()
        self.repository.update(order)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="approved",
            workflow_step=self._state(order),
            workflow_type=OBJECT_TYPE,
            before_value={"state": PurchaseOrderState.PENDING_APPROVAL.value},
            after_value={
                "state": PurchaseOrderState.APPROVED.value,
                "required_role": required_role,
                "policy_name": routing["policy_name"],
                "routing_reason": routing["reason"],
                **({
                    "acted_under_delegation": str(acting_delegation.id),
                    "delegated_authority_of": str(acting_delegation.from_user_id),
                } if acting_delegation else {}),
            },
        )
        self.repository.commit()
        return self.repository.refresh(order)

    def reject_order(self, po_id: UUID, reason: str, current_user: dict) -> PurchaseOrder:
        can_approve, _ = resolve_permission(self.db, current_user, PERM_APPROVE_PO)
        if not can_approve:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "reject purchase orders"
            )
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")

        if not transition_state(
            self.db, order, PurchaseOrderState.REJECTED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        order.rejection_reason = reason
        self.repository.update(order)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="rejected",
            workflow_step=self._state(order),
            workflow_type=OBJECT_TYPE,
            comment=reason,
        )
        self.repository.commit()
        return self.repository.refresh(order)

    def issue_order(self, po_id: UUID, current_user: dict) -> PurchaseOrder:
        """Send an approved order to the vendor.

        The point of no return: once issued, goods may arrive and a liability
        exists. The configured guard on this transition checks the vendor is
        verified, because catching that at invoice approval is far too late.
        """
        self._require(current_user, PERM_CREATE_PO, "issue purchase orders")
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")

        if not transition_state(
            self.db, order, PurchaseOrderState.ISSUED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="issued",
            workflow_step=self._state(order),
            workflow_type=OBJECT_TYPE,
            after_value={"vendor_name": order.vendor_name},
        )
        self.repository.commit()
        return self.repository.refresh(order)

    def close_order(self, po_id: UUID, current_user: dict) -> PurchaseOrder:
        self._require(current_user, PERM_CREATE_PO, "close purchase orders")
        order = self.repository.get_by_id(po_id)
        if not order:
            raise ValueError("Purchase order not found")

        if not transition_state(
            self.db, order, PurchaseOrderState.CLOSED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="closed",
            workflow_step=self._state(order),
            workflow_type=OBJECT_TYPE,
        )
        self.repository.commit()
        return self.repository.refresh(order)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _state(order: PurchaseOrder) -> str:
        return str(getattr(order.current_state, "value", order.current_state)).lower()

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _audit_block(self, order: PurchaseOrder, current_user: dict, reason: str) -> None:
        """A refused approval is recorded and committed on its own, so the
        attempt survives even though the action does not."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=order.id,
            action="approval_blocked",
            workflow_type=OBJECT_TYPE,
            comment=f"Blocked: {reason}",
            after_value={"reason": reason},
        )
        self.repository.commit()

    def _resolve_vendor(self, vendor_id: Optional[UUID], vendor_name: Optional[str]):
        """Resolve the vendor master record.

        Unlike the invoice upload path, a PO never auto-creates a vendor: you
        are choosing who to buy from, so it is a deliberate selection rather
        than something inferred from a scanned document.
        """
        if vendor_id:
            vendor = self.vendor_repository.get_by_id(vendor_id)
            if not vendor:
                raise ValueError("Vendor not found")
            return vendor.id, vendor.legal_name
        if vendor_name and vendor_name.strip():
            vendor = self.vendor_repository.get_by_legal_name(vendor_name.strip())
            if not vendor:
                raise ValueError(
                    f"No vendor named '{vendor_name.strip()}'. Create the vendor "
                    "first — a purchase order names who you are buying from."
                )
            return vendor.id, vendor.legal_name
        raise ValueError("A purchase order must name a vendor")

    def _replace_lines(self, order: PurchaseOrder, lines, tenant_id) -> None:
        """Rewrite the order's lines and recompute its totals.

        Totals are derived from the lines rather than accepted from the caller,
        so the header can never disagree with what was actually ordered — the
        three-way match compares against these numbers.
        """
        order.lines.clear()
        self.db.flush()

        subtotal = Decimal("0")
        for index, line in enumerate(lines or [], start=1):
            amount = Decimal(line.quantity) * Decimal(line.unit_price)
            subtotal += amount
            self.repository.add_line(PurchaseOrderLine(
                tenant_id=tenant_id,
                purchase_order_id=order.id,
                line_number=index,
                description=line.description,
                product_code=line.product_code,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=amount,
            ))

        tax = Decimal(order.tax_amount or 0)
        order.subtotal_amount = subtotal
        order.tax_amount = tax
        order.total_amount = subtotal + tax

    def _next_po_number(self) -> str:
        """Per-tenant sequential reference. The count is tenant-scoped by the
        ORM listener, so two tenants do not share a series."""
        existing = self.db.query(PurchaseOrder).count()
        return f"PO-{existing + 1:05d}"
