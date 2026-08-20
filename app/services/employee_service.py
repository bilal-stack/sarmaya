"""The staff record, and who is allowed to see what is in it.

Build Book, Variant C, plus "field-level masking for sensitive fields where
needed (bank accounts, national IDs)".

Most of an employee record is ordinary directory data — name, job title, who
they report to, which cost centre carries them. Three fields are not: salary,
national ID and bank account. Those are rendered only to a caller holding
`hr.view_compensation`, and masked for everybody else.

**Masking happens here, on the way out, not in the column.** Payroll variance,
headcount cost and every budget check are arithmetic on real numbers, so the
stored values have to be real. What changes by permission is what leaves the
service — which is exactly how vendor bank details already work, and the reason
that pattern is repeated rather than reinvented.

The rule worth stating plainly: **an employee is not a user.** Linking one is
optional and reversible, and creating an employee never creates a login. An HR
administrator adding a new starter must not be able to grant system access as a
side effect of doing their job — that is an access-control decision, and it
belongs with the permission that grants access.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_VIEW_HR, PERM_MANAGE_EMPLOYEES, PERM_VIEW_COMPENSATION,
)
from app.models.employee import (
    Employee, EMPLOYMENT_STATUSES, EMPLOYMENT_TYPES, EMP_ACTIVE, EMP_LEFT,
)
from app.models.user import User
from app.services.audit import log_audit
from app.utils.masking import mask_account
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "employee"

#: What a caller without `hr.view_compensation` sees in place of a salary.
#: Not zero and not null: both read as "this person is unpaid", and a report
#: built on a masked list would then be quietly wrong rather than obviously
#: unavailable.
SALARY_WITHHELD = "restricted"


def _now():
    return make_naive(to_utc(utc_now()))


class EmployeeService:
    def __init__(self, db: Session):
        self.db = db

    # --- reading -------------------------------------------------------------

    def list_employees(
        self, current_user: dict, *, org_unit_id: Optional[UUID] = None,
        include_left: bool = False,
    ) -> List[Dict]:
        self._require(current_user, PERM_VIEW_HR, "view employees")

        query = self.db.query(Employee)
        if org_unit_id:
            query = query.filter(Employee.org_unit_id == org_unit_id)
        if not include_left:
            query = query.filter(Employee.status != EMP_LEFT)

        may_see_pay = has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        return [
            self._render(employee, may_see_pay)
            for employee in query.order_by(Employee.full_name).all()
        ]

    def get(self, employee_id: UUID, current_user: dict) -> Dict:
        self._require(current_user, PERM_VIEW_HR, "view employees")
        employee = self._get(employee_id)
        return self._render(
            employee, has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        )

    def _render(self, employee: Employee, may_see_pay: bool) -> Dict:
        """One employee, with the sensitive fields resolved by permission.

        The masked shape keeps the same keys as the unmasked one. A response
        that dropped the fields entirely would make every caller write two
        branches, and the one that forgot would render `undefined` next to
        somebody's name.
        """
        data = {
            "id": employee.id,
            "employee_number": employee.employee_number,
            "full_name": employee.full_name,
            "work_email": employee.work_email,
            "job_title": employee.job_title,
            "employment_type": employee.employment_type,
            "status": employee.status,
            "org_unit_id": employee.org_unit_id,
            "manager_id": employee.manager_id,
            "start_date": employee.start_date,
            "end_date": employee.end_date,
            "user_id": employee.user_id,
            "has_login": employee.user_id is not None,
            "is_sensitive_role": employee.is_sensitive_role,
            "background_check_cleared": employee.background_check_cleared,
            "compensation_visible": may_see_pay,
        }

        if may_see_pay:
            data["base_salary"] = (
                float(employee.base_salary) if employee.base_salary is not None else None
            )
            data["pay_currency"] = employee.pay_currency
            data["national_id"] = employee.national_id
            data["bank_account"] = employee.bank_account
        else:
            data["base_salary"] = SALARY_WITHHELD
            data["pay_currency"] = employee.pay_currency
            # Partially masked rather than withheld: enough survives to confirm
            # you are looking at the right record without carrying the
            # identifier itself, which is the same trade vendor bank details
            # already make.
            data["national_id"] = mask_account(employee.national_id)
            data["bank_account"] = mask_account(employee.bank_account)

        return data

    # --- writing -------------------------------------------------------------

    def create(
        self, current_user: dict, *, employee_number: str, full_name: str,
        job_title: str, start_date, employment_type: str = "permanent",
        work_email: Optional[str] = None, org_unit_id: Optional[UUID] = None,
        manager_id: Optional[UUID] = None,
        base_salary: Optional[Decimal] = None, pay_currency: Optional[str] = None,
        national_id: Optional[str] = None, bank_account: Optional[str] = None,
        is_sensitive_role: bool = False, user_id: Optional[UUID] = None,
    ) -> Dict:
        self._require(current_user, PERM_MANAGE_EMPLOYEES, "manage employees")

        employee_number = (employee_number or "").strip()
        if not employee_number:
            raise ValueError("An employee needs an employee number")
        if not (full_name or "").strip():
            raise ValueError("An employee needs a name")
        if not (job_title or "").strip():
            raise ValueError("An employee needs a job title")
        if employment_type not in EMPLOYMENT_TYPES:
            raise ValueError(
                f"{employment_type!r} is not an employment type. One of: "
                f"{', '.join(EMPLOYMENT_TYPES)}"
            )
        if self.db.query(Employee).filter(
            Employee.employee_number == employee_number
        ).first():
            raise ValueError(
                f"An employee with number {employee_number!r} already exists"
            )

        # Setting a salary is a compensation action, whoever is doing it.
        if base_salary is not None and not has_permission(
            current_user["role"], PERM_VIEW_COMPENSATION
        ):
            raise PermissionError(
                "Setting a salary needs permission to see compensation. "
                "Create the employee without one and have somebody who holds "
                "that permission set it."
            )

        if manager_id:
            self._require_exists(manager_id, "Manager")
        if user_id:
            self._link_check(user_id)

        employee = Employee(
            tenant_id=current_user["tenant_id"],
            employee_number=employee_number,
            full_name=full_name.strip(),
            work_email=work_email,
            job_title=job_title.strip(),
            employment_type=employment_type,
            status=EMP_ACTIVE,
            org_unit_id=org_unit_id,
            manager_id=manager_id,
            start_date=start_date,
            base_salary=base_salary,
            pay_currency=pay_currency,
            national_id=national_id,
            bank_account=bank_account,
            is_sensitive_role=is_sensitive_role,
            user_id=user_id,
            correlation_id=uuid4(),
        )
        self.db.add(employee)
        self.db.flush()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=employee.id, action="created",
            # Deliberately no salary in the trail. The audit log is readable by
            # anyone with audit.view, which is a wider audience than
            # hr.view_compensation — writing pay into it would route around the
            # masking this service exists to enforce.
            after_value={
                "employee_number": employee.employee_number,
                "full_name": employee.full_name,
                "job_title": employee.job_title,
                "employment_type": employee.employment_type,
                "has_salary": base_salary is not None,
                "is_sensitive_role": is_sensitive_role,
            },
        )
        self.db.commit()
        self.db.refresh(employee)
        return self._render(employee, True)

    def link_user(
        self, employee_id: UUID, user_id: Optional[UUID], current_user: dict
    ) -> Dict:
        """Attach or detach the login this person signs in with.

        A separate action from creating the employee, because it is an
        access-control change rather than a personnel one — and because most
        employees never need one.
        """
        self._require(current_user, PERM_MANAGE_EMPLOYEES, "manage employees")

        employee = self._get(employee_id)
        before = employee.user_id

        if user_id is not None:
            self._link_check(user_id, exclude_employee_id=employee_id)

        employee.user_id = user_id
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=employee.id,
            action="user_linked" if user_id else "user_unlinked",
            before_value={"user_id": str(before) if before else None},
            after_value={"user_id": str(user_id) if user_id else None},
            comment=(
                "Linking an account does not grant it anything: the role on "
                "the account still decides what it may do."
            ),
        )
        self.db.commit()
        self.db.refresh(employee)
        return self._render(
            employee, has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        )

    def set_status(
        self, employee_id: UUID, status: str, current_user: dict,
        end_date=None,
    ) -> Dict:
        """Move somebody between active, on leave, notice and left.

        Leaving is a status change, never a deletion: the employment happened,
        and last year's payroll and every approval they gave still point here.
        """
        self._require(current_user, PERM_MANAGE_EMPLOYEES, "manage employees")

        if status not in EMPLOYMENT_STATUSES:
            raise ValueError(
                f"{status!r} is not an employment status. One of: "
                f"{', '.join(EMPLOYMENT_STATUSES)}"
            )

        employee = self._get(employee_id)
        before = employee.status

        if status == EMP_LEFT and end_date is None:
            raise ValueError(
                "Somebody who has left needs an end date. Without one, "
                "'how many people did we employ in March' has no answer, which "
                "is the first thing a payroll variance asks."
            )

        employee.status = status
        if end_date is not None:
            employee.end_date = end_date

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=employee.id, action=f"status_{status}",
            before_value={"status": before},
            after_value={"status": status, "end_date": str(end_date) if end_date else None},
        )
        self.db.commit()
        self.db.refresh(employee)
        return self._render(
            employee, has_permission(current_user["role"], PERM_VIEW_COMPENSATION)
        )

    # --- helpers -------------------------------------------------------------

    def _get(self, employee_id: UUID) -> Employee:
        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError("Employee not found")
        return employee

    def _require_exists(self, employee_id: UUID, label: str) -> Employee:
        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError(f"{label} not found")
        return employee

    def _link_check(
        self, user_id: UUID, exclude_employee_id: Optional[UUID] = None
    ) -> None:
        """One account belongs to at most one employee.

        Two employment records sharing a login would make "who did this"
        ambiguous on every approval that account gives — which is the one
        question the audit trail exists to answer.
        """
        if not self.db.query(User).filter(User.id == user_id).first():
            raise ValueError("User not found")

        query = self.db.query(Employee).filter(Employee.user_id == user_id)
        if exclude_employee_id:
            query = query.filter(Employee.id != exclude_employee_id)
        existing = query.first()
        if existing:
            raise ValueError(
                f"That account is already linked to {existing.employee_number}. "
                "One login belongs to one employee, or every approval it gives "
                "is ambiguous."
            )

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
