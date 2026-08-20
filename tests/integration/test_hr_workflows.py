"""Headcount, onboarding and expenses.

Build Book Variant C1 and C2. Each has one rule that carries the module, and in
every case the failure is quiet:

  * **A headcount request states its cost.** Approving a job title while nobody
    has decided what it costs is a commitment made by accident, and it is paid
    for years.
  * **An offboarding access task left open is somebody who left and can still
    sign in.** That is why onboarding and offboarding share an engine — the
    second half is the one with teeth.
  * **A claim with no receipt is an assertion.** Enforced at submission, while
    the claimant still has the receipt, not a week later.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import UserRole
from app.models.employee import Employee, EMP_LEFT
from app.models.file import File
from app.models.hr import (
    HC_APPROVED, HC_FILLED, EXP_APPROVED, EXP_PENDING_APPROVAL,
    FLOW_OFFBOARDING, TASK_DONE, TASK_NOT_APPLICABLE, TASK_CATEGORY_ACCESS,
    OnboardingTask,
)
from app.services.expense_service import ExpenseService, RECEIPT_REQUIRED_ABOVE
from app.services.headcount_service import HeadcountService
from app.services.onboarding_service import OnboardingService

pytestmark = pytest.mark.integration


def _employee(db, tenant, *, number="E-001", name="Sam Staff", user_id=None,
              cleared=False, end_date=None, status=None):
    employee = Employee(
        id=uuid.uuid4(), tenant_id=tenant.id, employee_number=number,
        full_name=name, job_title="Analyst", start_date=date(2026, 9, 1),
        background_check_cleared=cleared, user_id=user_id,
        end_date=end_date, status=status or "active",
        correlation_id=uuid.uuid4(),
    )
    db.add(employee)
    db.flush()
    return employee


class TestHeadcountRequests:
    def _request(self, db, actor, cost="120000", sensitive=False, positions=1):
        return HeadcountService(db).create(
            actor, job_title="Analyst", annual_cost=Decimal(cost),
            positions=positions, is_sensitive_role=sensitive,
            justification="Team is at capacity and two projects are waiting.",
        )

    def test_a_request_without_a_cost_is_refused(self, db, tenant, make_user):
        """Approving a job title while nobody has decided what it costs is a
        commitment made by accident."""
        manager = make_user(UserRole.MANAGER)

        with pytest.raises(ValueError, match="annual cost"):
            HeadcountService(db).create(
                manager, job_title="Analyst", annual_cost=Decimal("0"),
                justification="Because",
            )

    def test_a_request_without_a_justification_cannot_be_submitted(
        self, db, tenant, make_user
    ):
        """An approver deciding on a job title and a number is deciding with
        nothing to go on."""
        manager = make_user(UserRole.MANAGER)
        request = HeadcountService(db).create(
            manager, job_title="Analyst", annual_cost=Decimal("120000"),
        )

        with pytest.raises(ValueError, match="justification"):
            HeadcountService(db).submit(request.id, manager)

    def test_the_total_is_cost_times_positions(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)

        request = self._request(db, manager, cost="120000", positions=3)

        assert request.total_amount == Decimal("360000")

    def test_the_raiser_cannot_approve(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        request = self._request(db, admin)
        HeadcountService(db).submit(request.id, admin)

        with pytest.raises(PermissionError, match="raised this"):
            HeadcountService(db).approve(request.id, admin)

    def test_a_manager_cannot_approve_headcount(self, db, tenant, make_user):
        """Requesting and approving a hire are separate grants: a manager asks,
        the CFO decides what it costs."""
        manager = make_user(UserRole.MANAGER)
        other_manager = make_user(UserRole.MANAGER)
        request = self._request(db, manager)
        HeadcountService(db).submit(request.id, manager)

        with pytest.raises(PermissionError, match="does not have permission"):
            HeadcountService(db).approve(request.id, other_manager)

    def test_the_cfo_approves_it(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        request = self._request(db, manager)
        HeadcountService(db).submit(request.id, manager)

        approved = HeadcountService(db).approve(request.id, cfo)

        assert approved.current_state == HC_APPROVED

    def test_a_sensitive_role_needs_a_cleared_background_check(
        self, db, tenant, make_user
    ):
        """Checked when somebody is placed into the role, because verification
        happens to a person and at approval time there was no person yet."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        request = self._request(db, manager, sensitive=True)
        HeadcountService(db).submit(request.id, manager)
        HeadcountService(db).approve(request.id, cfo)
        unchecked = _employee(db, tenant, cleared=False)

        with pytest.raises(ValueError, match="background check"):
            HeadcountService(db).mark_filled(request.id, unchecked.id, manager)

    def test_a_cleared_person_can_fill_it(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        request = self._request(db, manager, sensitive=True)
        HeadcountService(db).submit(request.id, manager)
        HeadcountService(db).approve(request.id, cfo)
        cleared = _employee(db, tenant, cleared=True)

        filled = HeadcountService(db).mark_filled(request.id, cleared.id, manager)

        assert filled.current_state == HC_FILLED

    def test_a_filled_request_cannot_authorise_a_second_hire(
        self, db, tenant, make_user
    ):
        """One approval, one thing — the same reasoning that makes a
        requisition terminal once converted."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        request = self._request(db, manager)
        HeadcountService(db).submit(request.id, manager)
        HeadcountService(db).approve(request.id, cfo)
        first = _employee(db, tenant, number="E-A", cleared=True)
        second = _employee(db, tenant, number="E-B", cleared=True)
        HeadcountService(db).mark_filled(request.id, first.id, manager)

        with pytest.raises(ValueError, match="Only an approved request"):
            HeadcountService(db).mark_filled(request.id, second.id, manager)

    def test_plan_versus_actual_counts_committed_cost(
        self, db, tenant, make_user
    ):
        """An approved role nobody has hired is committed cost the budget
        already carries and the headcount does not show."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        _employee(db, tenant, number="E-EXISTING")
        request = self._request(db, manager, cost="200000")
        HeadcountService(db).submit(request.id, manager)
        HeadcountService(db).approve(request.id, cfo)

        report = HeadcountService(db).plan_versus_actual(cfo)

        assert report["employees_on_the_books"] == 1
        assert report["approved_not_yet_filled"] == 1
        assert report["committed_annual_cost"] == 200000.0


class TestOnboardingAndOffboarding:
    def test_a_checklist_spans_several_teams(self, db, tenant, make_user):
        """The point of the engine: most of these are not HR's to do, and
        nothing else in the company would chase them."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)

        tasks = OnboardingService(db).create_checklist(employee.id, admin)

        assert len({t.owning_team for t in tasks}) >= 3
        assert any(t.category == TASK_CATEGORY_ACCESS for t in tasks)

    def test_a_second_checklist_is_refused(self, db, tenant, make_user):
        """It would double every task and make "is this person onboarded"
        unanswerable."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        OnboardingService(db).create_checklist(employee.id, admin)

        with pytest.raises(ValueError, match="already has"):
            OnboardingService(db).create_checklist(employee.id, admin)

    def test_offboarding_is_a_separate_flow_on_the_same_person(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        OnboardingService(db).create_checklist(employee.id, admin)

        offboarding = OnboardingService(db).create_checklist(
            employee.id, admin, flow=FLOW_OFFBOARDING,
        )

        assert all(t.flow == FLOW_OFFBOARDING for t in offboarding)

    def test_skipping_a_task_requires_a_reason(self, db, tenant, make_user):
        """"Not applicable" with no explanation is how an access revocation
        quietly disappears."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        tasks = OnboardingService(db).create_checklist(employee.id, admin)

        with pytest.raises(ValueError, match="needs a note"):
            OnboardingService(db).set_status(
                tasks[0].id, TASK_NOT_APPLICABLE, admin,
            )

    def test_completing_a_task_records_who_did_it(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        tasks = OnboardingService(db).create_checklist(employee.id, admin)

        done = OnboardingService(db).set_status(tasks[0].id, TASK_DONE, admin)

        assert str(done.completed_by) == str(admin["id"])
        assert done.completed_at is not None

    def test_someone_who_left_with_access_still_open_is_surfaced(
        self, db, tenant, make_user
    ):
        """The question an auditor asks directly, and the reason offboarding
        shares this engine."""
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        employee = _employee(
            db, tenant, user_id=target["id"], end_date=date(2026, 8, 1),
            status=EMP_LEFT,
        )
        OnboardingService(db).create_checklist(
            employee.id, admin, flow=FLOW_OFFBOARDING,
        )

        outstanding = OnboardingService(db).outstanding_access(admin)

        assert any(row["employee_id"] == employee.id for row in outstanding)
        row = next(r for r in outstanding if r["employee_id"] == employee.id)
        assert row["employment_ended"] is True
        assert row["still_has_login"] is True

    def test_a_revoked_account_drops_off_that_list(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant, status=EMP_LEFT, end_date=date(2026, 8, 1))
        OnboardingService(db).create_checklist(
            employee.id, admin, flow=FLOW_OFFBOARDING,
        )
        access_tasks = [
            t for t in db.query(OnboardingTask).filter(
                OnboardingTask.employee_id == employee.id
            ).all()
            if t.category in ("access", "account")
        ]
        for task in access_tasks:
            OnboardingService(db).set_status(task.id, TASK_DONE, admin)

        outstanding = OnboardingService(db).outstanding_access(admin)

        assert not any(row["employee_id"] == employee.id for row in outstanding)

    def test_completion_is_reported_by_team(self, db, tenant, make_user):
        """"Onboarding is 60% done" tells nobody what to do; "IT has four open
        tasks" tells them exactly."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant)
        OnboardingService(db).create_checklist(employee.id, admin)

        report = OnboardingService(db).completion(admin)

        teams = {row["team"] for row in report["by_team"]}
        assert "IT" in teams
        assert report["completion_percent"] == 0.0


class TestExpenseClaims:
    def _claim(self, db, employee, actor, amount="500", category="meals"):
        return ExpenseService(db).create(
            actor, employee_id=employee.id, category=category,
            total_amount=Decimal(amount), incurred_date=date(2026, 8, 1),
        )

    def _attach_receipt(self, db, tenant, claim, actor):
        db.add(File(
            id=uuid.uuid4(), tenant_id=tenant.id,
            original_filename="receipt.pdf", stored_filename="r.pdf",
            file_path="./uploads/r.pdf", mime_type="application/pdf",
            file_size=100, file_hash="b" * 64,
            object_type="expense_reimbursement", object_id=claim.id,
            uploaded_by=actor["id"],
        ))
        db.flush()

    def test_a_small_claim_needs_no_receipt(self, db, tenant, make_user):
        """Small cash items are the ones that genuinely lose their paperwork."""
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="200")

        submitted = ExpenseService(db).submit(claim.id, manager)

        assert submitted.current_state == EXP_PENDING_APPROVAL

    def test_a_large_claim_without_a_receipt_is_refused_at_submission(
        self, db, tenant, make_user
    ):
        """At submission, so the claimant finds out while they still have the
        receipt in their hand."""
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(
            db, employee, manager, amount=str(RECEIPT_REQUIRED_ABOVE + 1),
        )

        with pytest.raises(ValueError, match="needs a receipt"):
            ExpenseService(db).submit(claim.id, manager)

    def test_travel_always_needs_one_whatever_the_amount(
        self, db, tenant, make_user
    ):
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="50", category="travel")

        with pytest.raises(ValueError, match="needs a receipt"):
            ExpenseService(db).submit(claim.id, manager)

    def test_attaching_a_receipt_unblocks_it(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="5000")
        self._attach_receipt(db, tenant, claim, manager)

        submitted = ExpenseService(db).submit(claim.id, manager)

        assert submitted.has_receipt is True

    def test_nobody_approves_their_own_claim(self, db, tenant, make_user):
        """The self-approval easiest to rationalise, which is what makes it
        worth making impossible — admins included."""
        admin = make_user(UserRole.ADMIN)
        employee = _employee(db, tenant, user_id=admin["id"])
        claim = self._claim(db, employee, admin, amount="200")
        ExpenseService(db).submit(claim.id, admin)

        with pytest.raises(PermissionError, match="cannot approve it"):
            ExpenseService(db).approve(claim.id, admin)

    def test_nor_one_entered_on_their_behalf(self, db, tenant, make_user):
        """Somebody else keying it in does not make it somebody else's claim."""
        admin = make_user(UserRole.ADMIN)
        claimant = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=claimant["id"])
        claim = self._claim(db, employee, admin, amount="200")
        ExpenseService(db).submit(claim.id, admin)

        with pytest.raises(PermissionError, match="claim is yours"):
            ExpenseService(db).approve(claim.id, claimant)

    def test_an_unrelated_approver_can_approve(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="200")
        ExpenseService(db).submit(claim.id, manager)

        approved = ExpenseService(db).approve(claim.id, cfo)

        assert approved.current_state == EXP_APPROVED

    def test_waiving_the_receipt_rule_needs_a_written_reason(
        self, db, tenant, make_user
    ):
        """A rule that can be waived silently is not a rule."""
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="200")
        ExpenseService(db).submit(claim.id, manager)
        # Push it over the threshold after submission, so approval faces a
        # claim that now needs evidence it does not have.
        claim.total_amount = RECEIPT_REQUIRED_ABOVE + 1
        db.flush()

        with pytest.raises(ValueError, match="written reason"):
            ExpenseService(db).approve(claim.id, cfo)

        approved = ExpenseService(db).approve(
            claim.id, cfo, override_reason="Receipt lost in transit; card "
                                          "statement checked against the claim.",
        )
        assert approved.policy_override_reason is not None

    def test_a_claimant_sees_only_their_own(self, db, tenant, make_user):
        """An expense list is a record of where people went and what they
        bought. There is no reason for it to be company-readable by anyone
        holding a login."""
        clerk = make_user(UserRole.AP_CLERK)
        manager = make_user(UserRole.MANAGER)
        mine = _employee(db, tenant, number="E-MINE", user_id=clerk["id"])
        theirs = _employee(db, tenant, number="E-THEIRS", user_id=manager["id"])
        self._claim(db, mine, clerk, amount="100")
        self._claim(db, theirs, manager, amount="100")

        rows = ExpenseService(db).list_claims(clerk)

        assert len(rows) == 1
        assert rows[0]["employee_id"] == mine.id

    def test_a_paid_claim_cannot_be_cancelled(self, db, tenant, make_user):
        manager = make_user(UserRole.MANAGER)
        cfo = make_user(UserRole.CFO)
        employee = _employee(db, tenant, user_id=manager["id"])
        claim = self._claim(db, employee, manager, amount="200")
        ExpenseService(db).submit(claim.id, manager)
        ExpenseService(db).approve(claim.id, cfo)
        ExpenseService(db).mark_paid(claim.id, cfo)

        with pytest.raises(ValueError, match="cannot be cancelled"):
            ExpenseService(db).cancel(claim.id, manager)


class TestEveryWorkflowHasAClock:
    """A guard, not a feature test.

    This gap has now appeared twice: the inventory workflows shipped with SLA
    settings and no `WORKFLOW_TYPE`, so nothing scanned them; and the HR
    workflows shipped with `WORKFLOW_TYPE` and no configured states, so the
    scan found nothing to measure. Both halves are needed and neither fails
    loudly on its own — the records simply sit outside every clock in the
    system and read as never overdue.

    Written against the registries rather than against a list of workflows, so
    the next module added is covered by it without anybody remembering to.
    """

    def test_every_scanned_workflow_has_states_configured(self):
        from app.services.config_defaults import DEFAULT_WORKFLOWS
        from app.services.workflow import workflow_models

        unconfigured = set(workflow_models()) - set(DEFAULT_WORKFLOWS)

        assert not unconfigured, (
            f"These declare WORKFLOW_TYPE so the escalation runner scans them, "
            f"but have no states in DEFAULT_WORKFLOWS, so it finds no SLA to "
            f"measure and silently does nothing: {sorted(unconfigured)}"
        )

    def test_every_configured_workflow_has_a_model(self):
        """The other direction: states configured for a workflow no model
        declares are states nothing will ever enter."""
        from app.services.config_defaults import DEFAULT_WORKFLOWS
        from app.services.workflow import workflow_models

        orphaned = set(DEFAULT_WORKFLOWS) - set(workflow_models())

        assert not orphaned, (
            f"States configured for workflows no model declares: {sorted(orphaned)}"
        )

    def test_every_workflow_model_can_hold_a_timer(self):
        """`state_entered_at` is what an SLA is measured from. A model without
        it computes no deadline at all, which reads as "never overdue"."""
        from app.services.workflow import workflow_models

        missing = [
            name for name, model in workflow_models().items()
            if not hasattr(model, "state_entered_at")
        ]

        assert not missing, (
            f"These have no state_entered_at, so their SLA can never be "
            f"computed: {missing}"
        )

    #: Waiting states that deliberately carry no SLA, each with the reason.
    #: An allowlist rather than a weaker assertion: a new clock-free state has
    #: to be argued for here, in writing, instead of just not failing.
    CLOCK_FREE = {
        # Transient. The service moves through these to their terminal state
        # inside one transaction, so nothing ever sits in them and an SLA
        # would measure a moment that does not exist.
        "inventory_adjustment.approved": "posts in the same transaction",
        "payroll_change_request.approved": "applies in the same transaction",
        # A different clock already governs these, and a second one would
        # escalate on a schedule that disagrees with the real deadline.
        "rfq.issued": "the tender's own closing date is the deadline",
        "purchase_order.issued": "the PO's expected_date drives the supplier "
                                 "delivery report",
        # Genuinely open questions, recorded rather than hidden. Both are
        # waiting on somebody, and neither is chased today; whether they
        # should be is a business decision, not a technical one.
        "requisition.approved": "waiting to be converted to an order - "
                                "arguably should be chased; see status notes",
        "invoice.approved": "approved and unpaid - surfaced by the control "
                            "room rather than escalated",
        "purchase_order.approved": "waiting to be issued to the vendor - "
                                   "arguably should be chased",
        "invoice.validated": "waiting to be submitted by the same person who "
                             "validated it",
    }

    def test_every_waiting_state_carries_an_sla_or_a_reason(self):
        """Build Book, DR-037: every waiting state has a clock.

        A state somebody can sit in — not initial, not final — with no SLA is a
        queue nothing chases. Some legitimately have no clock: they are passed
        through inside a single transaction, or a domain date governs them. Those
        are listed above with their reason, so the exception is a written
        argument rather than an absence nobody noticed.
        """
        from app.services.config_defaults import DEFAULT_WORKFLOWS

        naked = []
        for workflow, states in DEFAULT_WORKFLOWS.items():
            for row in states:
                name, is_initial, is_final = row[0], row[3], row[4]
                sla = row[8] if len(row) > 8 else {}
                if is_initial or is_final:
                    continue
                if (sla or {}).get("hours"):
                    continue
                key = f"{workflow}.{name}"
                if key not in self.CLOCK_FREE:
                    naked.append(key)

        assert not naked, (
            "Waiting states with no SLA and no recorded reason — nothing will "
            f"ever chase these: {naked}. Give them an SLA, or add them to "
            "CLOCK_FREE with the argument for why they do not need one."
        )

    def test_the_clock_free_list_has_no_stale_entries(self):
        """An allowlist that outlives the thing it excuses is how a real gap
        gets permanently hidden behind an old justification."""
        from app.services.config_defaults import DEFAULT_WORKFLOWS

        real_states = {
            f"{workflow}.{row[0]}"
            for workflow, states in DEFAULT_WORKFLOWS.items()
            for row in states
        }
        stale = set(self.CLOCK_FREE) - real_states

        assert not stale, f"CLOCK_FREE names states that no longer exist: {stale}"
