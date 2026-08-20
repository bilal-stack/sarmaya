"""The HR API, the reports, and HR's arrival in the Decision Inbox.

The API tests exist because the service tests cannot see how an endpoint is
*wired*. The one that matters most here is masking: a service that masks
correctly is worth nothing if the route serialises the model instead of the
service's output, and that mistake looks identical to working code until
somebody reads a response.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import UserRole
from app.models.employee import Employee
from app.models.hr import PAY_REASON_PROMOTION
from app.services.dashboards import DashboardService
from app.services.decision_inbox_service import DecisionInboxService
from app.services.expense_service import ExpenseService
from app.services.headcount_service import HeadcountService
from app.services.payroll_change_service import PayrollChangeService

pytestmark = pytest.mark.integration


def _employee(db, tenant, *, number="E-001", name="Sam Staff", user_id=None,
              salary="80000"):
    employee = Employee(
        id=uuid.uuid4(), tenant_id=tenant.id, employee_number=number,
        full_name=name, job_title="Analyst", start_date=date(2026, 1, 6),
        base_salary=Decimal(salary), pay_currency="PKR",
        national_id="35202-1234567-1", bank_account="PK36SCBL0000001123456702",
        user_id=user_id, correlation_id=uuid.uuid4(),
    )
    db.add(employee)
    db.flush()
    return employee


class TestMaskingHoldsAtTheEdge:
    def test_the_endpoint_masks_for_a_manager(
        self, db, tenant, client, as_user, make_user
    ):
        """A service that masks correctly is worth nothing if the route
        serialises the model instead — and that mistake looks exactly like
        working code."""
        _employee(db, tenant)
        db.commit()
        as_user(make_user(UserRole.MANAGER))

        response = client.get("/api/v1/hr/employees")

        assert response.status_code == 200, response.text
        row = response.json()[0]
        assert row["base_salary"] == "restricted"
        assert "1234567" not in str(row["national_id"])

    def test_the_endpoint_shows_everything_to_the_cfo(
        self, db, tenant, client, as_user, make_user
    ):
        _employee(db, tenant)
        db.commit()
        as_user(make_user(UserRole.CFO))

        row = client.get("/api/v1/hr/employees").json()[0]

        assert row["base_salary"] == 80000.0
        assert row["national_id"] == "35202-1234567-1"

    def test_the_raw_salary_is_nowhere_in_a_masked_response(
        self, db, tenant, client, as_user, make_user
    ):
        """Checked against the whole response body rather than one field: a
        nested copy would leak just as effectively as the top-level one."""
        _employee(db, tenant, salary="123456")
        db.commit()
        as_user(make_user(UserRole.MANAGER))

        body = client.get("/api/v1/hr/employees").text

        assert "123456" not in body

    def test_a_clerk_is_refused_the_directory(
        self, db, tenant, client, as_user, make_user
    ):
        as_user(make_user(UserRole.AP_CLERK))

        assert client.get("/api/v1/hr/employees").status_code == 403


class TestTheEndpointsWork:
    def test_creating_a_person_and_linking_an_account(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        as_user(admin)

        created = client.post("/api/v1/hr/employees", json={
            "employee_number": "E-900", "full_name": "New Starter",
            "job_title": "Driver", "start_date": "2026-09-01",
        })
        assert created.status_code == 201, created.text
        assert created.json()["has_login"] is False

        linked = client.post(
            f"/api/v1/hr/employees/{created.json()['id']}/user",
            json={"user_id": str(target["id"])},
        )
        assert linked.json()["has_login"] is True

    def test_the_headcount_flow(self, db, tenant, client, as_user, make_user):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        as_user(manager)

        created = client.post("/api/v1/hr/headcount", json={
            "job_title": "Analyst", "annual_cost": "120000",
            "justification": "Two projects waiting on capacity.",
        })
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]

        client.post(f"/api/v1/hr/headcount/{request_id}/submit")
        as_user(cfo)
        approved = client.post(f"/api/v1/hr/headcount/{request_id}/approve")

        assert approved.json()["current_state"] == "approved"

    def test_a_manager_cannot_approve_headcount_through_the_api(
        self, db, tenant, client, as_user, make_user
    ):
        """The permission split has to hold at the edge, not only in the
        service."""
        manager = make_user(UserRole.MANAGER)
        other = make_user(UserRole.MANAGER)
        as_user(manager)
        request_id = client.post("/api/v1/hr/headcount", json={
            "job_title": "Analyst", "annual_cost": "120000",
            "justification": "Capacity.",
        }).json()["id"]
        client.post(f"/api/v1/hr/headcount/{request_id}/submit")

        as_user(other)
        assert client.post(
            f"/api/v1/hr/headcount/{request_id}/approve"
        ).status_code == 403

    def test_a_checklist_can_be_raised_and_worked(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        db.commit()
        as_user(admin)

        tasks = client.post(
            f"/api/v1/hr/employees/{employee.id}/checklist", json={},
        )
        assert tasks.status_code == 201, tasks.text
        task_id = tasks.json()[0]["id"]

        done = client.post(
            f"/api/v1/hr/tasks/{task_id}/status", json={"status": "done"},
        )
        assert done.json()["status"] == "done"

    def test_skipping_a_task_without_a_note_is_refused(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        db.commit()
        as_user(admin)
        task_id = client.post(
            f"/api/v1/hr/employees/{employee.id}/checklist", json={},
        ).json()[0]["id"]

        response = client.post(
            f"/api/v1/hr/tasks/{task_id}/status",
            json={"status": "not_applicable"},
        )

        assert response.status_code == 400

    def test_an_expense_claim_end_to_end(
        self, db, tenant, client, as_user, make_user
    ):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        db.commit()
        as_user(manager)

        claim = client.post("/api/v1/hr/expenses", json={
            "employee_id": str(employee.id), "category": "meals",
            "total_amount": "250", "incurred_date": "2026-08-01",
        })
        assert claim.status_code == 201, claim.text
        claim_id = claim.json()["id"]

        client.post(f"/api/v1/hr/expenses/{claim_id}/submit")
        as_user(cfo)
        approved = client.post(f"/api/v1/hr/expenses/{claim_id}/approve", json={})

        assert approved.json()["current_state"] == "approved"

    def test_you_cannot_approve_your_own_claim_through_the_api(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant, user_id=admin["id"])
        db.commit()
        as_user(admin)
        claim_id = client.post("/api/v1/hr/expenses", json={
            "employee_id": str(employee.id), "category": "meals",
            "total_amount": "250", "incurred_date": "2026-08-01",
        }).json()["id"]
        client.post(f"/api/v1/hr/expenses/{claim_id}/submit")

        assert client.post(
            f"/api/v1/hr/expenses/{claim_id}/approve", json={}
        ).status_code == 403

    def test_a_pay_change_never_returns_a_salary_to_the_wrong_role(
        self, db, tenant, client, as_user, make_user
    ):
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, salary="80000")
        db.commit()
        as_user(manager)

        created = client.post("/api/v1/hr/payroll-changes", json={
            "employee_id": str(employee.id), "new_salary": "95000",
            "reason_code": PAY_REASON_PROMOTION, "effective_date": "2026-10-01",
        })
        assert created.status_code == 201, created.text

        listed = client.get("/api/v1/hr/payroll-changes").json()[0]

        assert listed["change_amount"] == 15000.0
        assert listed["new_salary"] is None


class TestHrReachesTheInbox:
    def test_a_pay_change_awaiting_approval_appears(
        self, db, tenant, make_user
    ):
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant)
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal("95000"),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        service.submit(change.id, manager)

        inbox = DecisionInboxService(db).get_inbox(cfo)

        assert "payroll_change_request" in {i["object_type"] for i in inbox["items"]}

    def test_the_inbox_never_shows_a_salary(self, db, tenant, make_user):
        """The amount on the item is the size of the change. The inbox is read
        by anyone who can approve, and it is not where pay leaks."""
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, salary="80000")
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal("95000"),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        service.submit(change.id, manager)

        item = next(
            i for i in DecisionInboxService(db).get_inbox(cfo)["items"]
            if i["object_id"] == change.id
        )

        assert item["amount"] == 15000.0

    def test_the_subject_of_a_pay_change_never_sees_it(
        self, db, tenant, make_user
    ):
        """They cannot approve it, so showing it is an item they cannot
        action."""
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=cfo["id"])
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal("95000"),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        service.submit(change.id, manager)

        items = DecisionInboxService(db).get_inbox(cfo)["items"]

        assert change.id not in {i["object_id"] for i in items}

    def test_a_headcount_request_appears(self, db, tenant, make_user):
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        service = HeadcountService(db)
        request = service.create(
            manager, job_title="Analyst", annual_cost=Decimal("120000"),
            justification="Capacity.",
        )
        service.submit(request.id, manager)

        inbox = DecisionInboxService(db).get_inbox(cfo)

        assert "headcount_request" in {i["object_type"] for i in inbox["items"]}

    def test_an_expense_claim_appears(self, db, tenant, make_user):
        cfo = make_user(UserRole.CFO)
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=manager["id"])
        service = ExpenseService(db)
        claim = service.create(
            manager, employee_id=employee.id, category="meals",
            total_amount=Decimal("250"), incurred_date=date(2026, 8, 1),
        )
        service.submit(claim.id, manager)

        inbox = DecisionInboxService(db).get_inbox(cfo)

        assert "expense_reimbursement" in {i["object_type"] for i in inbox["items"]}


class TestTheHrReports:
    def test_time_to_hire_is_measured_from_approval(self, db, tenant, make_user):
        """The gap before approval is a budget decision and belongs to whoever
        is sitting on it. Mixing the two produces a number neither team can
        act on."""
        from datetime import timedelta
        from app.utils.datetime_helpers import utc_now

        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant)
        service = HeadcountService(db)
        request = service.create(
            manager, job_title="Analyst", annual_cost=Decimal("120000"),
            justification="Capacity.",
        )
        service.submit(request.id, manager)
        service.approve(request.id, cfo)
        request.approved_at = utc_now() - timedelta(days=30)
        db.flush()
        service.mark_filled(request.id, employee.id, manager)

        report = DashboardService(db).hiring_pipeline(cfo)

        assert report["filled"] == 1
        assert report["average_days_to_fill"] == 30.0

    def test_open_roles_are_reported_separately_from_filled_ones(
        self, db, tenant, make_user
    ):
        """An average over completed hires flatters every pipeline — the roles
        that never get filled are exactly the ones missing from it."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        service = HeadcountService(db)
        request = service.create(
            manager, job_title="Analyst", annual_cost=Decimal("120000"),
            justification="Capacity.",
        )
        service.submit(request.id, manager)
        service.approve(request.id, cfo)

        report = DashboardService(db).hiring_pipeline(cfo)

        assert report["approved_still_open"] == 1
        assert report["average_days_to_fill"] is None
        assert len(report["open_positions_ageing"]) == 1

    def test_payroll_variance_reports_movement_not_payroll(
        self, db, tenant, make_user
    ):
        """Readable with hr.view while salaries are not, so it reports the size
        of what moved rather than what anybody earns."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, salary="80000")
        service = PayrollChangeService(db)
        change = service.create(
            manager, employee_id=employee.id, new_salary=Decimal("95000"),
            reason_code=PAY_REASON_PROMOTION, effective_date=date(2026, 10, 1),
        )
        service.submit(change.id, manager)
        service.approve(change.id, cfo)

        report = DashboardService(db).payroll_variance(manager)

        assert report["total_movement"] == 15000.0
        assert report["increases"] == 1
        assert "80000" not in str(report)

    def test_expense_exceptions_surfaces_waived_rules(
        self, db, tenant, make_user
    ):
        from app.services.expense_service import RECEIPT_REQUIRED_ABOVE

        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        service = ExpenseService(db)
        claim = service.create(
            manager, employee_id=employee.id, category="meals",
            total_amount=Decimal("200"), incurred_date=date(2026, 8, 1),
        )
        service.submit(claim.id, manager)
        claim.total_amount = RECEIPT_REQUIRED_ABOVE + 1
        db.flush()
        service.approve(claim.id, cfo, override_reason="Receipt lost; card "
                                                       "statement checked.")

        report = DashboardService(db).expense_exceptions(cfo)

        assert report["policy_overrides"] == 1
        assert report["overrides"][0]["reason"].startswith("Receipt lost")

    def test_approved_but_unpaid_claims_are_chased(self, db, tenant, make_user):
        """An employee is out of pocket and nothing else in the system would
        notice."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        service = ExpenseService(db)
        claim = service.create(
            manager, employee_id=employee.id, category="meals",
            total_amount=Decimal("250"), incurred_date=date(2026, 8, 1),
        )
        service.submit(claim.id, manager)
        service.approve(claim.id, cfo)

        report = DashboardService(db).expense_exceptions(cfo)

        assert report["approved_awaiting_payment"] == 1
        assert report["value_awaiting_payment"] == 250.0

    def test_a_clerk_cannot_read_hr_reports(self, db, tenant, make_user):
        """These aggregate people rather than invoices, so they read with
        hr.view rather than the dashboard permission."""
        clerk = make_user(UserRole.AP_CLERK)

        with pytest.raises(PermissionError):
            DashboardService(db).payroll_variance(clerk)

    def test_each_hr_report_exports(
        self, db, tenant, client, as_user, make_user
    ):
        as_user(make_user(UserRole.CFO))

        for report in ("hiring-pipeline", "payroll-variance", "expense-exceptions"):
            response = client.get(f"/api/v1/dashboard/{report}/export?format=html")
            assert response.status_code == 200, f"{report}: {response.text[:200]}"
