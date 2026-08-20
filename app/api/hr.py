"""HR: people, hires, onboarding, pay changes and expense claims.

Build Book Variant C. One router, because these are one module — a headcount
request is filled by an employee, an onboarding checklist hangs off that
employee, and a pay change is about them.

The thing to know before reading: **the employee endpoints never return a
salary to a caller without `hr.view_compensation`.** That is enforced in the
service, not here, so a second caller added later cannot skip it by accident.
The API's job is to pass the caller through and let the service decide.
"""
from datetime import date
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.models.hr import FLOW_ONBOARDING
from app.services.employee_service import EmployeeService
from app.services.expense_service import ExpenseService
from app.services.headcount_service import HeadcountService
from app.services.onboarding_service import OnboardingService
from app.services.payroll_change_service import PayrollChangeService

router = APIRouter(prefix="/hr", tags=["HR"])


def _raise_for(exc: Exception):
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --- schemas ---------------------------------------------------------------

class EmployeeCreate(BaseModel):
    employee_number: str
    full_name: str
    job_title: str
    start_date: date
    employment_type: str = "permanent"
    work_email: Optional[str] = None
    org_unit_id: Optional[UUID] = None
    manager_id: Optional[UUID] = None
    #: Accepted, but the service refuses it unless the caller may see
    #: compensation — otherwise anybody who can add a person could set pay.
    base_salary: Optional[Decimal] = None
    pay_currency: Optional[str] = None
    national_id: Optional[str] = None
    bank_account: Optional[str] = None
    is_sensitive_role: bool = False


class LinkUserIn(BaseModel):
    #: Null detaches the account. Detaching is as much an access-control event
    #: as attaching, so it goes through the same endpoint and the same trail.
    user_id: Optional[UUID] = None


class StatusIn(BaseModel):
    status: str
    end_date: Optional[date] = None


class HeadcountCreate(BaseModel):
    job_title: str
    annual_cost: Decimal
    positions: int = Field(default=1, ge=1)
    org_unit_id: Optional[UUID] = None
    employment_type: str = "permanent"
    is_sensitive_role: bool = False
    justification: Optional[str] = None
    target_start_date: Optional[date] = None


class FilledIn(BaseModel):
    employee_id: UUID


class ReasonIn(BaseModel):
    reason: str


class ChecklistIn(BaseModel):
    flow: str = FLOW_ONBOARDING
    extra_tasks: Optional[List[dict]] = None


class TaskStatusIn(BaseModel):
    status: str
    note: Optional[str] = None


class PayrollChangeCreate(BaseModel):
    employee_id: UUID
    new_salary: Decimal
    reason_code: str
    effective_date: date
    reason_note: Optional[str] = None


class ExpenseCreate(BaseModel):
    employee_id: UUID
    category: str
    total_amount: Decimal
    incurred_date: date
    description: Optional[str] = None
    currency: Optional[str] = None
    org_unit_id: Optional[UUID] = None


class ApproveExpenseIn(BaseModel):
    #: Waiving the receipt rule needs a written reason, which is recorded and
    #: reported as a policy override.
    override_reason: Optional[str] = None


# --- employees -------------------------------------------------------------

@router.get("/employees")
def list_employees(
    org_unit_id: Optional[UUID] = None,
    include_left: bool = False,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The staff list.

    Salary, national ID and bank details are masked unless the caller holds
    `hr.view_compensation`. The keys are present either way, so a client never
    has to branch on which shape it received.
    """
    try:
        return EmployeeService(db).list_employees(
            current_user, org_unit_id=org_unit_id, include_left=include_left
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/employees/{employee_id}")
def get_employee(
    employee_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return EmployeeService(db).get(employee_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(
    payload: EmployeeCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Add a person. Creates no login — see `POST /employees/{id}/user`."""
    try:
        return EmployeeService(db).create(current_user, **payload.model_dump())
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/employees/{employee_id}/user")
def link_user(
    employee_id: UUID,
    payload: LinkUserIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Attach or detach the account this person signs in with.

    Separate from creating the employee on purpose: linking an account is an
    access-control change, and an HR administrator adding a starter must not
    grant system access as a side effect. Linking grants nothing by itself —
    the role on the account still decides what it may do.
    """
    try:
        return EmployeeService(db).link_user(
            employee_id, payload.user_id, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/employees/{employee_id}/status")
def set_employee_status(
    employee_id: UUID,
    payload: StatusIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Move somebody between active, on leave, notice and left.

    Leaving is a status change, never a deletion: the employment happened, and
    last year's payroll still points here. `left` requires an end date.
    """
    try:
        return EmployeeService(db).set_status(
            employee_id, payload.status, current_user, end_date=payload.end_date
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- headcount -------------------------------------------------------------

@router.get("/headcount")
def list_headcount(
    state: Optional[str] = None,
    org_unit_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            _headcount_dict(r)
            for r in HeadcountService(db).list_requests(
                current_user, state=state, org_unit_id=org_unit_id
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/headcount", status_code=status.HTTP_201_CREATED)
def create_headcount(
    payload: HeadcountCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return _headcount_dict(
            HeadcountService(db).create(current_user, **payload.model_dump())
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/headcount/{request_id}/{action}")
def act_on_headcount(
    request_id: UUID,
    action: str,
    payload: Optional[dict] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """submit | approve | reject | fill | cancel."""
    service = HeadcountService(db)
    payload = payload or {}
    try:
        if action == "submit":
            result = service.submit(request_id, current_user)
        elif action == "approve":
            result = service.approve(request_id, current_user)
        elif action == "reject":
            result = service.reject(request_id, current_user, payload.get("reason", ""))
        elif action == "fill":
            result = service.mark_filled(
                request_id, payload.get("employee_id"), current_user
            )
        elif action == "cancel":
            result = service.cancel(request_id, current_user)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unknown action. One of: submit, approve, reject, fill, cancel",
            )
        return _headcount_dict(result)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/headcount-plan")
def headcount_plan(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Plan versus actual. Approved-and-unfilled is the number that matters —
    committed cost the budget carries and the headcount does not show."""
    try:
        return HeadcountService(db).plan_versus_actual(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- onboarding ------------------------------------------------------------

@router.get("/employees/{employee_id}/tasks")
def employee_tasks(
    employee_id: UUID,
    flow: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return [
            _task_dict(t)
            for t in OnboardingService(db).tasks_for(
                employee_id, current_user, flow=flow
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/employees/{employee_id}/checklist", status_code=status.HTTP_201_CREATED)
def create_checklist(
    employee_id: UUID,
    payload: ChecklistIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Raise the onboarding — or offboarding — checklist for one person."""
    try:
        return [
            _task_dict(t)
            for t in OnboardingService(db).create_checklist(
                employee_id, current_user,
                flow=payload.flow, extra_tasks=payload.extra_tasks,
            )
        ]
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/tasks/{task_id}/status")
def set_task_status(
    task_id: UUID,
    payload: TaskStatusIn,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Move a task along. Skipping or blocking requires a note."""
    try:
        return _task_dict(
            OnboardingService(db).set_status(
                task_id, payload.status, current_user, note=payload.note
            )
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/outstanding-access")
def outstanding_access(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Who has left and can still sign in.

    The question an auditor asks directly, and the reason onboarding and
    offboarding share one engine.
    """
    try:
        return OnboardingService(db).outstanding_access(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/onboarding-completion")
def onboarding_completion(
    flow: str = FLOW_ONBOARDING,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return OnboardingService(db).completion(current_user, flow=flow)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- payroll changes -------------------------------------------------------

@router.get("/payroll-changes")
def list_payroll_changes(
    state: Optional[str] = None,
    employee_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Pay changes. The *size* of each change is always shown; the salaries
    themselves only to a caller who may see compensation."""
    try:
        return PayrollChangeService(db).list_changes(
            current_user, state=state, employee_id=employee_id
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/payroll-changes", status_code=status.HTTP_201_CREATED)
def create_payroll_change(
    payload: PayrollChangeCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        service = PayrollChangeService(db)
        change = service.create(current_user, **payload.model_dump())
        return service.render(change, True)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/payroll-changes/{change_id}/{action}")
def act_on_payroll_change(
    change_id: UUID,
    action: str,
    payload: Optional[dict] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """submit | approve | reject | cancel.

    Approving applies the new salary in the same transaction. A change that
    approved but failed to apply would be paperwork saying somebody got a rise
    that never reached their pay.
    """
    from app.core.roles import has_permission, PERM_VIEW_COMPENSATION

    service = PayrollChangeService(db)
    payload = payload or {}
    try:
        if action == "submit":
            result = service.submit(change_id, current_user)
        elif action == "approve":
            result = service.approve(change_id, current_user)
        elif action == "reject":
            result = service.reject(change_id, current_user, payload.get("reason", ""))
        elif action == "cancel":
            result = service.cancel(change_id, current_user)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unknown action. One of: submit, approve, reject, cancel",
            )
        return service.render(
            result, has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- expenses --------------------------------------------------------------

@router.get("/expenses")
def list_expenses(
    state: Optional[str] = None,
    employee_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Claims. Somebody who can only claim sees their own — an expense list is
    a record of where people went and what they bought."""
    try:
        return ExpenseService(db).list_claims(
            current_user, state=state, employee_id=employee_id
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/expenses", status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        service = ExpenseService(db)
        return service.render(service.create(current_user, **payload.model_dump()))
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/expenses/{claim_id}/approve")
def approve_expense(
    claim_id: UUID,
    payload: Optional[ApproveExpenseIn] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Approve a claim, waiving the receipt rule only with a written reason."""
    try:
        service = ExpenseService(db)
        return service.render(
            service.approve(
                claim_id, current_user,
                override_reason=(payload.override_reason if payload else None),
            )
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/expenses/{claim_id}/{action}")
def act_on_expense(
    claim_id: UUID,
    action: str,
    payload: Optional[dict] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """submit | reject | pay | cancel. Approval has its own route above,
    because waiving a policy rule takes a body nothing else here needs."""
    service = ExpenseService(db)
    payload = payload or {}
    try:
        if action == "submit":
            result = service.submit(claim_id, current_user)
        elif action == "reject":
            result = service.reject(claim_id, current_user, payload.get("reason", ""))
        elif action == "pay":
            result = service.mark_paid(claim_id, current_user)
        elif action == "cancel":
            result = service.cancel(claim_id, current_user)
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Unknown action. One of: submit, reject, pay, cancel",
            )
        return service.render(result)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# --- rendering -------------------------------------------------------------

def _headcount_dict(request) -> dict:
    return {
        "id": request.id,
        "request_number": request.request_number,
        "job_title": request.job_title,
        "org_unit_id": request.org_unit_id,
        "positions": request.positions,
        "employment_type": request.employment_type,
        "is_sensitive_role": request.is_sensitive_role,
        "annual_cost": float(request.annual_cost or 0),
        "total_amount": float(request.total_amount or 0),
        "justification": request.justification,
        "target_start_date": request.target_start_date,
        "current_state": request.current_state,
        "created_by": request.created_by,
        "approved_by": request.approved_by,
        "filled_by_employee_id": request.filled_by_employee_id,
        "created_at": request.created_at,
        "correlation_id": request.correlation_id,
    }


def _task_dict(task) -> dict:
    return {
        "id": task.id,
        "employee_id": task.employee_id,
        "flow": task.flow,
        "category": task.category,
        "title": task.title,
        "detail": task.detail,
        "owning_team": task.owning_team,
        "status": task.status,
        "due_date": task.due_date,
        "completed_at": task.completed_at,
        "resolution_note": task.resolution_note,
    }
