"""CFO and Finance Director.

AP aging with its exception funnel, and spend analytics. Both refuse to
drop what they cannot classify: payables with no due date and spend with
no GL account stay in the denominator and are named.
"""
from datetime import timedelta
from typing import Dict, List

from sqlalchemy import func, select

from app.core.enums import (
    InvoiceState, PaymentState,
)
from app.models.bank_statement import BankStatementLine
from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentLine
from app.models.vendor import Vendor
from app.utils.money import money_to_float
from app.services.dashboards._shared import (
    DashboardBase, _now,
)


class CfoReports(DashboardBase):
    # --- CFO: what we owe, and what we spend ---------------------------------

    #: Payables aging, in the buckets a finance team actually uses. Deliberately
    #: NOT the module-level AGE_BUCKETS, which are 0-2/3-7/8-30/30+ and exist to
    #: answer "how long has this been stuck" for an operational reader. A CFO
    #: reading a payables balance expects 30/60/90, measured against the due
    #: date rather than against how long a record has sat in a state. Two
    #: different questions that would give two different numbers from the same
    #: invoices, so they get two different scales.
    PAYABLE_BUCKETS = [
        (None, 0, "not yet due"),
        (0, 30, "1-30 days overdue"),
        (30, 60, "31-60 days overdue"),
        (60, 90, "61-90 days overdue"),
        (90, None, "over 90 days overdue"),
    ]

    #: The pipeline a payable travels, in order. Each stage is somewhere money
    #: can sit, and the funnel exists so a CFO can see which stage is holding
    #: the balance — a large "approved, not in a run" is a treasury problem, a
    #: large "awaiting approval" is an operations one, and the total is the
    #: same either way.
    FUNNEL_STAGES = [
        ("awaiting_approval", "Awaiting approval",
         "Nobody has agreed to pay this yet."),
        ("approved_not_scheduled", "Approved, not in a payment run",
         "Agreed but not scheduled. This is the stage that quietly grows."),
        ("scheduled_not_released", "In a run, not released",
         "Prepared and waiting on a second signature."),
        ("released_not_cleared", "Released, not seen on a statement",
         "Instructed. Not yet confirmed to have left the account."),
    ]

    def ap_aging(self, current_user: dict) -> Dict:
        """What we owe, how late it is, and which stage is holding it.

        Build Book, CFO / Finance Director: "AP aging with exception funnel."

        Aged against the **due date**, not against how long the record has been
        sitting. Those are different figures and only the first is a payables
        aging — an invoice entered yesterday on 90-day terms is not overdue,
        and one entered ninety days ago on 7-day terms is badly overdue. Aging
        by record age would rank those two the wrong way round.

        Invoices with no due date are counted and reported separately rather
        than bucketed as "not yet due". The column is nullable, so treating
        missing as "not due" would quietly move a possibly-overdue balance into
        the comfortable column — and the missing dates are themselves the
        finding, because nothing can chase what has no date.

        A point-in-time balance, so it takes no `days` window: what is owed is
        owed regardless of the period somebody happens to be looking at.
        """
        self._require(current_user)
        today = _now().date()

        rows = (
            self.db.query(
                Invoice.id, Invoice.invoice_number, Invoice.due_date,
                Invoice.total_amount, Invoice.current_state,
                Vendor.legal_name,
            )
            .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
            .filter(
                # Everything owed and not yet settled. Rejected and cancelled
                # are not liabilities; paid ones have already left.
                Invoice.current_state.in_([
                    InvoiceState.VALIDATED.value,
                    InvoiceState.PENDING_APPROVAL.value,
                    InvoiceState.APPROVED.value,
                ]),
            )
            .all()
        )

        buckets = {
            label: {"bucket": label, "count": 0, "amount": 0.0}
            for _, _, label in self.PAYABLE_BUCKETS
        }
        no_due_date = {"count": 0, "amount": 0.0}
        overdue_amount = 0.0
        total = 0.0
        worst: List[Dict] = []

        for row in rows:
            amount = money_to_float(row.total_amount or 0)
            total += amount

            if row.due_date is None:
                no_due_date["count"] += 1
                no_due_date["amount"] += amount
                continue

            days_over = (today - row.due_date).days
            bucket = buckets[self._payable_bucket(days_over)]
            bucket["count"] += 1
            bucket["amount"] += amount

            if days_over > 0:
                overdue_amount += amount
                worst.append({
                    "invoice_id": str(row.id),
                    "invoice_number": row.invoice_number,
                    "vendor": row.legal_name,
                    "amount": round(amount, 2),
                    "days_overdue": days_over,
                    "state": row.current_state,
                })

        for bucket in buckets.values():
            bucket["amount"] = round(bucket["amount"], 2)
        no_due_date["amount"] = round(no_due_date["amount"], 2)
        worst.sort(key=lambda r: (-r["days_overdue"], -r["amount"]))

        return {
            "as_of": today.isoformat(),
            "total_payable": round(total, 2),
            "total_overdue": round(overdue_amount, 2),
            "overdue_pct": round(overdue_amount * 100.0 / total, 1) if total else 0.0,
            "aging": [buckets[label] for _, _, label in self.PAYABLE_BUCKETS],
            # Reported on its own rather than folded into a bucket. See the
            # docstring: a payable with no due date cannot be chased, and
            # rolling it into "not yet due" would hide exactly that.
            "no_due_date": no_due_date,
            "funnel": self._payable_funnel(),
            "most_overdue": worst[:20],
        }

    def _payable_bucket(self, days_overdue: int) -> str:
        for low, high, label in self.PAYABLE_BUCKETS:
            if low is None:
                if days_overdue <= 0:
                    return label
                continue
            if days_overdue > low and (high is None or days_overdue <= high):
                return label
        return self.PAYABLE_BUCKETS[-1][2]

    def _payable_funnel(self) -> List[Dict]:
        """Where the payable balance is sitting, stage by stage.

        Each stage is counted from the records themselves rather than from a
        status column somebody maintains, so a run abandoned halfway shows up
        where it actually is rather than where it was last marked.
        """
        awaiting = self._sum_invoices([
            InvoiceState.VALIDATED.value, InvoiceState.PENDING_APPROVAL.value,
        ])

        # Approved invoices that no payment line references at all.
        scheduled = (
            self.db.query(PaymentLine.invoice_id)
            .filter(PaymentLine.invoice_id.isnot(None))
            .subquery()
        )
        unscheduled = (
            self.db.query(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total_amount), 0),
            )
            .filter(
                Invoice.current_state == InvoiceState.APPROVED.value,
                ~Invoice.id.in_(select(scheduled.c.invoice_id)),
            )
            .one()
        )

        totals = {
            "awaiting_approval": awaiting,
            "approved_not_scheduled": (
                int(unscheduled[0] or 0), money_to_float(unscheduled[1] or 0),
            ),
            "scheduled_not_released": self._sum_payment_lines([
                PaymentState.DRAFT.value, PaymentState.PENDING_RELEASE.value,
            ]),
            "released_not_cleared": self._sum_payment_lines(
                [PaymentState.RELEASED.value], unmatched=True
            ),
        }
        return [
            {
                "stage": key,
                "label": label,
                "note": note,
                "count": totals[key][0],
                "amount": round(totals[key][1], 2),
            }
            for key, label, note in self.FUNNEL_STAGES
        ]

    def _sum_invoices(self, states: List[str]):
        row = (
            self.db.query(
                func.count(Invoice.id),
                func.coalesce(func.sum(Invoice.total_amount), 0),
            )
            .filter(Invoice.current_state.in_(states))
            .one()
        )
        return int(row[0] or 0), money_to_float(row[1] or 0)

    def _sum_payment_lines(self, states: List[str], unmatched: bool = False):
        query = (
            self.db.query(
                func.count(PaymentLine.id),
                func.coalesce(func.sum(PaymentLine.amount), 0),
            )
            .join(Payment, Payment.id == PaymentLine.payment_id)
            .filter(Payment.current_state.in_(states))
        )
        if unmatched:
            # Released but never seen on a statement: money that has left on
            # paper and not in evidence.
            matched = (
                self.db.query(BankStatementLine.matched_payment_id)
                .filter(BankStatementLine.matched_payment_id.isnot(None))
                .subquery()
            )
            query = query.filter(
                ~Payment.id.in_(select(matched.c.matched_payment_id))
            )
        row = query.one()
        return int(row[0] or 0), money_to_float(row[1] or 0)

    # --- CFO: spend analytics ------------------------------------------------

    def spend_analytics(self, current_user: dict, days: int = 365) -> Dict:
        """Where the money went, by the dimensions that actually exist.

        Build Book, CFO / Finance Director: "spend analytics."

        Three dimensions, because three is what an invoice carries: vendor, GL
        account code, and cost centre. There is no category taxonomy in this
        system, so there is no category breakdown here — the same reasoning the
        approval matrix applies to its missing category axis. A fourth chart
        built on a field nobody fills would look like analysis and be noise.

        Both `gl_account_code` and `cost_center` are nullable, and the
        unclassified share is reported as its own figure rather than dropped
        from the denominator. A spend breakdown that silently omits the
        unclassified reads as complete while describing a fraction, and the
        fraction it describes is the well-behaved one — an unclassified
        balance is the part nobody has had to justify.

        Counted on **approved** spend and later: an invoice somebody entered
        and nobody agreed to is not spend, it is a claim.
        """
        self._require(current_user)
        since = (_now() - timedelta(days=days)).date()

        rows = (
            self.db.query(
                Invoice.total_amount, Invoice.invoice_date,
                Invoice.gl_account_code, Invoice.cost_center,
                Invoice.vendor_id, Vendor.legal_name,
            )
            .outerjoin(Vendor, Vendor.id == Invoice.vendor_id)
            .filter(
                Invoice.current_state.in_([
                    InvoiceState.APPROVED.value, InvoiceState.PAID.value,
                ]),
                Invoice.invoice_date >= since,
            )
            .all()
        )

        by_vendor: Dict[str, Dict] = {}
        by_account: Dict[str, Dict] = {}
        by_cost_centre: Dict[str, Dict] = {}
        by_month: Dict[str, Dict] = {}
        total = 0.0
        no_account = 0.0
        no_cost_centre = 0.0

        for row in rows:
            amount = money_to_float(row.total_amount or 0)
            total += amount

            vendor = row.legal_name or "(no vendor)"
            self._accumulate(by_vendor, vendor, amount)

            if row.gl_account_code:
                self._accumulate(by_account, row.gl_account_code, amount)
            else:
                no_account += amount

            if row.cost_center:
                self._accumulate(by_cost_centre, row.cost_center, amount)
            else:
                no_cost_centre += amount

            if row.invoice_date:
                self._accumulate(
                    by_month, row.invoice_date.strftime("%Y-%m"), amount
                )

        top_vendors = sorted(by_vendor.values(), key=lambda r: -r["amount"])
        # Concentration: how much of the spend sits with the largest handful.
        # A CFO reads this as negotiating position on one side and single-
        # supplier exposure on the other, which is why it is one number rather
        # than an invitation to add up the table.
        #
        # Not reported below six vendors, where it is arithmetically always
        # 100% and therefore says nothing about concentration — it says there
        # are five or fewer suppliers, which the vendor count already says. A
        # headline "100%" would read as an alarm about exposure rather than a
        # fact about the size of the supplier list, so it is withheld the same
        # way match rate and payment failures are on the AP/Treasury report.
        top_five = sum(r["amount"] for r in top_vendors[:5])
        concentration = (
            round(top_five * 100.0 / total, 1)
            if total and len(by_vendor) > 5 else None
        )

        return {
            "window_days": days,
            "since": since.isoformat(),
            "total_spend": round(total, 2),
            "invoice_count": len(rows),
            "by_vendor": top_vendors[:25],
            "vendor_count": len(by_vendor),
            "top_5_vendor_share_pct": concentration,
            "by_gl_account": sorted(
                by_account.values(), key=lambda r: -r["amount"]
            )[:25],
            "by_cost_centre": sorted(
                by_cost_centre.values(), key=lambda r: -r["amount"]
            )[:25],
            "by_month": sorted(by_month.values(), key=lambda r: r["key"]),
            # Kept in the denominator and named. See the docstring.
            "unclassified": {
                "no_gl_account": round(no_account, 2),
                "no_gl_account_pct": (
                    round(no_account * 100.0 / total, 1) if total else 0.0
                ),
                "no_cost_centre": round(no_cost_centre, 2),
                "no_cost_centre_pct": (
                    round(no_cost_centre * 100.0 / total, 1) if total else 0.0
                ),
            },
            #: Absent for the same reason the approval matrix reports no
            #: category axis: invoices carry no category, so a breakdown by one
            #: would be invented here rather than measured.
            "by_category": None,
        }

    @staticmethod
    def _accumulate(target: Dict[str, Dict], key: str, amount: float) -> None:
        entry = target.setdefault(key, {"key": key, "count": 0, "amount": 0.0})
        entry["count"] += 1
        entry["amount"] = round(entry["amount"] + amount, 2)

