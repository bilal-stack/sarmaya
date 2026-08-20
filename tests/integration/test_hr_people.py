"""Employees, and what a payroll change is allowed to do.

Build Book Variant C. Two things here are worth more than the rest of the
module, and both are the kind that look fine when broken:

  * **Salary, national ID and bank details never leave the service in the
    clear** without `hr.view_compensation`. An HR list that renders everybody's
    pay to whoever opens it is a data breach with a UI, and it looks exactly
    like a working directory.
  * **Nobody signs their own pay rise, and nobody signs their manager's.** The
    first is obvious. The second is the one that matters: two managers
    approving each other's rises pass every check that looks at one record at
    a time, and each approval reads as correct on its own.

The third thing, quieter than both: an employee is not a user. Creating a
person must never create a login, because that would be an access-control
decision made accidentally by HR.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.models.employee import Employee, EMP_LEFT, EMP_ON_LEAVE
from app.models.hr import PAY_APPLIED, PAY_PENDING_APPROVAL, PAY_REASON_PROMOTION
from app.services.employee_service import EmployeeService, SALARY_WITHHELD
from app.services.payroll_change_service import PayrollChangeService

pytestmark = pytest.mark.integration


def _employee(db, tenant, *, number="E-001", name="Sam Staff", salary="80000",
              user_id=None, manager_id=None, national_id="35202-1234567-1"):
    employee = Employee(
        id=uuid.uuid4(), tenant_id=tenant.id, employee_number=number,
        full_name=name, job_title="Analyst", start_date=date(2025, 1, 6),
        base_salary=Decimal(salary) if salary else None, pay_currency="PKR",
        national_id=national_id, bank_account="PK36SCBL0000001123456702",
        user_id=user_id, manager_id=manager_id,
    )
    db.add(employee)
    db.flush()
    return employee


class TestAnEmployeeIsNotAUser:
    def test_creating_an_employee_creates_no_login(self, db, tenant, make_user):
        """Otherwise an HR administrator adding a starter grants system access
        as a side effect of doing their job."""
        from app.models.user import User

        admin = make_user(UserRole.ADMIN)
        before = db.query(User).count()

        EmployeeService(db).create(
            admin, employee_number="E-100", full_name="New Starter",
            job_title="Driver", start_date=date(2026, 9, 1),
        )

        assert db.query(User).count() == before

    def test_an_employee_can_exist_with_no_account(self, db, tenant, make_user):
        """Most of a real workforce. A picker, a driver, a cleaner — employed,
        paid, and never signing in."""
        admin = make_user(UserRole.ADMIN)

        created = EmployeeService(db).create(
            admin, employee_number="E-101", full_name="Warehouse Picker",
            job_title="Picker", start_date=date(2026, 9, 1),
        )

        assert created["user_id"] is None
        assert created["has_login"] is False

    def test_an_account_can_be_linked_afterwards(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant)

        linked = EmployeeService(db).link_user(employee.id, target["id"], admin)

        assert linked["has_login"] is True

    def test_one_account_cannot_belong_to_two_employees(
        self, db, tenant, make_user
    ):
        """Two employment records sharing a login makes "who did this"
        ambiguous on every approval that account gives."""
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        first = _employee(db, tenant, number="E-001")
        second = _employee(db, tenant, number="E-002", name="Other Person")

        EmployeeService(db).link_user(first.id, target["id"], admin)

        with pytest.raises(ValueError, match="already linked"):
            EmployeeService(db).link_user(second.id, target["id"], admin)


class TestSensitiveFieldsAreMasked:
    def test_a_manager_does_not_see_salaries(self, db, tenant, make_user):
        """A manager runs a team and has every reason to open the directory.
        That is not a reason to show them what everybody earns."""
        manager = make_user(UserRole.MANAGER)
        _employee(db, tenant)

        rows = EmployeeService(db).list_employees(manager)

        assert rows[0]["base_salary"] == SALARY_WITHHELD
        assert rows[0]["compensation_visible"] is False

    def test_the_withheld_value_is_not_zero_or_null(self, db, tenant, make_user):
        """Both read as "this person is unpaid", so a report built on a masked
        list would be quietly wrong rather than obviously unavailable."""
        manager = make_user(UserRole.MANAGER)
        _employee(db, tenant)

        row = EmployeeService(db).list_employees(manager)[0]

        assert row["base_salary"] is not None
        assert row["base_salary"] != 0

    def test_national_id_and_bank_details_are_partially_masked(
        self, db, tenant, make_user
    ):
        """Enough survives to confirm the right record; the identifier itself
        does not travel."""
        manager = make_user(UserRole.MANAGER)
        _employee(db, tenant)

        row = EmployeeService(db).list_employees(manager)[0]

        assert row["national_id"] == "••••67-1"
        assert row["bank_account"] == "••••6702"
        assert "1234567" not in str(row["national_id"])

    def test_the_cfo_sees_everything(self, db, tenant, make_user):
        cfo = make_user(UserRole.CFO)
        _employee(db, tenant)

        row = EmployeeService(db).list_employees(cfo)[0]

        assert row["base_salary"] == 80000.0
        assert row["national_id"] == "35202-1234567-1"
        assert row["compensation_visible"] is True

    def test_an_auditor_sees_compensation(self, db, tenant, make_user):
        """Payroll variance and ghost-employee checks are audit questions, and
        they cannot be asked against masked figures."""
        auditor = make_user(UserRole.AUDITOR)
        _employee(db, tenant)

        row = EmployeeService(db).list_employees(auditor)[0]

        assert row["base_salary"] == 80000.0

    def test_the_masked_shape_keeps_the_same_keys(self, db, tenant, make_user):
        """Dropping the fields would make every caller write two branches, and
        the one that forgot would render "undefined" next to somebody's name."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        _employee(db, tenant)

        masked = EmployeeService(db).list_employees(manager)[0]
        full = EmployeeService(db).list_employees(cfo)[0]

        assert set(masked) == set(full)

    def test_a_clerk_cannot_open_the_directory_at_all(
        self, db, tenant, make_user
    ):
        """An AP clerk has no business reading the staff list to do their job."""
        clerk = make_user(UserRole.AP_CLERK)

        with pytest.raises(PermissionError):
            EmployeeService(db).list_employees(clerk)

    def test_setting_a_salary_needs_the_compensation_permission(
        self, db, tenant, make_user
    ):
        """Otherwise anybody who can add an employee can set their pay, which
        makes the read permission decorative."""
        # A role that may manage employees but not see compensation is the
        # risk. Asserted against the guard rather than the current role table,
        # so it still holds if the table changes.
        manager = make_user(UserRole.MANAGER)

        with pytest.raises(PermissionError):
            EmployeeService(db).create(
                manager, employee_number="E-200", full_name="X",
                job_title="Y", start_date=date(2026, 9, 1),
                base_salary=Decimal("100000"),
            )

    def test_pay_never_reaches_the_audit_trail(self, db, tenant, make_user):
        """audit.view is a wider audience than hr.view_compensation. Writing a
        salary into the trail would route around the masking entirely."""
        admin = make_user(UserRole.ADMIN)

        created = EmployeeService(db).create(
            admin, employee_number="E-300", full_name="Paid Person",
            job_title="Analyst", start_date=date(2026, 9, 1),
            base_salary=Decimal("123456"),
        )

        entries = db.query(AuditLog).filter(
            AuditLog.object_id == created["id"]
        ).all()
        blob = " ".join(str(e.after_value) + str(e.comment) for e in entries)
        assert "123456" not in blob


class TestLeavingIsAStatusNotADeletion:
    def test_someone_who_left_keeps_their_record(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)

        EmployeeService(db).set_status(
            employee.id, EMP_LEFT, admin, end_date=date(2026, 8, 31),
        )

        assert db.query(Employee).filter(Employee.id == employee.id).first() is not None

    def test_leaving_requires_an_end_date(self, db, tenant, make_user):
        """Without one, "how many people did we employ in March" has no answer
        — the first thing a payroll variance asks."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)

        with pytest.raises(ValueError, match="end date"):
            EmployeeService(db).set_status(employee.id, EMP_LEFT, admin)

    def test_a_leaver_drops_out_of_the_default_list(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        EmployeeService(db).set_status(
            employee.id, EMP_LEFT, admin, end_date=date(2026, 8, 31),
        )

        assert EmployeeService(db).list_employees(admin) == []
        assert len(EmployeeService(db).list_employees(admin, include_left=True)) == 1

    def test_someone_on_notice_is_still_current(self, db, tenant, make_user):
        """They are on the payroll and still hold their access, which is
        exactly the period an offboarding checklist covers."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)

        EmployeeService(db).set_status(employee.id, EMP_ON_LEAVE, admin)

        db.refresh(employee)
        assert employee.is_current is True


class TestNobodySignsTheirOwnPay:
    def _request(self, db, tenant, employee, actor, new_salary="95000"):
        return PayrollChangeService(db).create(
            actor, employee_id=employee.id, new_salary=Decimal(new_salary),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )

    def test_you_cannot_raise_your_own(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        me = _employee(db, tenant, user_id=manager["id"])

        with pytest.raises(PermissionError, match="for yourself"):
            self._request(db, tenant, me, manager)

    def test_you_cannot_approve_your_own(self, db, tenant, make_user):
        """Even holding the approval permission, and even as an admin: this is
        the control, not a convenience."""
        admin = make_user(UserRole.ADMIN)
        other_admin = make_user(UserRole.ADMIN)
        me = _employee(db, tenant, user_id=admin["id"])
        change = self._request(db, tenant, me, other_admin)
        PayrollChangeService(db).submit(change.id, other_admin)

        with pytest.raises(PermissionError, match="for you"):
            PayrollChangeService(db).approve(change.id, admin)

    def test_you_cannot_approve_the_one_you_raised(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        change = self._request(db, tenant, employee, admin)
        PayrollChangeService(db).submit(change.id, admin)

        with pytest.raises(PermissionError, match="raised this"):
            PayrollChangeService(db).approve(change.id, admin)

    def test_you_cannot_approve_your_own_managers_rise(self, db, tenant, make_user):
        """The subtle one. Two managers approving each other's rises pass every
        check that looks at a single record, and each reads as correct alone."""
        approver_user = make_user(UserRole.ADMIN)
        raiser = make_user(UserRole.ADMIN)

        boss = _employee(db, tenant, number="E-BOSS", name="The Boss")
        _employee(
            db, tenant, number="E-ME", name="Me",
            user_id=approver_user["id"], manager_id=boss.id,
        )

        change = self._request(db, tenant, boss, raiser)
        PayrollChangeService(db).submit(change.id, raiser)

        with pytest.raises(PermissionError, match="your own manager"):
            PayrollChangeService(db).approve(change.id, approver_user)

    def test_an_unrelated_approver_is_fine(self, db, tenant, make_user):
        """The rule has to permit the ordinary case, or people route around
        it."""
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant)

        change = self._request(db, tenant, employee, manager)
        PayrollChangeService(db).submit(change.id, manager)
        approved = PayrollChangeService(db).approve(change.id, cfo)

        assert approved.current_state == PAY_APPLIED

    def test_a_manager_cannot_approve_at_all(self, db, tenant, make_user):
        """The permission split: requesting and approving a pay change are
        separate grants, so no arrangement of roles collapses them."""
        manager = make_user(UserRole.MANAGER)
        other_manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant)

        change = self._request(db, tenant, employee, manager)
        PayrollChangeService(db).submit(change.id, manager)

        with pytest.raises(PermissionError, match="does not have permission"):
            PayrollChangeService(db).approve(change.id, other_manager)


class TestApplyingTheChange:
    def _approved(self, db, tenant, make_user, new_salary="95000"):
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, salary="80000")
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal(new_salary),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        service.submit(change.id, manager)
        return service, change, employee, cfo, manager

    def test_the_salary_moves_only_on_approval(self, db, tenant, make_user):
        service, change, employee, cfo, _ = self._approved(db, tenant, make_user)

        assert employee.base_salary == Decimal("80000")

        service.approve(change.id, cfo)

        db.refresh(employee)
        assert employee.base_salary == Decimal("95000")

    def test_the_size_of_the_change_is_recorded(self, db, tenant, make_user):
        service, change, _, cfo, _ = self._approved(db, tenant, make_user)

        assert change.total_amount == Decimal("15000")

    def test_a_cut_is_measured_the_same_way(self, db, tenant, make_user):
        """Absolute, because a cut of 200k deserves the same scrutiny as a rise
        — both are large changes to somebody's pay and both can be a mistake."""
        service, change, _, cfo, _ = self._approved(
            db, tenant, make_user, new_salary="60000",
        )

        assert change.total_amount == Decimal("20000")

    def test_the_employees_own_trail_shows_it(self, db, tenant, make_user):
        """So reading one person's history shows their pay changing, without
        having to know that payroll changes are a separate record."""
        service, change, employee, cfo, _ = self._approved(db, tenant, make_user)
        service.approve(change.id, cfo)

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == employee.id,
            AuditLog.action == "salary_changed",
        ).first()
        assert entry is not None

    def test_an_applied_change_cannot_be_cancelled(self, db, tenant, make_user):
        """Reverse it with another change, so the record shows both what
        happened and what undid it."""
        service, change, _, cfo, manager = self._approved(db, tenant, make_user)
        service.approve(change.id, cfo)

        with pytest.raises(ValueError, match="cannot be cancelled"):
            service.cancel(change.id, manager)

    def test_the_request_reads_without_salaries_for_the_wrong_role(
        self, db, tenant, make_user
    ):
        """The *size* of the change is shown either way — an approver has to
        know whether they are signing 2% or 40%, and without it the threshold
        is unexplainable. The salaries stay behind the permission."""
        service, change, _, cfo, manager = self._approved(db, tenant, make_user)

        rows = service.list_changes(manager)

        assert rows[0]["change_amount"] == 15000.0
        assert rows[0]["new_salary"] is None
        assert rows[0]["compensation_visible"] is False

    def test_the_request_reaches_an_approver_on_arrival(
        self, db, tenant, make_user
    ):
        """Not only when it breaches its SLA."""
        from app.models.notification_outbox import NotificationOutbox

        make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant)
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal("90000"),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        before = db.query(NotificationOutbox).count()

        service.submit(change.id, manager)

        assert db.query(NotificationOutbox).count() > before
        assert change.current_state == PAY_PENDING_APPROVAL
