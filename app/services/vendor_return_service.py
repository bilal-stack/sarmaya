"""Returns to the vendor, and who is answerable for them.

Build Book, Variant D1: "returns management with reason codes and vendor
accountability", and the report "supplier delivery performance".

The governance content here is `vendor_attributable`. Whether a return is the
supplier's fault is decided from its reason code **at creation** and then
stored, rather than judged when a report runs. Two reasons, and the second is
the one that decided it:

  * A supplier scorecard computed by filtering on today's definition changes
    retrospectively the moment somebody edits that definition. A number that
    silently rewrites last quarter is not evidence, and a supplier arguing
    about their score is exactly when it must not move.
  * It puts the judgement next to the person making it, at the moment they
    pick the reason, instead of burying it in a dashboard query.

Stock leaves on **dispatch**, not on approval. Approving is a decision; the
goods are still on the premises until they physically go, and a ledger that
says otherwise is wrong for as long as the lorry takes to arrive.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_MANAGE_RETURNS, PERM_APPROVE_RETURN,
    PERM_VIEW_INVENTORY,
)
from app.models.inventory import (
    Item, StockLocation, MOVE_RETURN, REASON_CODES, VENDOR_ATTRIBUTABLE_REASONS,
)
from app.models.inventory_control import (
    VendorReturn, VendorReturnLine,
    RET_DRAFT, RET_PENDING_APPROVAL, RET_APPROVED, RET_DISPATCHED,
    RET_CREDITED, RET_REJECTED, RET_CANCELLED,
)
from app.models.vendor import Vendor
from app.services import sod
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.workflow import _enter_state
from app.services.stock_service import StockService, _as_decimal
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "vendor_return"
WORKFLOW_TYPE = "vendor_return"


def _now():
    return make_naive(to_utc(utc_now()))


class VendorReturnService:
    def __init__(self, db: Session):
        self.db = db
        self.stock = StockService(db)

    # --- creating ------------------------------------------------------------

    def create(
        self, current_user: dict, *, vendor_id: UUID, location_id: UUID,
        reason_code: str, lines: List[Dict],
        purchase_order_id: Optional[UUID] = None,
        reason_note: Optional[str] = None,
    ) -> VendorReturn:
        self._require(current_user, PERM_MANAGE_RETURNS, "raise vendor returns")

        if reason_code not in REASON_CODES:
            raise ValueError(
                f"{reason_code!r} is not a reason code. One of: "
                f"{', '.join(REASON_CODES)}"
            )
        if not lines:
            raise ValueError("A return with no lines returns nothing")

        vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
        if not vendor:
            raise ValueError("Vendor not found")

        location = (
            self.db.query(StockLocation)
            .filter(StockLocation.id == location_id)
            .first()
        )
        if not location:
            raise ValueError("Stock location not found")

        vendor_return = VendorReturn(
            tenant_id=current_user["tenant_id"],
            return_number=self._next_number(),
            vendor_id=vendor_id,
            purchase_order_id=purchase_order_id,
            location_id=location_id,
            reason_code=reason_code,
            reason_note=reason_note,
            # Decided here, once. See the module docstring on why this is not
            # recomputed when a scorecard runs.
            vendor_attributable=reason_code in VENDOR_ATTRIBUTABLE_REASONS,
            current_state=RET_DRAFT,
            created_by=current_user["id"],
            correlation_id=uuid4(),
        )
        self.db.add(vendor_return)
        self.db.flush()

        total = Decimal("0")
        for index, line in enumerate(lines, start=1):
            item = self.db.query(Item).filter(Item.id == line.get("item_id")).first()
            if not item:
                raise ValueError(f"Line {index}: item not found")

            quantity = _as_decimal(line.get("quantity", 0))
            if quantity <= 0:
                raise ValueError(
                    f"Line {index}: a return quantity must be positive. The "
                    "direction is already known — goods are going back."
                )

            unit_cost = _as_decimal(item.standard_cost or 0)
            total += quantity * unit_cost

            self.db.add(VendorReturnLine(
                tenant_id=vendor_return.tenant_id,
                return_id=vendor_return.id,
                item_id=item.id,
                goods_receipt_line_id=line.get("goods_receipt_line_id"),
                line_number=index,
                quantity=quantity,
                unit_cost=item.standard_cost,
                note=line.get("note"),
            ))

        vendor_return.total_value = total
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="created",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_DRAFT,
            after_value={
                "return_number": vendor_return.return_number,
                "vendor": vendor.legal_name,
                "reason_code": reason_code,
                "vendor_attributable": vendor_return.vendor_attributable,
                "total_value": float(total),
            },
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    # --- the workflow --------------------------------------------------------

    def submit(self, return_id: UUID, current_user: dict) -> VendorReturn:
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_MANAGE_RETURNS, "submit vendor returns")

        if vendor_return.current_state != RET_DRAFT:
            raise ValueError(
                f"Only a draft can be submitted; this is {vendor_return.current_state}"
            )

        _enter_state(vendor_return, RET_PENDING_APPROVAL)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="submitted",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_PENDING_APPROVAL,
        )
        NotificationService(self.db).notify_awaiting_action(
            vendor_return, PERM_APPROVE_RETURN, "approve or reject",
            exclude_user_id=vendor_return.created_by,
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    def approve(self, return_id: UUID, current_user: dict) -> VendorReturn:
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_APPROVE_RETURN, "approve vendor returns")

        if vendor_return.current_state != RET_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted return can be approved; this is "
                f"{vendor_return.current_state}"
            )
        # Same separation as an adjustment: sending goods back writes value off
        # the balance sheet, and the person who raised it should not be the one
        # who signs it away.
        if sod._same_person(vendor_return.created_by, current_user.get("id")):
            raise PermissionError(
                "You raised this return, so you cannot approve it."
            )

        _enter_state(vendor_return, RET_APPROVED)
        vendor_return.approved_by = current_user["id"]
        vendor_return.approved_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="approved",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_APPROVED,
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    def dispatch(self, return_id: UUID, current_user: dict) -> VendorReturn:
        """The goods leave. This is when stock moves, not at approval."""
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_MANAGE_RETURNS, "dispatch vendor returns")

        if vendor_return.current_state != RET_APPROVED:
            raise ValueError(
                f"Only an approved return can be dispatched; this is "
                f"{vendor_return.current_state}"
            )

        for line in vendor_return.lines:
            item = self.db.query(Item).filter(Item.id == line.item_id).first()
            if item is None or not item.is_stocked:
                continue
            self.stock.post_movement(
                tenant_id=vendor_return.tenant_id,
                item_id=line.item_id,
                location_id=vendor_return.location_id,
                quantity=-_as_decimal(line.quantity),
                movement_type=MOVE_RETURN,
                current_user=current_user,
                reason_code=vendor_return.reason_code,
                source_type=OBJECT_TYPE,
                source_id=vendor_return.id,
                correlation_id=vendor_return.correlation_id,
            )

        _enter_state(vendor_return, RET_DISPATCHED)
        vendor_return.dispatched_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="dispatched",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_DISPATCHED,
            comment="Stock removed; awaiting the vendor's credit note.",
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    def record_credit(
        self, return_id: UUID, current_user: dict, credit_note_reference: str
    ) -> VendorReturn:
        """The vendor has credited it. Closes the loop back to AP.

        Until this happens the return is money the company is owed, which is
        why dispatched-but-not-credited is worth ageing rather than treating as
        finished business.
        """
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_MANAGE_RETURNS, "record return credits")

        if vendor_return.current_state != RET_DISPATCHED:
            raise ValueError(
                f"Only a dispatched return can be credited; this is "
                f"{vendor_return.current_state}"
            )
        if not credit_note_reference or not credit_note_reference.strip():
            raise ValueError(
                "A credit needs the vendor's credit note reference, or there is "
                "nothing to reconcile it against later"
            )

        _enter_state(vendor_return, RET_CREDITED)
        vendor_return.credit_note_reference = credit_note_reference.strip()
        vendor_return.credited_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="credited",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_CREDITED,
            after_value={"credit_note_reference": vendor_return.credit_note_reference},
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    def reject(
        self, return_id: UUID, current_user: dict, reason: str
    ) -> VendorReturn:
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_APPROVE_RETURN, "reject vendor returns")

        if vendor_return.current_state != RET_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted return can be rejected; this is "
                f"{vendor_return.current_state}"
            )
        if not reason or not reason.strip():
            raise ValueError("A rejection needs a reason")

        _enter_state(vendor_return, RET_REJECTED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="rejected",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_REJECTED,
            comment=reason.strip(),
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    def cancel(self, return_id: UUID, current_user: dict) -> VendorReturn:
        vendor_return = self._get(return_id)
        self._require(current_user, PERM_MANAGE_RETURNS, "cancel vendor returns")

        if vendor_return.current_state in (RET_DISPATCHED, RET_CREDITED, RET_CANCELLED):
            raise ValueError(
                f"A {vendor_return.current_state} return cannot be cancelled. "
                "Goods that have left are brought back with a receipt, not by "
                "withdrawing the record that sent them."
            )

        _enter_state(vendor_return, RET_CANCELLED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=vendor_return.id, action="cancelled",
            workflow_type=WORKFLOW_TYPE, workflow_step=RET_CANCELLED,
        )
        self.db.commit()
        self.db.refresh(vendor_return)
        return vendor_return

    # --- reading -------------------------------------------------------------

    def list_returns(
        self, current_user: dict, state: Optional[str] = None,
        vendor_id: Optional[UUID] = None,
    ) -> List[VendorReturn]:
        self._require(current_user, PERM_VIEW_INVENTORY, "view vendor returns")

        query = self.db.query(VendorReturn)
        if state:
            query = query.filter(VendorReturn.current_state == state)
        if vendor_id:
            query = query.filter(VendorReturn.vendor_id == vendor_id)
        return query.order_by(VendorReturn.created_at.desc()).all()

    def get(self, return_id: UUID, current_user: dict) -> VendorReturn:
        self._require(current_user, PERM_VIEW_INVENTORY, "view vendor returns")
        return self._get(return_id)

    # --- helpers -------------------------------------------------------------

    def _get(self, return_id: UUID) -> VendorReturn:
        vendor_return = (
            self.db.query(VendorReturn)
            .filter(VendorReturn.id == return_id)
            .first()
        )
        if not vendor_return:
            raise ValueError("Return not found")
        return vendor_return

    def _next_number(self) -> str:
        count = self.db.query(VendorReturn).count()
        return f"RET-{count + 1:05d}"

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
