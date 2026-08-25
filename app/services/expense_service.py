"""Expense reimbursements.

Build Book, Variant C2: "expense reimbursements with policy rules and evidence
requirements".

A claim is a payment request with a person attached, so it is shaped like an
invoice and controlled like one. Two rules carry the weight:

**A claim needs a receipt.** Without one it is an assertion, and an
organisation that pays assertions has no expense control at all. The
requirement is enforced at submission rather than at approval, so the claimant
finds out while they still have the receipt in their hand rather than a week
later.

**Nobody approves their own claim** — not even an administrator. This is the
version of self-approval that is easiest to rationalise ("it was only lunch")
and therefore the one most worth making impossible.

A policy rule can be waived, but only with a reason, and the waiver lands in
`policy_override_reason` — the same field the overrides dashboard already
reports on, so a waived expense rule joins that report without any new
plumbing.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_CLAIM_EXPENSE, PERM_APPROVE_EXPENSE, PERM_VIEW_HR,
)
from app.models.employee import Employee
from app.models.file import File
from app.models.hr import (
    ExpenseReimbursement, EXP_DRAFT, EXP_PENDING_APPROVAL, EXP_APPROVED,
    EXP_PAID, EXP_REJECTED, EXP_CANCELLED,
)
from app.services import sod
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.integration_posting_service import JournalPostingService
from app.models.integration import SOURCE_EXPENSE_REIMBURSEMENT
from app.services.workflow import _enter_state
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "expense_reimbursement"
WORKFLOW_TYPE = "expense_reimbursement"

#: Above this, a receipt is required no matter the category. Small cash items
#: are the ones that genuinely lose their paperwork; a large claim without a
#: receipt is a different conversation.
RECEIPT_REQUIRED_ABOVE = Decimal("1000")

#: Categories where a receipt is required whatever the amount, because these
#: are the ones that get abused when they are not evidenced.
ALWAYS_EVIDENCED = ("travel", "accommodation", "entertainment", "equipment")


def _now():
    return make_naive(to_utc(utc_now()))


class ExpenseService:
    def __init__(self, db: Session):
        self.db = db

    # --- creating ------------------------------------------------------------

    def create(
        self, current_user: dict, *, employee_id: UUID, category: str,
        total_amount: Decimal, incurred_date, description: Optional[str] = None,
        currency: Optional[str] = None, org_unit_id: Optional[UUID] = None,
    ) -> ExpenseReimbursement:
        self._require(current_user, PERM_CLAIM_EXPENSE, "claim expenses")

        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError("Employee not found")

        total_amount = Decimal(str(total_amount or 0))
        if total_amount <= 0:
            raise ValueError("A claim needs an amount")
        if not (category or "").strip():
            raise ValueError("A claim needs a category")

        claim = ExpenseReimbursement(
            tenant_id=current_user["tenant_id"],
            claim_number=self._next_number(),
            employee_id=employee.id,
            org_unit_id=org_unit_id or employee.org_unit_id,
            category=category.strip().lower(),
            description=description,
            total_amount=total_amount,
            currency=currency,
            incurred_date=incurred_date,
            current_state=EXP_DRAFT,
            created_by=current_user["id"],
            correlation_id=uuid4(),
        )
        self.db.add(claim)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="created",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_DRAFT,
            after_value={
                "claim_number": claim.claim_number,
                "category": claim.category,
                "total_amount": float(total_amount),
            },
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    # --- the evidence rule ---------------------------------------------------

    def receipt_required(self, claim: ExpenseReimbursement) -> bool:
        return (
            claim.category in ALWAYS_EVIDENCED
            or Decimal(claim.total_amount or 0) > RECEIPT_REQUIRED_ABOVE
        )

    def _refresh_receipt_flag(self, claim: ExpenseReimbursement) -> bool:
        """Whether a receipt is actually attached, read from the file store.

        Denormalised onto the claim so the submit guard is one read and the
        reason a claim was blocked is legible on the record itself.
        """
        attached = (
            self.db.query(File)
            .filter(
                File.object_type == OBJECT_TYPE,
                File.object_id == claim.id,
            )
            .count()
        )
        claim.has_receipt = attached > 0
        return claim.has_receipt

    # --- the workflow --------------------------------------------------------

    def submit(self, claim_id: UUID, current_user: dict) -> ExpenseReimbursement:
        """Send it for approval, if there is anything to approve.

        The receipt check runs here rather than at approval so the claimant
        finds out while they still have the receipt, rather than a week later
        when an approver bounces it.
        """
        claim = self._get(claim_id)
        self._require(current_user, PERM_CLAIM_EXPENSE, "submit expense claims")

        if claim.current_state != EXP_DRAFT:
            raise ValueError(
                f"Only a draft can be submitted; this is {claim.current_state}"
            )

        self._refresh_receipt_flag(claim)
        if self.receipt_required(claim) and not claim.has_receipt:
            raise ValueError(
                f"This claim needs a receipt: {claim.category} claims and "
                f"anything over {RECEIPT_REQUIRED_ABOVE} must be evidenced. "
                "A claim with no receipt is an assertion."
            )

        _enter_state(claim, EXP_PENDING_APPROVAL)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="submitted",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_PENDING_APPROVAL,
            after_value={
                "total_amount": float(claim.total_amount),
                "has_receipt": claim.has_receipt,
            },
        )
        NotificationService(self.db).notify_awaiting_action(
            claim, PERM_APPROVE_EXPENSE, "approve or reject",
            exclude_user_id=claim.created_by,
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    def approve(
        self, claim_id: UUID, current_user: dict,
        override_reason: Optional[str] = None,
    ) -> ExpenseReimbursement:
        claim = self._get(claim_id)
        self._require(current_user, PERM_APPROVE_EXPENSE, "approve expense claims")

        if claim.current_state != EXP_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted claim can be approved; this is "
                f"{claim.current_state}"
            )

        # No admin exemption. This is the self-approval that is easiest to
        # rationalise, which is what makes it worth making impossible.
        if sod._same_person(claim.created_by, current_user.get("id")):
            raise PermissionError(
                "You raised this claim, so you cannot approve it."
            )
        employee = (
            self.db.query(Employee).filter(Employee.id == claim.employee_id).first()
        )
        if employee is not None and employee.user_id is not None and sod._same_person(
            employee.user_id, current_user.get("id")
        ):
            raise PermissionError(
                "This claim is yours, so you cannot approve it — even if "
                "somebody else entered it for you."
            )

        # A rule can be waived, with a reason, and the waiver is reported.
        if self.receipt_required(claim) and not claim.has_receipt:
            if not (override_reason or "").strip():
                raise ValueError(
                    "This claim has no receipt and one is required. Approving "
                    "it anyway needs a written reason, which is recorded and "
                    "reported as a policy override."
                )
            claim.policy_override_reason = override_reason.strip()

        _enter_state(claim, EXP_APPROVED)
        claim.approved_by = current_user["id"]
        claim.approved_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="approved",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_APPROVED,
            comment=claim.policy_override_reason,
            after_value={
                "total_amount": float(claim.total_amount),
                "policy_override": bool(claim.policy_override_reason),
            },
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    def reject(
        self, claim_id: UUID, current_user: dict, reason: str
    ) -> ExpenseReimbursement:
        claim = self._get(claim_id)
        self._require(current_user, PERM_APPROVE_EXPENSE, "reject expense claims")

        if claim.current_state != EXP_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted claim can be rejected; this is "
                f"{claim.current_state}"
            )
        if not reason or not reason.strip():
            raise ValueError("A rejection needs a reason")

        _enter_state(claim, EXP_REJECTED)
        claim.rejected_reason = reason.strip()
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="rejected",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_REJECTED,
            comment=reason.strip(),
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    def mark_paid(self, claim_id: UUID, current_user: dict) -> ExpenseReimbursement:
        claim = self._get(claim_id)
        self._require(current_user, PERM_APPROVE_EXPENSE, "settle expense claims")

        if claim.current_state != EXP_APPROVED:
            raise ValueError(
                f"Only an approved claim can be paid; this is {claim.current_state}"
            )

        _enter_state(claim, EXP_PAID)
        claim.paid_at = _now()
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="paid",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_PAID,
        )

        # Opportunistic, same as the payment call site — a no-op for every
        # tenant with no accounting system connected. See
        # JournalPostingService.enqueue.
        JournalPostingService(self.db).enqueue(
            SOURCE_EXPENSE_REIMBURSEMENT, claim, current_user
        )

        self.db.commit()
        self.db.refresh(claim)
        return claim

    def cancel(self, claim_id: UUID, current_user: dict) -> ExpenseReimbursement:
        claim = self._get(claim_id)
        self._require(current_user, PERM_CLAIM_EXPENSE, "cancel expense claims")

        if claim.current_state == EXP_PAID:
            raise ValueError(
                "A paid claim cannot be cancelled. Money has left; recover it "
                "with a separate record rather than by withdrawing this one."
            )

        _enter_state(claim, EXP_CANCELLED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=claim.id, action="cancelled",
            workflow_type=WORKFLOW_TYPE, workflow_step=EXP_CANCELLED,
        )
        self.db.commit()
        self.db.refresh(claim)
        return claim

    # --- reading -------------------------------------------------------------

    def list_claims(
        self, current_user: dict, state: Optional[str] = None,
        employee_id: Optional[UUID] = None,
    ) -> List[Dict]:
        """Claims this caller may see.

        Somebody who can only claim sees their own. That is not a convenience:
        an expense list is a record of where people went and what they bought,
        and there is no reason for it to be readable across the company by
        anyone who happens to hold a login.
        """
        query = self.db.query(ExpenseReimbursement)

        if not has_permission(current_user["role"], PERM_VIEW_HR):
            if not has_permission(current_user["role"], PERM_CLAIM_EXPENSE):
                raise PermissionError(
                    f"Role '{current_user['role']}' cannot view expense claims"
                )
            mine = (
                self.db.query(Employee)
                .filter(Employee.user_id == current_user.get("id"))
                .first()
            )
            query = query.filter(
                ExpenseReimbursement.employee_id == (
                    mine.id if mine else None
                )
            )

        if state:
            query = query.filter(ExpenseReimbursement.current_state == state)
        if employee_id:
            query = query.filter(ExpenseReimbursement.employee_id == employee_id)

        return [
            self.render(claim)
            for claim in query.order_by(
                ExpenseReimbursement.created_at.desc()
            ).all()
        ]

    def render(self, claim: ExpenseReimbursement) -> Dict:
        return {
            "id": claim.id,
            "claim_number": claim.claim_number,
            "employee_id": claim.employee_id,
            "category": claim.category,
            "description": claim.description,
            "total_amount": float(claim.total_amount or 0),
            "currency": claim.currency,
            "incurred_date": claim.incurred_date,
            "has_receipt": claim.has_receipt,
            "receipt_required": self.receipt_required(claim),
            "current_state": claim.current_state,
            "created_by": claim.created_by,
            "approved_by": claim.approved_by,
            "policy_override_reason": claim.policy_override_reason,
            "paid_at": claim.paid_at,
            "created_at": claim.created_at,
            "correlation_id": claim.correlation_id,
        }

    # --- helpers -------------------------------------------------------------

    def _get(self, claim_id: UUID) -> ExpenseReimbursement:
        claim = (
            self.db.query(ExpenseReimbursement)
            .filter(ExpenseReimbursement.id == claim_id)
            .first()
        )
        if not claim:
            raise ValueError("Expense claim not found")
        return claim

    def _next_number(self) -> str:
        count = self.db.query(ExpenseReimbursement).count()
        return f"EXP-{count + 1:05d}"

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
