"""Asking to hire, and what it costs.

Build Book, Variant C1: "headcount request, approvals, hiring pipeline
checkpoints, offer approvals", with the control "budget and headcount policy
checks against role and department".

A hire is agreed once and paid for years, which is what makes this worth a
workflow rather than a form. The specific control is that a request states its
annual cost up front, so what somebody approves is a number rather than a job
title — a role approved without a cost is approved without anybody having
decided what it costs.

Two states that look redundant and are not:

  * **`approved` vs `filled`.** An approved request that has been hired against
    must not authorise a second hire. Same reasoning that makes a requisition
    terminal once converted: one approval, one thing.
  * **The sensitive-role gate sits at `filled`, not at approval.** The Build
    Book asks for "background verification evidence requirements for sensitive
    roles"; verification happens to a *person*, and at approval time there is
    no person yet. Checking it when the request is filled is the first moment
    the question can be asked at all.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_REQUEST_HEADCOUNT, PERM_APPROVE_HEADCOUNT, PERM_VIEW_HR,
)
from app.models.employee import Employee, EMP_LEFT
from app.models.hr import (
    HeadcountRequest, HC_DRAFT, HC_PENDING_APPROVAL, HC_APPROVED, HC_FILLED,
    HC_REJECTED, HC_CANCELLED,
)
from app.services import sod
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.workflow import _enter_state
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "headcount_request"
WORKFLOW_TYPE = "headcount_request"


def _now():
    return make_naive(to_utc(utc_now()))


class HeadcountService:
    def __init__(self, db: Session):
        self.db = db

    # --- creating ------------------------------------------------------------

    def create(
        self, current_user: dict, *, job_title: str, annual_cost: Decimal,
        positions: int = 1, org_unit_id: Optional[UUID] = None,
        employment_type: str = "permanent", is_sensitive_role: bool = False,
        justification: Optional[str] = None, target_start_date=None,
    ) -> HeadcountRequest:
        self._require(current_user, PERM_REQUEST_HEADCOUNT, "request headcount")

        if not (job_title or "").strip():
            raise ValueError("A headcount request needs a job title")
        if positions < 1:
            raise ValueError("A headcount request is for at least one position")

        annual_cost = Decimal(str(annual_cost or 0))
        if annual_cost <= 0:
            raise ValueError(
                "A headcount request needs an annual cost. Approving a role "
                "without one is approving a job title while nobody has decided "
                "what it costs."
            )

        request = HeadcountRequest(
            tenant_id=current_user["tenant_id"],
            request_number=self._next_number(),
            job_title=job_title.strip(),
            org_unit_id=org_unit_id,
            positions=positions,
            employment_type=employment_type,
            is_sensitive_role=is_sensitive_role,
            annual_cost=annual_cost,
            total_amount=annual_cost * positions,
            justification=justification,
            target_start_date=target_start_date,
            current_state=HC_DRAFT,
            created_by=current_user["id"],
            correlation_id=uuid4(),
        )
        self.db.add(request)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="created",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_DRAFT,
            after_value={
                "request_number": request.request_number,
                "job_title": request.job_title,
                "positions": positions,
                "total_amount": float(request.total_amount),
                "is_sensitive_role": is_sensitive_role,
            },
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    # --- the workflow --------------------------------------------------------

    def submit(self, request_id: UUID, current_user: dict) -> HeadcountRequest:
        request = self._get(request_id)
        self._require(current_user, PERM_REQUEST_HEADCOUNT, "submit headcount requests")

        if request.current_state != HC_DRAFT:
            raise ValueError(
                f"Only a draft can be submitted; this is {request.current_state}"
            )
        if not (request.justification or "").strip():
            raise ValueError(
                "A headcount request needs a justification before it goes for "
                "approval. An approver deciding on a job title and a number is "
                "deciding with nothing to go on."
            )

        _enter_state(request, HC_PENDING_APPROVAL)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="submitted",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_PENDING_APPROVAL,
            comment=f"{request.positions} position(s), {request.total_amount} a year.",
        )
        NotificationService(self.db).notify_awaiting_action(
            request, PERM_APPROVE_HEADCOUNT, "approve or reject",
            exclude_user_id=request.created_by,
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    def approve(self, request_id: UUID, current_user: dict) -> HeadcountRequest:
        request = self._get(request_id)
        self._require(current_user, PERM_APPROVE_HEADCOUNT, "approve headcount requests")

        if request.current_state != HC_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted request can be approved; this is "
                f"{request.current_state}"
            )
        if sod._same_person(request.created_by, current_user.get("id")):
            raise PermissionError(
                "You raised this headcount request, so you cannot approve it."
            )

        _enter_state(request, HC_APPROVED)
        request.approved_by = current_user["id"]
        request.approved_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="approved",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_APPROVED,
            after_value={"total_amount": float(request.total_amount)},
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    def reject(
        self, request_id: UUID, current_user: dict, reason: str
    ) -> HeadcountRequest:
        request = self._get(request_id)
        self._require(current_user, PERM_APPROVE_HEADCOUNT, "reject headcount requests")

        if request.current_state != HC_PENDING_APPROVAL:
            raise ValueError(
                f"Only a submitted request can be rejected; this is "
                f"{request.current_state}"
            )
        if not reason or not reason.strip():
            raise ValueError("A rejection needs a reason")

        _enter_state(request, HC_REJECTED)
        request.rejected_reason = reason.strip()
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="rejected",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_REJECTED,
            comment=reason.strip(),
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    def mark_filled(
        self, request_id: UUID, employee_id: UUID, current_user: dict
    ) -> HeadcountRequest:
        """Record who was hired against this request.

        Where the background-verification gate lives, because verification
        happens to a person and at approval time there was no person yet.
        """
        request = self._get(request_id)
        self._require(current_user, PERM_REQUEST_HEADCOUNT, "fill headcount requests")

        if request.current_state != HC_APPROVED:
            raise ValueError(
                f"Only an approved request can be filled; this is "
                f"{request.current_state}"
            )

        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError("Employee not found")

        # Build Book: "background verification evidence requirements for
        # sensitive roles". Checked at the moment somebody is actually placed
        # into the role, which is the first point the question has an answer.
        if request.is_sensitive_role and not employee.background_check_cleared:
            raise ValueError(
                f"{employee.employee_number} has no cleared background check, "
                "and this role was raised as sensitive. Record the verification "
                "before filling it."
            )

        _enter_state(request, HC_FILLED)
        request.filled_by_employee_id = employee.id
        request.filled_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="filled",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_FILLED,
            after_value={"employee": employee.employee_number},
            comment=(
                "Terminal: an approved request that has been hired against "
                "cannot authorise a second hire."
            ),
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    def cancel(self, request_id: UUID, current_user: dict) -> HeadcountRequest:
        request = self._get(request_id)
        self._require(current_user, PERM_REQUEST_HEADCOUNT, "cancel headcount requests")

        if request.current_state == HC_FILLED:
            raise ValueError(
                "A filled request cannot be cancelled — somebody was hired "
                "against it. End their employment instead, which is a different "
                "and much more deliberate act."
            )

        _enter_state(request, HC_CANCELLED)
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=request.id, action="cancelled",
            workflow_type=WORKFLOW_TYPE, workflow_step=HC_CANCELLED,
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    # --- reading -------------------------------------------------------------

    def list_requests(
        self, current_user: dict, state: Optional[str] = None,
        org_unit_id: Optional[UUID] = None,
    ) -> List[HeadcountRequest]:
        self._require(current_user, PERM_VIEW_HR, "view headcount requests")

        query = self.db.query(HeadcountRequest)
        if state:
            query = query.filter(HeadcountRequest.current_state == state)
        if org_unit_id:
            query = query.filter(HeadcountRequest.org_unit_id == org_unit_id)
        return query.order_by(HeadcountRequest.created_at.desc()).all()

    def plan_versus_actual(self, current_user: dict) -> Dict:
        """Build Book report: headcount plan vs actual.

        "Plan" is what has been approved and not yet filled; "actual" is who is
        on the books. Open approvals are the number that matters — an approved
        role nobody has hired is committed cost the budget already carries and
        the headcount does not show.
        """
        self._require(current_user, PERM_VIEW_HR, "view headcount reports")

        approved_open = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_APPROVED)
            .all()
        )
        filled = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_FILLED)
            .count()
        )
        awaiting = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_PENDING_APPROVAL)
            .all()
        )
        current_staff = (
            self.db.query(Employee)
            .filter(Employee.status != EMP_LEFT)
            .count()
        )

        return {
            "employees_on_the_books": current_staff,
            "approved_not_yet_filled": len(approved_open),
            "committed_annual_cost": round(
                sum(float(r.total_amount or 0) for r in approved_open), 2
            ),
            "requests_awaiting_approval": len(awaiting),
            "value_awaiting_approval": round(
                sum(float(r.total_amount or 0) for r in awaiting), 2
            ),
            "filled_to_date": filled,
            "open_positions": [
                {
                    "request_number": r.request_number,
                    "job_title": r.job_title,
                    "positions": r.positions,
                    "annual_cost": float(r.annual_cost or 0),
                    "target_start_date": r.target_start_date,
                    "approved_at": r.approved_at,
                }
                for r in sorted(
                    approved_open, key=lambda r: r.approved_at or _now()
                )
            ],
        }

    # --- helpers -------------------------------------------------------------

    def _get(self, request_id: UUID) -> HeadcountRequest:
        request = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.id == request_id)
            .first()
        )
        if not request:
            raise ValueError("Headcount request not found")
        return request

    def _next_number(self) -> str:
        count = self.db.query(HeadcountRequest).count()
        return f"HC-{count + 1:05d}"

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
