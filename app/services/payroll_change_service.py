"""Changing what somebody is paid.

Build Book, Variant C: "payroll change requests, approvals, payroll run
evidence capture", and among the controls, "SoD for HR actions and payroll
approvals".

This is the clearest separation-of-duties surface in the product, and it has
three distinct failure modes rather than one:

  * **Raising your own salary.** The obvious one, and the easiest to stop.
  * **Approving your own request.** Also easy, and already the shape every
    other module uses.
  * **Approving a rise for your own manager.** The subtle one, and the reason
    the first two are not enough: if I approve my manager's rise and my manager
    approves mine, both requests pass every check that only looks at one record
    at a time. So the rule reaches one step up the reporting line.

The applied change is written to the employee record only at the end, in the
same transaction as the approval. A request that approved but failed to apply
would leave the paperwork saying somebody got a rise they never received; one
that applied before approval would be a rise nobody signed.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_REQUEST_PAYROLL_CHANGE, PERM_APPROVE_PAYROLL_CHANGE,
    PERM_VIEW_HR, PERM_VIEW_COMPENSATION,
)
from app.models.employee import Employee
from app.models.hr import (
    PayrollChangeRequest, PAY_REASONS,
    PAY_DRAFT, PAY_PENDING_APPROVAL, PAY_APPROVED, PAY_APPLIED, PAY_REJECTED,
    PAY_CANCELLED,
)
from app.services import sod
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.workflow import _enter_state
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "payroll_change_request"
WORKFLOW_TYPE = "payroll_change_request"


def _now():
    return make_naive(to_utc(utc_now()))


class PayrollChangeService:
    def __init__(self, db: Session):
        self.db = db

    # --- creating ------------------------------------------------------------

    def create(
        self, current_user: dict, *, employee_id: UUID, new_salary: Decimal,
        reason_code: str, effective_date, reason_note: Optional[str] = None,
    ) -> PayrollChangeRequest:
        self._require(
            current_user, PERM_REQUEST_PAYROLL_CHANGE, "request payroll changes"
        )

        if reason_code not in PAY_REASONS:
            raise ValueError(
                f"{reason_code!r} is not a payroll change reason. One of: "
                f"{', '.join(PAY_REASONS)}"
            )

        employee = self._employee(employee_id)
        new_salary = Decimal(str(new_salary))
        if new_salary < 0:
            raise ValueError("A salary cannot be negative")

        # SoD, first form: nobody raises their own.
        if self._is_self(employee, current_user):
            raise PermissionError(
                "You cannot raise a payroll change for yourself. Ask your "
                "manager to raise it."
            )

        current_salary = employee.base_salary
        change = PayrollChangeRequest(
            tenant_id=current_user["tenant_id"],
            request_number=self._next_number(),
            employee_id=employee.id,
            reason_code=reason_code,
            reason_note=reason_note,
            current_salary=current_salary,
            new_salary=new_salary,
            # The size of the jump, which is what the approval threshold reads.
            # Absolute, so a cut of 200k gets the same scrutiny as a rise: both
            # are large changes to somebody's pay and both can be a mistake.
            total_amount=abs(new_salary - Decimal(current_salary or 0)),
            effective_date=effective_date,
            current_state=PAY_DRAFT,
            created_by=current_user["id"],
            correlation_id=uuid4(),
        )
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=change.id, action="created",
            workflow_type=WORKFLOW_TYPE, workflow_step=PAY_DRAFT,
            # The change amount is recorded; the salaries themselves are not.
            # audit.view is a wider audience than hr.view_compensation, and
            # writing pay into the trail would route around the masking.
            after_value={
                "request_number": change.request_number,
                "employee": employee.employee_number,
                "reason_code": reason_code,
                "change_amount": float(change.total_amount),
                "direction": "increase" if new_salary > Decimal(
                    current_salary or 0
                ) else "decrease",
            },
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    # --- the workflow --------------------------------------------------------

    def submit(self, change_id: UUID, current_user: dict) -> PayrollChangeRequest:
        change = self._get(change_id)
        self._require(
            current_user, PERM_REQUEST_PAYROLL_CHANGE, "submit payroll changes"
        )

        if change.current_state != PAY_DRAFT:
            raise ValueError(
                f"Only a draft can be submitted; this is {change.current_state}"
            )

        _enter_state(change, PAY_PENDING_APPROVAL)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=change.id, action="submitted",
            workflow_type=WORKFLOW_TYPE, workflow_step=PAY_PENDING_APPROVAL,
        )
        NotificationService(self.db).notify_awaiting_action(
            change, PERM_APPROVE_PAYROLL_CHANGE, "approve or reject",
            exclude_user_id=change.created_by,
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def approve(self, change_id: UUID, current_user: dict) -> PayrollChangeRequest:
        """Approve and apply, in one transaction.

        Applying separately would let an approved change sit unapplied — the
        paperwork saying somebody got a rise that never reached their pay.
        """
        change = self._get(change_id)
        self._require(
            current_user, PERM_APPROVE_PAYROLL_CHANGE, "approve payroll changes"
        )

        if change.current_state != PAY_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted change can be approved; this is "
                f"{change.current_state}"
            )

        employee = self._employee(change.employee_id)
        self._refuse_conflicted_approver(change, employee, current_user)

        _enter_state(change, PAY_APPROVED)
        change.approved_by = current_user["id"]
        change.approved_at = _now()

        before_salary = employee.base_salary
        employee.base_salary = change.new_salary
        change.applied_at = _now()
        _enter_state(change, PAY_APPLIED)

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=change.id, action="approved",
            workflow_type=WORKFLOW_TYPE, workflow_step=PAY_APPLIED,
            after_value={
                "change_amount": float(change.total_amount),
                "effective_date": str(change.effective_date),
                "applied": True,
            },
        )
        # The employee's own trail carries it too, so reading one person's
        # history shows their pay changing without having to know that payroll
        # changes are a separate record.
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="employee",
            object_id=employee.id, action="salary_changed",
            comment=(
                f"{change.request_number}: {change.reason_code.replace('_', ' ')}, "
                f"effective {change.effective_date}."
            ),
            after_value={"change_amount": float(change.total_amount)},
        )
        logger.info(
            "Payroll change %s applied to %s (was set: %s)",
            change.request_number, employee.employee_number,
            before_salary is not None,
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def _refuse_conflicted_approver(
        self, change: PayrollChangeRequest, employee: Employee, current_user: dict
    ) -> None:
        """The three ways an approver can be the wrong person.

        The third is the one that needs explaining: two managers can approve
        each other's rises, and each approval passes every check that looks at
        a single record. Reaching one step up the reporting line closes the
        pair. It does not close a three-way ring, and deliberately so — the
        rule people can predict is worth more than the one that catches every
        arrangement, and a ring is visible in the overrides report.
        """
        if sod._same_person(change.created_by, current_user.get("id")):
            raise PermissionError(
                "You raised this payroll change, so you cannot approve it."
            )

        if self._is_self(employee, current_user):
            raise PermissionError(
                "This payroll change is for you, so you cannot approve it."
            )

        approver_employee = (
            self.db.query(Employee)
            .filter(Employee.user_id == current_user.get("id"))
            .first()
        )
        if (
            approver_employee is not None
            and approver_employee.manager_id is not None
            and approver_employee.manager_id == employee.id
        ):
            raise PermissionError(
                "This payroll change is for your own manager. Approving it "
                "while they approve yours would let two people sign each "
                "other's rises, and each approval would look correct on its "
                "own. Send it a level up."
            )

    def reject(
        self, change_id: UUID, current_user: dict, reason: str
    ) -> PayrollChangeRequest:
        change = self._get(change_id)
        self._require(
            current_user, PERM_APPROVE_PAYROLL_CHANGE, "reject payroll changes"
        )

        if change.current_state != PAY_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted change can be rejected; this is "
                f"{change.current_state}"
            )
        if not reason or not reason.strip():
            raise ValueError("A rejection needs a reason")

        _enter_state(change, PAY_REJECTED)
        change.rejected_reason = reason.strip()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=change.id, action="rejected",
            workflow_type=WORKFLOW_TYPE, workflow_step=PAY_REJECTED,
            comment=reason.strip(),
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def cancel(self, change_id: UUID, current_user: dict) -> PayrollChangeRequest:
        change = self._get(change_id)
        self._require(
            current_user, PERM_REQUEST_PAYROLL_CHANGE, "cancel payroll changes"
        )

        if change.current_state == PAY_APPLIED:
            raise ValueError(
                "An applied change cannot be cancelled. Reverse it with "
                "another change, so the record shows both what happened and "
                "what undid it."
            )
        if change.current_state == PAY_CANCELLED:
            raise ValueError("Already cancelled")

        _enter_state(change, PAY_CANCELLED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=change.id, action="cancelled",
            workflow_type=WORKFLOW_TYPE, workflow_step=PAY_CANCELLED,
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    # --- reading -------------------------------------------------------------

    def list_changes(
        self, current_user: dict, state: Optional[str] = None,
        employee_id: Optional[UUID] = None,
    ) -> List[Dict]:
        self._require(current_user, PERM_VIEW_HR, "view payroll changes")

        query = self.db.query(PayrollChangeRequest)
        if state:
            query = query.filter(PayrollChangeRequest.current_state == state)
        if employee_id:
            query = query.filter(PayrollChangeRequest.employee_id == employee_id)

        may_see_pay = has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        return [
            self.render(change, may_see_pay)
            for change in query.order_by(
                PayrollChangeRequest.created_at.desc()
            ).all()
        ]

    def render(self, change: PayrollChangeRequest, may_see_pay: bool) -> Dict:
        """A change request, with the figures resolved by permission.

        The *size* of the change is shown either way, because an approver
        needs to know whether they are signing off 2% or 40% — and because
        without it the approval threshold is unexplainable. The salaries
        themselves are what stays behind the permission.
        """
        data = {
            "id": change.id,
            "request_number": change.request_number,
            "employee_id": change.employee_id,
            "reason_code": change.reason_code,
            "reason_note": change.reason_note,
            "change_amount": float(change.total_amount or 0),
            "effective_date": change.effective_date,
            "current_state": change.current_state,
            "created_by": change.created_by,
            "approved_by": change.approved_by,
            "created_at": change.created_at,
            "correlation_id": change.correlation_id,
        }
        if may_see_pay:
            data["current_salary"] = (
                float(change.current_salary) if change.current_salary is not None
                else None
            )
            data["new_salary"] = float(change.new_salary)
        else:
            data["current_salary"] = None
            data["new_salary"] = None
        data["compensation_visible"] = may_see_pay
        return data

    # --- helpers -------------------------------------------------------------

    def _get(self, change_id: UUID) -> PayrollChangeRequest:
        change = (
            self.db.query(PayrollChangeRequest)
            .filter(PayrollChangeRequest.id == change_id)
            .first()
        )
        if not change:
            raise ValueError("Payroll change request not found")
        return change

    def _employee(self, employee_id: UUID) -> Employee:
        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError("Employee not found")
        return employee

    @staticmethod
    def _is_self(employee: Employee, current_user: dict) -> bool:
        """Whether this employee record is the person acting.

        Resolved through the optional user link, which is the only connection
        between a login and a person. An employee with no linked account can
        never be "self", and that is correct: somebody with no login is not the
        one making the request.
        """
        return (
            employee.user_id is not None
            and sod._same_person(employee.user_id, current_user.get("id"))
        )

    def _next_number(self) -> str:
        count = self.db.query(PayrollChangeRequest).count()
        return f"PAY-{count + 1:05d}"

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
