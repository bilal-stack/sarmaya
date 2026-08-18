"""Purchase requisitions: the request, and the approval of it.

This is the first record in the chain, so its approval is the one every later
control is protecting. Three things follow from that:

  * **It starts the correlation chain.** The RFQ, the quotes, the purchase
    order, the receipts, the invoice and the payment all inherit it, so the
    story reads from the business need rather than from the commitment.
  * **Maker-checker applies.** You cannot approve your own request. Without
    this the record adds nothing — a requester who approves their own need has
    simply written themselves a permission slip.
  * **The approved estimate is a ceiling.** A purchase order raised against a
    requisition may not exceed it, or the approval covers a number nobody
    agreed to. Enforced where the order is created, recorded here.

Approval routing reuses the existing matrix, so the same amount thresholds that
decide who approves an invoice decide who may approve a request to spend.
"""
import logging
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.requisition import PurchaseRequisition, PurchaseRequisitionLine
from app.core.enums import RequisitionState, Currency
from app.core.roles import (
    has_permission, can_approve_amount,
    PERM_CREATE_REQUISITION, PERM_VIEW_REQUISITION, PERM_APPROVE_REQUISITION,
)
from app.services.workflow import transition_state
from app.services.correlation import new_correlation_id
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.core.roles import PERM_APPROVE_REQUISITION
from app.services import sod
from app.services.delegation import resolve_permission
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "requisition"


class RequisitionService:
    def __init__(self, db: Session):
        self.db = db

    # --- reads ---------------------------------------------------------------

    def list_requisitions(
        self, current_user: dict, state: Optional[str] = None
    ) -> List[PurchaseRequisition]:
        self._require(current_user, PERM_VIEW_REQUISITION, "view requisitions")
        query = self.db.query(PurchaseRequisition)
        if state:
            query = query.filter(PurchaseRequisition.current_state == state)
        return (
            query.order_by(PurchaseRequisition.created_at.desc()).limit(200).all()
        )

    def get_requisition(
        self, requisition_id: UUID, current_user: dict
    ) -> PurchaseRequisition:
        self._require(current_user, PERM_VIEW_REQUISITION, "view requisitions")
        return self._get(requisition_id)

    # --- writes --------------------------------------------------------------

    def create_requisition(self, data, current_user: dict) -> PurchaseRequisition:
        """Raise a request. Anyone who may create one may create it for
        themselves; approval is somebody else's job."""
        self._require(current_user, PERM_CREATE_REQUISITION, "raise requisitions")

        lines = getattr(data, "lines", None) or []
        requisition = PurchaseRequisition(
            tenant_id=current_user["tenant_id"],
            requisition_number=self._next_number(),
            title=data.title,
            justification=data.justification,
            budget_code=getattr(data, "budget_code", None),
            department=getattr(data, "department", None),
            requested_date=getattr(data, "requested_date", None) or utc_now().date(),
            needed_by=getattr(data, "needed_by", None),
            currency=getattr(data, "currency", None) or Currency.PKR,
            current_state=RequisitionState.DRAFT,
            # This record starts the story everything downstream joins.
            correlation_id=new_correlation_id(),
            created_by=current_user["id"],
            estimated_amount=Decimal("0"),
        )
        self.db.add(requisition)
        self.db.flush()

        total = Decimal("0")
        for index, line in enumerate(lines, start=1):
            amount = Decimal(str(line.quantity)) * Decimal(str(line.estimated_unit_price))
            total += amount
            self.db.add(PurchaseRequisitionLine(
                tenant_id=current_user["tenant_id"],
                requisition_id=requisition.id,
                line_number=index,
                description=line.description,
                product_code=getattr(line, "product_code", None),
                quantity=Decimal(str(line.quantity)),
                estimated_unit_price=Decimal(str(line.estimated_unit_price)),
                estimated_amount=amount,
            ))

        requisition.estimated_amount = total
        self.db.add(requisition)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="created",
            workflow_type=OBJECT_TYPE,
            after_value={
                "requisition_number": requisition.requisition_number,
                "title": requisition.title,
                "estimated_amount": str(total),
                "lines": len(lines),
            },
        )
        self.db.commit()
        self.db.refresh(requisition)
        return requisition

    def submit_requisition(
        self, requisition_id: UUID, current_user: dict
    ) -> PurchaseRequisition:
        self._require(current_user, PERM_CREATE_REQUISITION, "submit requisitions")
        requisition = self._get(requisition_id)

        if not transition_state(
            self.db, requisition, RequisitionState.PENDING_APPROVAL.value,
            current_user["id"],
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="submitted",
            workflow_step=self._state(requisition),
            workflow_type=OBJECT_TYPE,
            after_value={"estimated_amount": str(requisition.estimated_amount)},
        )
        NotificationService(self.db).notify_awaiting_action(
            requisition, PERM_APPROVE_REQUISITION, "approve or reject",
            exclude_user_id=requisition.created_by,
        )
        self.db.commit()
        self.db.refresh(requisition)
        return requisition

    def approve_requisition(
        self, requisition_id: UUID, current_user: dict
    ) -> PurchaseRequisition:
        """Authorise the need.

        The approval this grants is what an auditor traces back to when asking
        why anything downstream exists, so it carries the same controls as an
        invoice approval: a distinct permission, maker-checker, and the
        approval matrix's amount limits.
        """
        can_approve, delegation = resolve_permission(
            self.db, current_user, PERM_APPROVE_REQUISITION
        )
        if not can_approve:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "approve requisitions"
            )

        requisition = self._get(requisition_id)

        # Maker-checker. A requester approving their own request turns the
        # record from a control into a formality.
        if sod.violates_self_approval(requisition, current_user):
            self._audit_block(requisition, current_user, "self_approval")
            raise PermissionError(
                "Segregation of duties: a requisition must be approved by "
                "someone other than the person who raised it."
            )

        # The same thresholds that decide who approves an invoice decide who
        # may authorise a request to spend — reused rather than reimplemented
        # so the two cannot drift.
        amount = float(requisition.estimated_amount or 0)
        allowed, why_not = can_approve_amount(current_user["role"], amount)
        if not allowed:
            self._audit_block(requisition, current_user, "over_approval_limit")
            raise PermissionError(why_not)

        if not transition_state(
            self.db, requisition, RequisitionState.APPROVED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")

        requisition.approved_by = current_user["id"]
        requisition.approved_at = utc_now()
        self.db.add(requisition)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="approved",
            workflow_step=self._state(requisition),
            workflow_type=OBJECT_TYPE,
            comment=requisition.justification,
            after_value={
                "approved_by": str(current_user["id"]),
                "estimated_amount": str(requisition.estimated_amount),
                "acted_under_delegation": bool(delegation),
                "delegated_authority_of": (
                    str(delegation.from_user_id) if delegation else None
                ),
            },
        )
        self.db.commit()
        self.db.refresh(requisition)
        return requisition

    def reject_requisition(
        self, requisition_id: UUID, reason: str, current_user: dict
    ) -> PurchaseRequisition:
        can_approve, _ = resolve_permission(
            self.db, current_user, PERM_APPROVE_REQUISITION
        )
        if not can_approve:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "reject requisitions"
            )
        requisition = self._get(requisition_id)

        if not transition_state(
            self.db, requisition, RequisitionState.REJECTED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        requisition.rejection_reason = reason
        self.db.add(requisition)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="rejected",
            workflow_step=self._state(requisition),
            workflow_type=OBJECT_TYPE,
            comment=reason,
        )
        self.db.commit()
        self.db.refresh(requisition)
        return requisition

    def cancel_requisition(
        self, requisition_id: UUID, reason: str, current_user: dict
    ) -> PurchaseRequisition:
        self._require(current_user, PERM_CREATE_REQUISITION, "cancel requisitions")
        requisition = self._get(requisition_id)

        if not transition_state(
            self.db, requisition, RequisitionState.CANCELLED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="cancelled",
            workflow_step=self._state(requisition),
            workflow_type=OBJECT_TYPE,
            comment=reason,
        )
        self.db.commit()
        self.db.refresh(requisition)
        return requisition

    # --- helpers -------------------------------------------------------------

    def _get(self, requisition_id: UUID) -> PurchaseRequisition:
        requisition = (
            self.db.query(PurchaseRequisition)
            .filter(PurchaseRequisition.id == requisition_id)
            .first()
        )
        if not requisition:
            raise ValueError("Requisition not found")
        return requisition

    @staticmethod
    def _state(requisition: PurchaseRequisition) -> str:
        return str(
            getattr(requisition.current_state, "value", requisition.current_state)
        ).lower()

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _audit_block(
        self, requisition: PurchaseRequisition, current_user: dict, reason: str
    ) -> None:
        """A refused approval is committed on its own, so the attempt survives
        even though the action does not."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=requisition.id,
            action="approval_blocked",
            workflow_type=OBJECT_TYPE,
            comment=reason,
            after_value={"reason": reason},
        )
        self.db.commit()

    def _next_number(self) -> str:
        existing = self.db.query(PurchaseRequisition).count()
        return f"REQ-{existing + 1:05d}"
