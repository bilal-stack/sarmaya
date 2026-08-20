"""The onboarding and offboarding task engine.

Build Book, Variant C1: "onboarding task engine: accounts, devices, access,
documentation, training" and "cross-department handoff tasks to IT and Finance
with a single audit chain".

The engine exists because most of these tasks are not HR's to do. HR raises
them; IT provisions the laptop and the accounts, Finance sets up payroll. That
handoff is where onboarding actually fails — not because anybody refuses, but
because nothing chases another department's list, and the checklist lives in
somebody's inbox.

**Offboarding runs on the same engine, and that is the half that matters.** An
unfinished onboarding task is a new starter waiting for a laptop. An unfinished
*offboarding* task is somebody who left the company and can still sign in. So
the access-category tasks are tracked identically in both directions, and the
outstanding-access view answers a question an auditor asks directly.

Skipping a task requires a reason. "Not applicable" with no explanation is how
an access revocation quietly disappears.
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.roles import (
    has_permission, PERM_MANAGE_ONBOARDING, PERM_VIEW_HR,
)
from app.models.employee import Employee, EMP_LEFT
from app.models.hr import (
    OnboardingTask, TASK_STATES, TASK_CATEGORIES,
    TASK_PENDING, TASK_IN_PROGRESS, TASK_DONE, TASK_BLOCKED,
    TASK_NOT_APPLICABLE, TASK_CATEGORY_ACCOUNT, TASK_CATEGORY_ACCESS,
    TASK_CATEGORY_DEVICE, TASK_CATEGORY_DOCUMENT, TASK_CATEGORY_TRAINING,
    TASK_CATEGORY_PAYROLL, FLOW_ONBOARDING, FLOW_OFFBOARDING,
)
from app.services.audit import log_audit
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "onboarding_task"

TEAM_IT = "IT"
TEAM_FINANCE = "Finance"
TEAM_HR = "HR"

#: The default checklist. Deliberately a plain list rather than something
#: configurable-from-day-one: a client's real checklist is discovered during
#: implementation, and a configuration screen for a list nobody has written yet
#: is a screen with one wrong answer in it. `create_checklist` accepts extra
#: tasks, which is the escape hatch until that shape is known.
#:
#: (category, title, owning team, days from start date)
DEFAULT_ONBOARDING = [
    (TASK_CATEGORY_ACCOUNT, "Create email account", TEAM_IT, -3),
    (TASK_CATEGORY_ACCESS, "Grant system access for the role", TEAM_IT, -1),
    (TASK_CATEGORY_DEVICE, "Issue laptop and phone", TEAM_IT, -1),
    (TASK_CATEGORY_DOCUMENT, "Signed contract on file", TEAM_HR, -5),
    (TASK_CATEGORY_DOCUMENT, "Identity and right-to-work verified", TEAM_HR, -5),
    (TASK_CATEGORY_PAYROLL, "Add to payroll with bank details", TEAM_FINANCE, -2),
    (TASK_CATEGORY_TRAINING, "Security and policy induction", TEAM_HR, 5),
]

#: Offboarding. The access tasks come first and are due on the last day, not
#: after it — an account revoked "within a week" is a week of access somebody
#: no longer has any reason to hold.
DEFAULT_OFFBOARDING = [
    (TASK_CATEGORY_ACCESS, "Revoke all system access", TEAM_IT, 0),
    (TASK_CATEGORY_ACCOUNT, "Disable email account", TEAM_IT, 0),
    (TASK_CATEGORY_DEVICE, "Recover laptop, phone and passes", TEAM_IT, 0),
    (TASK_CATEGORY_PAYROLL, "Final pay and remove from payroll", TEAM_FINANCE, 3),
    (TASK_CATEGORY_DOCUMENT, "Exit interview recorded", TEAM_HR, 5),
]


def _now():
    return make_naive(to_utc(utc_now()))


class OnboardingService:
    def __init__(self, db: Session):
        self.db = db

    # --- creating ------------------------------------------------------------

    def create_checklist(
        self, employee_id: UUID, current_user: dict, *,
        flow: str = FLOW_ONBOARDING, extra_tasks: Optional[List[Dict]] = None,
    ) -> List[OnboardingTask]:
        """Raise the standard list for one person, plus anything role-specific.

        Refuses to run twice for the same flow: a second checklist would double
        every task and make "is this person fully onboarded" unanswerable.
        """
        self._require(current_user, PERM_MANAGE_ONBOARDING, "manage onboarding")

        if flow not in (FLOW_ONBOARDING, FLOW_OFFBOARDING):
            raise ValueError(f"{flow!r} is not a checklist flow")

        employee = (
            self.db.query(Employee).filter(Employee.id == employee_id).first()
        )
        if not employee:
            raise ValueError("Employee not found")

        existing = (
            self.db.query(OnboardingTask)
            .filter(
                OnboardingTask.employee_id == employee_id,
                OnboardingTask.flow == flow,
            )
            .count()
        )
        if existing:
            raise ValueError(
                f"{employee.employee_number} already has a {flow} checklist. "
                "A second one would double every task and make 'is this person "
                "done' unanswerable."
            )

        template = (
            DEFAULT_ONBOARDING if flow == FLOW_ONBOARDING else DEFAULT_OFFBOARDING
        )
        anchor = (
            employee.start_date if flow == FLOW_ONBOARDING
            else (employee.end_date or employee.start_date)
        )

        created = []
        for category, title, team, offset_days in template:
            task = OnboardingTask(
                tenant_id=current_user["tenant_id"],
                employee_id=employee.id,
                flow=flow,
                category=category,
                title=title,
                owning_team=team,
                status=TASK_PENDING,
                due_date=(anchor + timedelta(days=offset_days)) if anchor else None,
                correlation_id=employee.correlation_id,
            )
            self.db.add(task)
            created.append(task)

        for extra in (extra_tasks or []):
            category = extra.get("category")
            if category not in TASK_CATEGORIES:
                raise ValueError(
                    f"{category!r} is not a task category. One of: "
                    f"{', '.join(TASK_CATEGORIES)}"
                )
            task = OnboardingTask(
                tenant_id=current_user["tenant_id"],
                employee_id=employee.id,
                flow=flow,
                category=category,
                title=extra["title"],
                detail=extra.get("detail"),
                owning_team=extra.get("owning_team"),
                status=TASK_PENDING,
                due_date=extra.get("due_date"),
                correlation_id=employee.correlation_id,
            )
            self.db.add(task)
            created.append(task)

        self.db.flush()
        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type="employee",
            object_id=employee.id, action=f"{flow}_started",
            after_value={"tasks": len(created), "flow": flow},
            comment=(
                f"{len(created)} task(s) raised across "
                f"{len({t.owning_team for t in created})} team(s)."
            ),
        )
        self.db.commit()
        return created

    # --- working the list ----------------------------------------------------

    def set_status(
        self, task_id: UUID, status: str, current_user: dict,
        note: Optional[str] = None,
    ) -> OnboardingTask:
        """Move a task along.

        Skipping or blocking requires a note. "Not applicable" with no
        explanation is how an access revocation quietly disappears, and it is
        indistinguishable from one that was genuinely unnecessary.
        """
        self._require(current_user, PERM_MANAGE_ONBOARDING, "update onboarding tasks")

        if status not in TASK_STATES:
            raise ValueError(
                f"{status!r} is not a task status. One of: {', '.join(TASK_STATES)}"
            )

        task = self.db.query(OnboardingTask).filter(
            OnboardingTask.id == task_id
        ).first()
        if not task:
            raise ValueError("Task not found")

        if status in (TASK_NOT_APPLICABLE, TASK_BLOCKED) and not (note or "").strip():
            raise ValueError(
                f"Marking a task '{status}' needs a note. Without one it cannot "
                "be told apart from a task somebody quietly dropped."
            )

        before = task.status
        task.status = status
        if note:
            task.resolution_note = note.strip()
        if status == TASK_DONE:
            task.completed_by = current_user["id"]
            task.completed_at = _now()

        log_audit(
            db=self.db, tenant_id=current_user["tenant_id"],
            user_id=current_user["id"], object_type=OBJECT_TYPE,
            object_id=task.id, action=f"task_{status}",
            before_value={"status": before},
            after_value={"status": status, "title": task.title},
            comment=note.strip() if note else None,
        )
        self.db.commit()
        self.db.refresh(task)
        return task

    # --- reading -------------------------------------------------------------

    def tasks_for(
        self, employee_id: UUID, current_user: dict, flow: Optional[str] = None
    ) -> List[OnboardingTask]:
        self._require(current_user, PERM_VIEW_HR, "view onboarding tasks")

        query = self.db.query(OnboardingTask).filter(
            OnboardingTask.employee_id == employee_id
        )
        if flow:
            query = query.filter(OnboardingTask.flow == flow)
        return query.order_by(OnboardingTask.due_date, OnboardingTask.title).all()

    def outstanding_access(self, current_user: dict) -> List[Dict]:
        """The question an auditor asks directly: who has left and can still
        sign in?

        An access or account task still open against somebody whose employment
        has ended. This is the reason onboarding and offboarding share an
        engine — the offboarding half is the one with teeth.
        """
        self._require(current_user, PERM_VIEW_HR, "view onboarding tasks")

        rows = (
            self.db.query(OnboardingTask, Employee)
            .join(Employee, Employee.id == OnboardingTask.employee_id)
            .filter(
                OnboardingTask.flow == FLOW_OFFBOARDING,
                OnboardingTask.category.in_(
                    [TASK_CATEGORY_ACCESS, TASK_CATEGORY_ACCOUNT]
                ),
                OnboardingTask.status.in_([TASK_PENDING, TASK_IN_PROGRESS, TASK_BLOCKED]),
            )
            .all()
        )

        today = _now().date()
        return [
            {
                "task_id": task.id,
                "employee_id": employee.id,
                "employee_number": employee.employee_number,
                "full_name": employee.full_name,
                "title": task.title,
                "owning_team": task.owning_team,
                "status": task.status,
                "due_date": task.due_date,
                "days_overdue": (
                    (today - task.due_date).days
                    if task.due_date and task.due_date < today else 0
                ),
                "employment_ended": employee.status == EMP_LEFT,
                "still_has_login": employee.user_id is not None,
            }
            for task, employee in sorted(
                rows, key=lambda r: (r[0].due_date is None, r[0].due_date)
            )
        ]

    def completion(self, current_user: dict, flow: str = FLOW_ONBOARDING) -> Dict:
        """Build Book report: onboarding SLA completion.

        Reported by owning team, because "onboarding is 60% done" tells nobody
        what to do and "IT has four open tasks" tells them exactly.
        """
        self._require(current_user, PERM_VIEW_HR, "view onboarding reports")

        tasks = (
            self.db.query(OnboardingTask)
            .filter(OnboardingTask.flow == flow)
            .all()
        )
        today = _now().date()

        by_team: Dict[str, Dict] = {}
        overdue = 0
        for task in tasks:
            bucket = by_team.setdefault(
                task.owning_team or "Unassigned",
                {"team": task.owning_team or "Unassigned", "total": 0,
                 "done": 0, "open": 0, "overdue": 0},
            )
            bucket["total"] += 1
            if task.status == TASK_DONE:
                bucket["done"] += 1
            elif task.status != TASK_NOT_APPLICABLE:
                bucket["open"] += 1
                if task.due_date and task.due_date < today:
                    bucket["overdue"] += 1
                    overdue += 1

        settled = sum(
            1 for t in tasks if t.status in (TASK_DONE, TASK_NOT_APPLICABLE)
        )
        return {
            "flow": flow,
            "tasks": len(tasks),
            "settled": settled,
            "open": len(tasks) - settled,
            "overdue": overdue,
            "completion_percent": (
                round(settled / len(tasks) * 100, 1) if tasks else 0.0
            ),
            "by_team": sorted(by_team.values(), key=lambda r: -r["overdue"]),
        }

    # --- helpers -------------------------------------------------------------

    def _require(self, current_user: dict, permission: str, what: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {what}"
            )
