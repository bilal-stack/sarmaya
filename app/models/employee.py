"""People, as the organisation employs them.

Build Book, Variant C "HR OS". The first decision, and the one everything else
rests on: **an employee is not a user.**

A `User` is a login — an email, a password, a role, a token version. An
`Employee` is a person the company employs: a cost centre, a manager, a job
title, a start date, a salary. The two overlap for office staff and diverge
everywhere else. A warehouse picker, a driver and a cleaner are employees who
may never sign in; a contractor's login exists for three months against an
employment that never existed at all; and somebody who leaves keeps their
employment history long after their account is revoked.

Modelling them as one record would mean either creating logins for people who
should not have them — an access-control decision made accidentally by HR — or
losing the people who never sign in. So `user_id` is a nullable link, set when
a person happens to also have an account.

The second decision is that **salary and national ID never leave this layer in
the clear.** The Build Book asks for "field-level masking for sensitive fields
where needed (bank accounts, national IDs)", and salary belongs in that
sentence for the same reason: an HR list that renders everybody's pay to
whoever opens it is a data breach with a UI. The columns hold the real values;
what a caller sees is decided by permission at the service boundary, the same
way vendor bank details already work.
"""
from sqlalchemy import (
    Column, String, Date, Numeric, Boolean, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, SoftDeleteMixin

# --- employment status ------------------------------------------------------
#: Where somebody stands with the company. `left` rather than `deleted`: an
#: employee who has gone still appears on last year's payroll and in the audit
#: trail of every approval they gave.
EMP_ACTIVE = "active"
EMP_ON_LEAVE = "on_leave"
EMP_NOTICE = "notice"
EMP_LEFT = "left"

EMPLOYMENT_STATUSES = (EMP_ACTIVE, EMP_ON_LEAVE, EMP_NOTICE, EMP_LEFT)

# --- employment type --------------------------------------------------------
EMP_TYPE_PERMANENT = "permanent"
EMP_TYPE_FIXED_TERM = "fixed_term"
EMP_TYPE_CONTRACTOR = "contractor"
EMP_TYPE_INTERN = "intern"

EMPLOYMENT_TYPES = (
    EMP_TYPE_PERMANENT, EMP_TYPE_FIXED_TERM, EMP_TYPE_CONTRACTOR, EMP_TYPE_INTERN,
)

#: Roles where the Build Book requires background verification evidence before
#: an offer is made. Kept as a flag on the employee record rather than inferred
#: from the job title, because "sensitive" is a judgement somebody makes and
#: has to be able to defend — a regex over job titles would decide it silently
#: and get it wrong on the first unusual title.
SENSITIVE_ROLE_NOTE = (
    "Requires background verification evidence before an offer is issued."
)


class Employee(BaseModel, SoftDeleteMixin):
    """A person the company employs.

    Soft-deletable only. Withdrawing an employee record is a correction of a
    mistake — a duplicate, a wrong entry — not what happens when somebody
    leaves. Leaving is a status change, because the employment happened and
    the record has to keep saying so.
    """
    __tablename__ = "employees"

    OBJECT_TYPE = "employee"
    REFERENCE_FIELD = "employee_number"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    employee_number = Column(String(64), nullable=False, index=True)
    full_name = Column(String(255), nullable=False)

    #: Work email. Not a login — see the module docstring. Nullable because
    #: plenty of employees are never issued one.
    work_email = Column(String(255), nullable=True, index=True)

    #: The account this person signs in with, when they have one. Nullable and
    #: expected to stay that way for most of a real workforce.
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    job_title = Column(String(255), nullable=False)
    employment_type = Column(
        String(30), nullable=False, default=EMP_TYPE_PERMANENT,
    )
    status = Column(String(20), nullable=False, default=EMP_ACTIVE, index=True)

    #: Which part of the organisation carries this person's cost. The same
    #: org units the RBAC scopes use, so "headcount by cost centre" and
    #: "who may see this record" are answered from one structure rather than
    #: two that drift.
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    #: Reporting line. Self-referencing, and deliberately not enforced as
    #: acyclic in the database — the service refuses a cycle, because the
    #: error message is worth more than the constraint.
    manager_id = Column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True, index=True,
    )

    start_date = Column(Date, nullable=False)
    #: Set when somebody leaves. Kept alongside `status` rather than inferred
    #: from it: "left" without a date cannot answer "how many people did we
    #: employ in March", which is the first question a payroll variance asks.
    end_date = Column(Date, nullable=True)

    # --- sensitive ----------------------------------------------------------
    #: Annual base pay. Never rendered without `hr.view_compensation`; see
    #: `EmployeeService` for where that is enforced. Stored in full because
    #: payroll variance is arithmetic on real numbers, not on masked ones.
    base_salary = Column(Numeric(15, 2), nullable=True)
    pay_currency = Column(String(3), nullable=True)

    #: National identifier. Masked on the way out like a bank account, and for
    #: the same reason: it is the field an attacker wants and the field nobody
    #: needs to read in full to do their job.
    national_id = Column(String(64), nullable=True)

    #: Where wages are paid. Masked exactly like a vendor's, because the fraud
    #: is identical — redirect the account, receive the salary.
    bank_account = Column(String(64), nullable=True)

    #: The Build Book's "background verification evidence requirements for
    #: sensitive roles". A judgement recorded by a person, not inferred from
    #: the job title.
    is_sensitive_role = Column(Boolean, nullable=False, default=False)
    background_check_cleared = Column(Boolean, nullable=False, default=False)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    user = relationship("User", backref="employee_records")
    org_unit = relationship("OrgUnit", backref="employees")
    manager = relationship("Employee", remote_side="Employee.id", backref="reports")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "employee_number", name="uq_employees_tenant_number"
        ),
        # "Who works in this cost centre, and are they still here" — the shape
        # every headcount report asks for.
        Index("ix_employees_org_unit_status", "org_unit_id", "status"),
    )

    @property
    def is_current(self) -> bool:
        """Employed right now. `notice` counts: they are still on the payroll
        and still hold whatever access they hold, which is exactly the period
        an offboarding checklist exists to cover."""
        return self.status in (EMP_ACTIVE, EMP_ON_LEAVE, EMP_NOTICE)
