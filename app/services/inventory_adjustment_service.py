"""Inventory adjustments: the controlled way stock changes with no delivery.

Build Book, Variant D1: "inventory adjustments approval with thresholds and
evidence", "adjustment thresholds with dual approval above limit", "SoD
separation between receiver and approver".

This is the fraud surface of the inventory module, and it is worth being
explicit about why. Every other stock movement has a physical event behind it —
a lorry arrived, goods went back. An adjustment has nothing but somebody's
word, which makes writing stock off the way a theft is covered up. So it is the
one inventory record with a full workflow, a value threshold, two approvers
above the limit, and a separation between whoever counted and whoever signs.

Three decisions worth stating:

**Approving and posting are separate.** Approval is a decision; posting is what
moves the ledger. Collapsing them would mean an approval that fails to post
leaves stock silently unchanged with the paperwork saying otherwise. Kept
apart, that shows up as an approved-but-unposted row somebody can see.

**The threshold is evaluated once, at submission, and stored.** Recomputing at
approval time would let the required approver change after the fact if an
item's standard cost were edited in between — which is a way to route a large
write-off past the second signature without touching the adjustment itself.

**The second approver must be a different person from the first.** Otherwise
"dual approval" is one person clicking twice, which is the control failing
while appearing to work.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_ADJUST_INVENTORY, PERM_APPROVE_ADJUSTMENT,
    PERM_VIEW_INVENTORY,
)
from app.models.inventory import (
    Item, StockLocation, REASON_CODES, MOVE_ADJUSTMENT,
)
from app.models.inventory_control import (
    InventoryAdjustment, InventoryAdjustmentLine,
    ADJ_DRAFT, ADJ_PENDING_APPROVAL, ADJ_APPROVED, ADJ_POSTED, ADJ_REJECTED,
    ADJ_CANCELLED,
)
from app.services import sod
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.workflow import _enter_state
from app.services.stock_service import StockService, _as_decimal
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "inventory_adjustment"
WORKFLOW_TYPE = "inventory_adjustment"

#: Above this value, a second approver is required. A setting rather than a
#: constant would be better and is a configuration question; this is the
#: default the provisioning seeds, matching how approval policies already work.
DEFAULT_DUAL_APPROVAL_THRESHOLD = Decimal("50000")


def _now():
    return make_naive(to_utc(utc_now()))


class InventoryAdjustmentService:
    def __init__(self, db: Session):
        self.db = db
        self.stock = StockService(db)

    # --- creating ------------------------------------------------------------

    def create(
        self, current_user: dict, *, location_id: UUID, reason_code: str,
        lines: List[Dict], reason_note: Optional[str] = None,
    ) -> InventoryAdjustment:
        """Raise an adjustment in draft.

        `lines` are dicts of {item_id, quantity_change, note?}. Signed:
        negative writes stock off, positive writes it on.
        """
        self._require(current_user, PERM_ADJUST_INVENTORY, "raise inventory adjustments")

        if reason_code not in REASON_CODES:
            raise ValueError(
                f"{reason_code!r} is not a reason code. One of: "
                f"{', '.join(REASON_CODES)}"
            )
        if not lines:
            raise ValueError("An adjustment with no lines changes nothing")

        location = (
            self.db.query(StockLocation)
            .filter(StockLocation.id == location_id)
            .first()
        )
        if not location:
            raise ValueError("Stock location not found")

        adjustment = InventoryAdjustment(
            tenant_id=current_user["tenant_id"],
            adjustment_number=self._next_number(),
            location_id=location_id,
            reason_code=reason_code,
            reason_note=reason_note,
            current_state=ADJ_DRAFT,
            created_by=current_user["id"],
            correlation_id=uuid4(),
        )
        self.db.add(adjustment)
        self.db.flush()

        for index, line in enumerate(lines, start=1):
            self._add_line(adjustment, index, line, location_id)

        adjustment.total_value = self._value_of(adjustment)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=adjustment.id, action="created",
            workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_DRAFT,
            after_value={
                "adjustment_number": adjustment.adjustment_number,
                "reason_code": reason_code,
                "lines": len(lines),
                "total_value": float(adjustment.total_value),
            },
        )
        self.db.commit()
        self.db.refresh(adjustment)
        return adjustment

    def _add_line(
        self, adjustment: InventoryAdjustment, index: int, line: Dict,
        location_id: UUID,
    ) -> None:
        item_id = line.get("item_id")
        item = self.db.query(Item).filter(Item.id == item_id).first()
        if not item:
            raise ValueError(f"Line {index}: item not found")
        if not item.is_stocked:
            raise ValueError(
                f"Line {index}: {item.sku} is not a stocked item, so there is "
                "nothing to adjust"
            )

        change = _as_decimal(line.get("quantity_change", 0))
        if change == 0:
            raise ValueError(f"Line {index}: a change of zero adjusts nothing")

        self.db.add(InventoryAdjustmentLine(
            tenant_id=adjustment.tenant_id,
            adjustment_id=adjustment.id,
            item_id=item_id,
            line_number=index,
            quantity_change=change,
            # Recorded now so the adjustment reads as "expected 40, found 37"
            # later, rather than as a bare -3 that means nothing on its own.
            quantity_before=self.stock.on_hand(item_id, location_id),
            unit_cost=item.standard_cost,
            note=line.get("note"),
        ))

    def _value_of(self, adjustment: InventoryAdjustment) -> Decimal:
        """Absolute value at standard cost.

        Absolute because a write-off of 100k and a write-on of 100k are equally
        worth a second signature — netting them would let a single adjustment
        move a fortune in both directions and score as zero.
        """
        self.db.flush()
        total = Decimal("0")
        for line in adjustment.lines:
            cost = _as_decimal(line.unit_cost or 0)
            total += abs(_as_decimal(line.quantity_change)) * cost
        return total

    # --- the workflow --------------------------------------------------------

    def submit(self, adjustment_id: UUID, current_user: dict) -> InventoryAdjustment:
        adjustment = self._get(adjustment_id)
        self._require(current_user, PERM_ADJUST_INVENTORY, "submit inventory adjustments")

        if adjustment.current_state != ADJ_DRAFT:
            raise ValueError(
                f"Only a draft can be submitted; this is {adjustment.current_state}"
            )
        if not adjustment.lines:
            raise ValueError("An adjustment with no lines changes nothing")

        adjustment.total_value = self._value_of(adjustment)
        # Decided here and stored: see the module docstring on why this is not
        # recomputed at approval time.
        adjustment.requires_dual_approval = (
            adjustment.total_value > DEFAULT_DUAL_APPROVAL_THRESHOLD
        )
        _enter_state(adjustment, ADJ_PENDING_APPROVAL)
        adjustment.submitted_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=adjustment.id, action="submitted",
            workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_PENDING_APPROVAL,
            comment=(
                f"Value {adjustment.total_value}. "
                + ("Two approvers required." if adjustment.requires_dual_approval
                   else "One approver required.")
            ),
            after_value={
                "total_value": float(adjustment.total_value),
                "requires_dual_approval": adjustment.requires_dual_approval,
            },
        )
        # Told on arrival, not on breach. Without this the first an approver
        # hears about a write-off is the escalation saying it is late, which
        # makes the escalation meaningless as a signal (DR-037). Enqueued
        # inside the transaction so the message and the state land together.
        NotificationService(self.db).notify_awaiting_action(
            adjustment, PERM_APPROVE_ADJUSTMENT, "approve or reject",
            exclude_user_id=adjustment.created_by,
        )
        self.db.commit()
        self.db.refresh(adjustment)
        return adjustment

    def approve(self, adjustment_id: UUID, current_user: dict) -> InventoryAdjustment:
        """Approve, and post once every required signature is present."""
        adjustment = self._get(adjustment_id)
        self._require(
            current_user, PERM_APPROVE_ADJUSTMENT, "approve inventory adjustments"
        )

        if adjustment.current_state != ADJ_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted adjustment can be approved; this is "
                f"{adjustment.current_state}"
            )

        # Build Book: "SoD separation between receiver and approver". Deliberately
        # without the admin exemption the invoice rules carry: this is the
        # control that stops somebody writing off the stock they are
        # accountable for, and an admin doing it is precisely the case it
        # exists for.
        if sod._same_person(adjustment.created_by, current_user.get("id")):
            raise PermissionError(
                "You raised this adjustment, so you cannot approve it. Writing "
                "stock off is how a loss gets covered up, which is why the "
                "person who counted and the person who signs must differ."
            )

        first_approver_id = adjustment.approved_by
        if first_approver_id is None:
            adjustment.approved_by = current_user["id"]
            adjustment.approved_at = _now()
            action = "approved"
            if adjustment.requires_dual_approval:
                # Still waiting, on somebody who is neither the raiser nor the
                # person who just signed. Silence here would leave a large
                # write-off half-approved with nobody told it needs them.
                NotificationService(self.db).notify_awaiting_action(
                    adjustment, PERM_APPROVE_ADJUSTMENT,
                    "provide the second approval",
                    exclude_user_id=current_user["id"],
                )
        else:
            if sod._same_person(first_approver_id, current_user.get("id")):
                raise PermissionError(
                    "This adjustment already carries your approval. A second "
                    "signature from the same person is one person clicking "
                    "twice, not dual approval."
                )
            adjustment.second_approved_by = current_user["id"]
            adjustment.second_approved_at = _now()
            action = "second_approved"

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=adjustment.id, action=action,
            workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_PENDING_APPROVAL,
            after_value={"total_value": float(adjustment.total_value)},
        )

        if self._fully_approved(adjustment):
            _enter_state(adjustment, ADJ_APPROVED)
            self._post(adjustment, current_user)
            # Written here rather than inside _post: the audit entry and the
            # movements it describes have to land in the same commit, and the
            # method that owns the transaction is the one that can guarantee
            # that. A trail written by a helper that never commits is discarded
            # when the session closes.
            log_audit(
                db=self.db, tenant_id=adjustment.tenant_id,
                user_id=current_user["id"], object_type=OBJECT_TYPE,
                object_id=adjustment.id, action="posted",
                workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_POSTED,
                comment=f"{len(adjustment.lines)} stock movement(s) recorded.",
            )

        self.db.commit()
        self.db.refresh(adjustment)
        return adjustment

    def _fully_approved(self, adjustment: InventoryAdjustment) -> bool:
        if adjustment.approved_by is None:
            return False
        if adjustment.requires_dual_approval:
            return adjustment.second_approved_by is not None
        return True

    def _post(self, adjustment: InventoryAdjustment, current_user: dict) -> None:
        """Move the ledger.

        Separate from approval, and inside the same transaction: an adjustment
        that is marked posted while its movements rolled back would be a
        paperwork trail describing stock that never changed. The audit entry
        for posting is written by the caller, which owns the commit.
        """
        for line in adjustment.lines:
            self.stock.post_movement(
                tenant_id=adjustment.tenant_id,
                item_id=line.item_id,
                location_id=adjustment.location_id,
                quantity=line.quantity_change,
                movement_type=MOVE_ADJUSTMENT,
                current_user=current_user,
                reason_code=adjustment.reason_code,
                source_type=OBJECT_TYPE,
                source_id=adjustment.id,
                note=line.note,
                correlation_id=adjustment.correlation_id,
            )

        _enter_state(adjustment, ADJ_POSTED)
        adjustment.posted_at = _now()

    def reject(
        self, adjustment_id: UUID, current_user: dict, reason: str
    ) -> InventoryAdjustment:
        adjustment = self._get(adjustment_id)
        self._require(
            current_user, PERM_APPROVE_ADJUSTMENT, "reject inventory adjustments"
        )

        if adjustment.current_state != ADJ_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted adjustment can be rejected; this is "
                f"{adjustment.current_state}"
            )
        if not reason or not reason.strip():
            raise ValueError("A rejection needs a reason")

        _enter_state(adjustment, ADJ_REJECTED)
        adjustment.rejected_reason = reason.strip()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=adjustment.id, action="rejected",
            workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_REJECTED,
            comment=reason.strip(),
        )
        self.db.commit()
        self.db.refresh(adjustment)
        return adjustment

    def cancel(self, adjustment_id: UUID, current_user: dict) -> InventoryAdjustment:
        adjustment = self._get(adjustment_id)
        self._require(current_user, PERM_ADJUST_INVENTORY, "cancel inventory adjustments")

        if adjustment.current_state in (ADJ_POSTED, ADJ_CANCELLED):
            raise ValueError(
                f"A {adjustment.current_state} adjustment cannot be cancelled. "
                "Posted stock is reversed with an opposing adjustment, not by "
                "withdrawing the one that moved it."
            )

        _enter_state(adjustment, ADJ_CANCELLED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=adjustment.id, action="cancelled",
            workflow_type=WORKFLOW_TYPE, workflow_step=ADJ_CANCELLED,
        )
        self.db.commit()
        self.db.refresh(adjustment)
        return adjustment

    # --- reading -------------------------------------------------------------

    def list_adjustments(
        self, current_user: dict, state: Optional[str] = None,
        location_id: Optional[UUID] = None,
    ) -> List[InventoryAdjustment]:
        self._require(current_user, PERM_VIEW_INVENTORY, "view inventory adjustments")

        query = self.db.query(InventoryAdjustment)
        if state:
            query = query.filter(InventoryAdjustment.current_state == state)
        if location_id:
            query = query.filter(InventoryAdjustment.location_id == location_id)
        return query.order_by(InventoryAdjustment.created_at.desc()).all()

    def get(self, adjustment_id: UUID, current_user: dict) -> InventoryAdjustment:
        self._require(current_user, PERM_VIEW_INVENTORY, "view inventory adjustments")
        return self._get(adjustment_id)

    # --- helpers -------------------------------------------------------------

    def _get(self, adjustment_id: UUID) -> InventoryAdjustment:
        adjustment = (
            self.db.query(InventoryAdjustment)
            .filter(InventoryAdjustment.id == adjustment_id)
            .first()
        )
        if not adjustment:
            raise ValueError("Adjustment not found")
        return adjustment

    def _next_number(self) -> str:
        count = self.db.query(InventoryAdjustment).count()
        return f"ADJ-{count + 1:05d}"

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
