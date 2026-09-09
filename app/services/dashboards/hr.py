"""Variant C: HR Leadership.

Hiring pipeline, payroll variance and expense exceptions. These read
with hr.view rather than the dashboard permission — see _require_hr.
"""
from datetime import timedelta
from typing import Dict


from app.core.roles import has_permission
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    DashboardBase, _now,
)


class HrReports(DashboardBase):
    # --- Variant C: HR ------------------------------------------------------
    #
    # Build Book C1/C2 reports: time to hire and onboarding SLA completion,
    # headcount plan vs actual, payroll variance and exception trends.
    # `headcount_plan` lives on HeadcountService, which owns that question;
    # these are the two that read across HR rather than within one record.

    def hiring_pipeline(self, current_user: dict, days: int = 365) -> Dict:
        """Time to hire, and where requests are stuck.

        Measured from **approval** to filled, not from when the request was
        raised. The gap before approval is a budget decision and belongs to
        whoever is sitting on it; time-to-hire is a recruiting number, and
        mixing the two produces a figure neither team can act on.

        Requests still open are reported separately from those filled. An
        average computed only over completed hires flatters every pipeline —
        the roles that never get filled are exactly the ones missing from it.
        """
        self._require_hr(current_user)
        from app.models.hr import (
            HeadcountRequest, HC_APPROVED, HC_FILLED, HC_PENDING_APPROVAL,
        )

        since = _now() - timedelta(days=days)
        today = _now().date()

        filled = (
            self.db.query(HeadcountRequest)
            .filter(
                HeadcountRequest.current_state == HC_FILLED,
                HeadcountRequest.filled_at >= since,
            )
            .all()
        )
        still_open = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_APPROVED)
            .all()
        )
        awaiting = (
            self.db.query(HeadcountRequest)
            .filter(HeadcountRequest.current_state == HC_PENDING_APPROVAL)
            .count()
        )

        days_to_fill = [
            (row.filled_at - row.approved_at).days
            for row in filled
            if row.filled_at and row.approved_at
        ]

        return {
            "window_days": days,
            "filled": len(filled),
            "average_days_to_fill": (
                round(sum(days_to_fill) / len(days_to_fill), 1)
                if days_to_fill else None
            ),
            "longest_days_to_fill": max(days_to_fill) if days_to_fill else None,
            "awaiting_approval": awaiting,
            "approved_still_open": len(still_open),
            # The number an average hides: roles approved long ago that nobody
            # has hired. They are committed cost and an unstaffed team.
            "open_positions_ageing": [
                {
                    "request_number": row.request_number,
                    "job_title": row.job_title,
                    "positions": row.positions,
                    "days_open": (
                        (today - row.approved_at.date()).days
                        if row.approved_at else None
                    ),
                    "annual_cost": float(row.annual_cost or 0),
                }
                for row in sorted(
                    still_open,
                    key=lambda r: r.approved_at or _now(),
                )
            ],
        }

    def payroll_variance(self, current_user: dict, days: int = 365) -> Dict:
        """What pay changed, why, and how unusually.

        Build Book: "payroll variance and exception trends". Reported as
        *movement* — the total of every applied change and the reasons behind
        them — rather than as a payroll total, because this report is readable
        by anyone with hr.view while salaries are not. A variance report that
        leaked the payroll would defeat the masking the rest of the module
        enforces.

        Corrections are called out separately. A rise is a decision; a
        correction is a mistake somebody made earlier, and a rising number of
        them says something about the process rather than about pay.
        """
        self._require_hr(current_user)
        from app.models.hr import (
            PayrollChangeRequest, PAY_APPLIED, PAY_REJECTED,
            PAY_REASON_CORRECTION,
        )

        since = _now() - timedelta(days=days)

        applied = (
            self.db.query(PayrollChangeRequest)
            .filter(
                PayrollChangeRequest.current_state == PAY_APPLIED,
                PayrollChangeRequest.applied_at >= since,
            )
            .all()
        )
        rejected = (
            self.db.query(PayrollChangeRequest)
            .filter(
                PayrollChangeRequest.current_state == PAY_REJECTED,
                PayrollChangeRequest.created_at >= since,
            )
            .count()
        )

        by_reason: Dict[str, Dict] = {}
        increases = decreases = 0
        total_movement = 0.0
        for change in applied:
            amount = money_to_float(change.total_amount or 0)
            total_movement += amount
            bucket = by_reason.setdefault(
                change.reason_code,
                {"reason": change.reason_code, "count": 0, "movement": 0.0},
            )
            bucket["count"] += 1
            bucket["movement"] = round(bucket["movement"] + amount, 2)

            if change.current_salary is not None and change.new_salary is not None:
                if change.new_salary > change.current_salary:
                    increases += 1
                elif change.new_salary < change.current_salary:
                    decreases += 1

        corrections = sum(
            row["count"] for code, row in by_reason.items()
            if code == PAY_REASON_CORRECTION
        )

        return {
            "window_days": days,
            "changes_applied": len(applied),
            "total_movement": round(total_movement, 2),
            "increases": increases,
            "decreases": decreases,
            "corrections": corrections,
            "rejected": rejected,
            "by_reason": sorted(by_reason.values(), key=lambda r: -r["movement"]),
        }

    def expense_exceptions(self, current_user: dict, days: int = 90) -> Dict:
        """Claims that needed a rule waived, and claims waiting to be paid.

        Two different failures in one report because they are read by the same
        person. An override is a control being set aside; an approved claim
        that has not been paid is an employee out of pocket, and nothing else
        in the system chases it.
        """
        self._require_hr(current_user)
        from app.models.employee import Employee
        from app.models.hr import (
            ExpenseReimbursement, EXP_APPROVED, EXP_PENDING_APPROVAL, EXP_PAID,
        )

        since = _now() - timedelta(days=days)
        today = _now().date()

        claims = (
            self.db.query(ExpenseReimbursement, Employee)
            .join(Employee, Employee.id == ExpenseReimbursement.employee_id)
            .filter(ExpenseReimbursement.created_at >= since)
            .all()
        )

        overrides, awaiting_payment = [], []
        paid = pending = 0
        by_category: Dict[str, Dict] = {}

        for claim, employee in claims:
            amount = money_to_float(claim.total_amount or 0)
            bucket = by_category.setdefault(
                claim.category,
                {"category": claim.category, "count": 0, "amount": 0.0},
            )
            bucket["count"] += 1
            bucket["amount"] = round(bucket["amount"] + amount, 2)

            if claim.policy_override_reason:
                overrides.append({
                    "claim_number": claim.claim_number,
                    "employee": employee.full_name,
                    "category": claim.category,
                    "amount": amount,
                    "reason": claim.policy_override_reason,
                    "approved_by": claim.approved_by,
                })
            if claim.current_state == EXP_PAID:
                paid += 1
            elif claim.current_state == EXP_PENDING_APPROVAL:
                pending += 1
            elif claim.current_state == EXP_APPROVED:
                awaiting_payment.append({
                    "claim_number": claim.claim_number,
                    "employee": employee.full_name,
                    "amount": amount,
                    "days_since_approval": (
                        (today - claim.approved_at.date()).days
                        if claim.approved_at else None
                    ),
                })

        awaiting_payment.sort(key=lambda r: -(r["days_since_approval"] or 0))

        return {
            "window_days": days,
            "claims": len(claims),
            "pending_approval": pending,
            "paid": paid,
            "policy_overrides": len(overrides),
            "approved_awaiting_payment": len(awaiting_payment),
            "value_awaiting_payment": round(
                sum(r["amount"] for r in awaiting_payment), 2
            ),
            "by_category": sorted(
                by_category.values(), key=lambda r: -r["amount"]
            ),
            "overrides": overrides,
            "awaiting_payment": awaiting_payment[:20],
        }

    def _require_hr(self, current_user: dict) -> None:
        """HR reports read with hr.view rather than the dashboard permission.

        Deliberately not the same gate: the other dashboards aggregate records
        anybody with invoices.view could open individually, while these
        aggregate people. Nothing here exposes a salary, but "who was hired,
        who left, whose pay moved" is still not company-readable.
        """
        from app.core.roles import PERM_VIEW_HR

        if not has_permission(current_user["role"], PERM_VIEW_HR):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot view HR reports"
            )

