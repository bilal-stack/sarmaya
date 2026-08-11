"""Reconciliation: comparing what was instructed against what the bank did.

Two questions, and the second is the one worth building for:

  * **Did what we instructed actually clear?** A released run with no bank line
    against it after several days means the file was never uploaded, the bank
    rejected it, or someone dropped it. The vendor is unpaid and nobody knows.
  * **Did anything clear that we never instructed?** An unexplained debit
    cannot be produced by any mistake inside the workflow. Every internal
    record can be wrong together; the bank's copy is the one nobody here wrote.

Matching is a **suggestion a human confirms**, never automatic. A wrong
automatic match is worse than no match at all: it marks a payment as cleared
that did not clear, and it hides the unexplained debit sitting next to it by
consuming it. So this module scores candidates and explains its reasoning, and
a person decides.

Scoring is deliberately transparent — each signal contributes a named reason
that is shown to the reconciler and stored in the audit entry. A confidence
number nobody can interrogate would just be an automatic match with extra
steps.
"""
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.bank_statement import BankStatement, BankStatementLine
from app.models.payment import Payment
from app.core.enums import PaymentState, Currency
from app.core.roles import (
    has_permission, PERM_VIEW_BANK_STATEMENT, PERM_IMPORT_BANK_STATEMENT,
    PERM_RECONCILE_PAYMENT,
)
from app.services.audit import log_audit
from app.services.statement_parsers import parse_statement, StatementParseError
from app.services import sod
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "bank_statement"

#: How far either side of a payment's date a statement line may fall and still
#: be considered the same event. Settlement is not same-day: a file uploaded on
#: Friday can book on Monday, and value dates lag booking dates.
DATE_WINDOW_DAYS = 10

#: A released run older than this with nothing matched against it is reported
#: as outstanding. Short enough to catch a file that was never uploaded, long
#: enough not to cry wolf over a normal settlement cycle.
OUTSTANDING_AFTER_DAYS = 5

#: Below this a candidate is not offered at all. Showing every debit against
#: every payment trains the reconciler to click through without reading.
MINIMUM_SCORE = 40


class MatchCandidate:
    """A possible payment for a statement line, with its reasoning."""

    def __init__(self, payment: Payment, score: int, reasons: List[str]):
        self.payment = payment
        self.score = score
        self.reasons = reasons

    @property
    def confidence(self) -> str:
        if self.score >= 90:
            return "high"
        if self.score >= 65:
            return "medium"
        return "low"


class ReconciliationService:
    def __init__(self, db: Session):
        self.db = db

    # --- import --------------------------------------------------------------

    def import_statement(
        self,
        content: str,
        current_user: dict,
        source_format: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> BankStatement:
        """Parse and store a statement file.

        The file's SHA-256 is checked before anything is written. Importing the
        same statement twice would duplicate every line, and a duplicated line
        both invents money that never moved and offers itself as a match for a
        payment that was already reconciled — the reconciliation would then be
        confidently wrong, which is worse than missing.
        """
        self._require(current_user, PERM_IMPORT_BANK_STATEMENT, "import bank statements")

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = (
            self.db.query(BankStatement)
            .filter(BankStatement.file_hash == digest)
            .first()
        )
        if existing:
            raise ValueError(
                f"This exact file was already imported as {existing.statement_reference} "
                f"on {existing.created_at:%Y-%m-%d}. Importing it again would "
                "duplicate every transaction on it."
            )

        try:
            parsed = parse_statement(content, source_format)
        except StatementParseError as exc:
            raise ValueError(f"Could not read the statement: {exc}") from exc

        if not parsed.lines:
            raise ValueError("The statement contained no transactions.")

        currency = self._currency(parsed.currency)
        statement = BankStatement(
            tenant_id=current_user["tenant_id"],
            statement_reference=parsed.statement_reference,
            account_identifier=parsed.account_identifier,
            source_format=parsed.source_format,
            statement_date=parsed.statement_date,
            opening_balance=parsed.opening_balance,
            closing_balance=parsed.closing_balance,
            currency=currency,
            file_hash=digest,
            original_filename=filename,
            imported_by=current_user["id"],
        )
        self.db.add(statement)
        self.db.flush()

        for line in parsed.lines:
            self.db.add(BankStatementLine(
                tenant_id=current_user["tenant_id"],
                bank_statement_id=statement.id,
                line_number=line.line_number,
                value_date=line.value_date,
                booking_date=line.booking_date,
                amount=line.amount,
                is_debit=line.is_debit,
                currency=currency,
                description=line.description,
                counterparty=line.counterparty,
                bank_reference=line.bank_reference,
            ))

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=statement.id,
            action="imported",
            workflow_type=OBJECT_TYPE,
            after_value={
                "statement_reference": statement.statement_reference,
                "source_format": parsed.source_format,
                "lines": len(parsed.lines),
                "debits": sum(1 for line in parsed.lines if line.is_debit),
                "sha256": digest,
            },
        )
        self.db.commit()
        self.db.refresh(statement)
        return statement

    # --- reads ---------------------------------------------------------------

    def list_statements(self, current_user: dict) -> List[BankStatement]:
        self._require(current_user, PERM_VIEW_BANK_STATEMENT, "view bank statements")
        return (
            self.db.query(BankStatement)
            .order_by(BankStatement.created_at.desc())
            .limit(200)
            .all()
        )

    def get_statement(self, statement_id: UUID, current_user: dict) -> BankStatement:
        self._require(current_user, PERM_VIEW_BANK_STATEMENT, "view bank statements")
        statement = (
            self.db.query(BankStatement)
            .filter(BankStatement.id == statement_id)
            .first()
        )
        if not statement:
            raise ValueError("Bank statement not found")
        return statement

    def suggestions_for_line(
        self, line_id: UUID, current_user: dict
    ) -> Tuple[BankStatementLine, List[MatchCandidate]]:
        """Ranked payment candidates for one statement line, with reasoning."""
        self._require(current_user, PERM_VIEW_BANK_STATEMENT, "view bank statements")
        line = self._get_line(line_id)
        return line, self._candidates(line)

    def unreconciled(self, current_user: dict) -> Dict[str, list]:
        """Both sides of the gap, in one answer.

        Returned together on purpose. A reconciler looking only at outstanding
        payments never sees the debit nobody instructed, and that is the item
        that matters most.
        """
        self._require(current_user, PERM_VIEW_BANK_STATEMENT, "view bank statements")

        cutoff = (utc_now() - timedelta(days=OUTSTANDING_AFTER_DAYS)).date()
        matched_payment_ids = {
            row.matched_payment_id
            for row in self.db.query(BankStatementLine)
            .filter(BankStatementLine.matched_payment_id.isnot(None))
            .all()
        }

        instructed_not_cleared = [
            payment
            for payment in self.db.query(Payment)
            .filter(Payment.current_state == PaymentState.RELEASED.value)
            .order_by(Payment.payment_date.asc())
            .all()
            if payment.id not in matched_payment_ids and payment.payment_date <= cutoff
        ]

        cleared_not_instructed = (
            self.db.query(BankStatementLine)
            .filter(
                BankStatementLine.matched_payment_id.is_(None),
                BankStatementLine.is_debit.is_(True),
            )
            .order_by(BankStatementLine.value_date.desc())
            .limit(500)
            .all()
        )

        # Each unexplained debit carries its own candidates, so the reconciler
        # can tell "we have not matched this yet" from "nothing here could
        # possibly explain this" — only the second is a fraud signal.
        for line in cleared_not_instructed:
            line.candidates = self._candidates(line)

        return {
            "instructed_not_cleared": instructed_not_cleared,
            "cleared_not_instructed": cleared_not_instructed,
        }

    # --- writes --------------------------------------------------------------

    def confirm_match(
        self, line_id: UUID, payment_id: UUID, current_user: dict
    ) -> BankStatementLine:
        """Record that a human decided this line settles this payment."""
        self._require(current_user, PERM_RECONCILE_PAYMENT, "reconcile payments")

        line = self._get_line(line_id)
        if line.matched_payment_id:
            raise ValueError(
                "This statement line is already matched. Unmatch it first if the "
                "match was wrong."
            )
        if not line.is_debit:
            raise ValueError(
                "A credit did not pay anyone. Only a debit can settle a payment run."
            )

        payment = self.db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("Payment not found")

        state = str(getattr(payment.current_state, "value", payment.current_state)).lower()
        if state != PaymentState.RELEASED.value:
            raise ValueError(
                f"Payment {payment.payment_number} is {state}, not released. "
                "A run that was never authorised cannot be what cleared — if the "
                "bank debited this, that is the finding, not a match."
            )

        # The releaser authorised the money leaving. Letting the same person
        # certify the bank line that explains it puts the instruction and its
        # verification in one pair of hands, which is the whole point of
        # reconciliation being a separate control. The preparer is deliberately
        # not blocked: their work was already checked at release, and blocking
        # them too would leave small teams with nobody able to reconcile.
        if sod.violates_self_reconciliation(payment.released_by, current_user):
            self._audit_block(line, payment, current_user, "self_reconciliation")
            raise PermissionError(
                "Segregation of duties: a payment must be reconciled by someone "
                "other than the person who released it."
            )

        already = (
            self.db.query(BankStatementLine)
            .filter(
                BankStatementLine.matched_payment_id == payment_id,
                BankStatementLine.id != line_id,
            )
            .first()
        )
        if already:
            raise ValueError(
                f"Payment {payment.payment_number} is already matched to another "
                "statement line. Two bank debits for one instruction is a "
                "duplicate payment, not a reconciliation."
            )

        candidates = {c.payment.id: c for c in self._candidates(line)}
        candidate = candidates.get(payment_id)

        line.matched_payment_id = payment_id
        line.matched_by = current_user["id"]
        line.matched_at = utc_now()
        self.db.add(line)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="payment",
            object_id=payment.id,
            action="reconciled",
            workflow_type="payment",
            comment=(
                f"Matched to bank line {line.line_number} of "
                f"{line.statement.statement_reference}."
            ),
            after_value={
                "statement_line_id": str(line.id),
                "bank_reference": line.bank_reference,
                "bank_amount": str(line.amount),
                "payment_amount": str(payment.total_amount),
                # What the system thought before the human decided. A confirmed
                # low-confidence match is exactly what a later reviewer wants to
                # find, and it is unrecoverable if not written down now.
                "suggested_score": candidate.score if candidate else 0,
                "suggested_reasons": candidate.reasons if candidate else [],
                "confirmed_against_suggestion": candidate is None,
            },
        )
        self.db.commit()
        self.db.refresh(line)
        return line

    def unmatch(
        self, line_id: UUID, reason: str, current_user: dict
    ) -> BankStatementLine:
        """Undo a match. Requires a reason — a match that was wrong is a finding."""
        self._require(current_user, PERM_RECONCILE_PAYMENT, "reconcile payments")
        if not (reason or "").strip():
            raise ValueError("A reason is required to undo a match")

        line = self._get_line(line_id)
        if not line.matched_payment_id:
            raise ValueError("This statement line is not matched")

        payment_id = line.matched_payment_id
        line.matched_payment_id = None
        line.matched_by = None
        line.matched_at = None
        self.db.add(line)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="payment",
            object_id=payment_id,
            action="reconciliation_undone",
            workflow_type="payment",
            comment=reason.strip(),
            before_value={"statement_line_id": str(line.id)},
        )
        self.db.commit()
        self.db.refresh(line)
        return line

    # --- matching ------------------------------------------------------------

    def _candidates(self, line: BankStatementLine) -> List[MatchCandidate]:
        """Score released payments against one statement line.

        Only released runs are considered: a draft was never instructed, so a
        bank debit resembling one is a finding rather than a match.
        """
        if not line.is_debit:
            return []

        matched_elsewhere = {
            row.matched_payment_id
            for row in self.db.query(BankStatementLine)
            .filter(
                BankStatementLine.matched_payment_id.isnot(None),
                BankStatementLine.id != line.id,
            )
            .all()
        }

        payments = (
            self.db.query(Payment)
            .filter(Payment.current_state == PaymentState.RELEASED.value)
            .all()
        )

        candidates = []
        for payment in payments:
            if payment.id in matched_elsewhere:
                continue
            score, reasons = self._score(line, payment)
            if score >= MINIMUM_SCORE:
                candidates.append(MatchCandidate(payment, score, reasons))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:5]

    def _score(self, line: BankStatementLine, payment: Payment) -> Tuple[int, List[str]]:
        """Weigh one payment against one line.

        Weights reflect how hard each signal is to produce by coincidence. The
        bank echoing our own payment reference is near-conclusive; an amount
        matching to the cent is strong; a date in range is weak on its own,
        because everything settles in the same week.
        """
        score = 0
        reasons: List[str] = []

        haystack = " ".join(
            filter(None, [line.description, line.bank_reference, line.counterparty])
        ).lower()
        number = (payment.payment_number or "").lower()
        if number and number in haystack:
            score += 60
            reasons.append(f"Bank reference contains {payment.payment_number}")

        bank_amount = Decimal(line.amount or 0)
        payment_amount = Decimal(payment.total_amount or 0)
        if bank_amount == payment_amount:
            score += 40
            reasons.append("Amount matches exactly")
        elif payment_amount and abs(bank_amount - payment_amount) <= payment_amount * Decimal("0.01"):
            # Bank charges deducted at source are the usual explanation for a
            # near miss, and they are worth surfacing rather than hiding.
            score += 20
            reasons.append(
                f"Amount differs by {abs(bank_amount - payment_amount):.2f} "
                "— check for bank charges"
            )
        else:
            # A different amount is the strongest disqualifier there is, so it
            # cannot be outweighed by a date and a name that happen to fit.
            return 0, []

        line_date = line.value_date or line.booking_date
        if line_date and payment.payment_date:
            days = abs((line_date - payment.payment_date).days)
            if days <= 2:
                score += 15
                reasons.append("Cleared within two days of the payment date")
            elif days <= DATE_WINDOW_DAYS:
                score += 8
                reasons.append(f"Cleared {days} days after the payment date")
            else:
                score -= 20
                reasons.append(
                    f"Dated {days} days from the payment — outside the usual window"
                )

        if line.counterparty:
            counterparty = line.counterparty.strip().lower()
            for payment_line in payment.lines:
                vendor = (payment_line.vendor_name or "").strip().lower()
                if vendor and (vendor in counterparty or counterparty in vendor):
                    score += 15
                    reasons.append(f"Counterparty matches {payment_line.vendor_name}")
                    break

        if line.currency and payment.currency and line.currency != payment.currency:
            score -= 30
            reasons.append("Currency differs from the payment")

        return max(score, 0), reasons

    # --- helpers -------------------------------------------------------------

    def _get_line(self, line_id: UUID) -> BankStatementLine:
        line = (
            self.db.query(BankStatementLine)
            .filter(BankStatementLine.id == line_id)
            .first()
        )
        if not line:
            raise ValueError("Statement line not found")
        return line

    @staticmethod
    def _currency(code: Optional[str]) -> Optional[Currency]:
        """Banks send currency codes we may not model; an unknown one is left
        null rather than guessed, because a wrong currency on a reconciliation
        is worse than a missing one."""
        if not code:
            return None
        try:
            return Currency(code.strip().upper())
        except ValueError:
            logger.warning("Statement carries unmodelled currency %r", code)
            return None

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _audit_block(
        self, line: BankStatementLine, payment: Payment, current_user: dict, reason: str
    ) -> None:
        """A refused match is committed on its own, so the attempt survives even
        though the action does not."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="payment",
            object_id=payment.id,
            action="reconciliation_blocked",
            workflow_type="payment",
            comment=reason,
            after_value={"reason": reason, "statement_line_id": str(line.id)},
        )
        self.db.commit()
