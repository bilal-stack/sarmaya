"""Payment runs: prepare, release, settle.

The system never moves money. Releasing a run authorises an instruction and
marks the invoices paid; a treasury user then uploads the exported file to
their own banking portal. Nothing here talks to a bank.

Three controls, in the order they bite:

  * **Maker-checker.** Preparing and releasing are separate permissions, and
    the preparer is refused their own release even when they hold both. This is
    the control every upstream approval exists to protect.
  * **Re-checked at release.** A run can wait days. In that time an invoice can
    be paid by another run, a vendor can be blocked, an approval can be undone.
    The release guard re-reads all of it, because release is irreversible.
  * **No double payment.** An invoice already sitting on a released or pending
    run cannot be added to another. This is the check that actually stops money
    going out twice.

On correlation: a run may settle invoices from several chains, so it carries
its own and writes an entry onto each settled invoice's chain naming it. It
appears in every story it touches without pretending to belong to one.
"""
import logging
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentLine
from app.models.vendor import Vendor
from app.core.enums import (
    InvoiceState, PaymentState, VendorStatus, Currency, BankChangeState,
)
from app.core.roles import (
    has_permission, PERM_VIEW_PAYMENT, PERM_PREPARE_PAYMENT, PERM_RELEASE_PAYMENT,
)
from app.services.workflow import transition_state
from app.services.correlation import new_correlation_id
from app.services.audit import log_audit
from app.services.notification_service import NotificationService
from app.services.integration_posting_service import JournalPostingService
from app.models.integration import SOURCE_PAYMENT
from app.services import sod
from app.services.delegation import resolve_permission
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

OBJECT_TYPE = "payment"


class PaymentService:
    def __init__(self, db: Session):
        self.db = db

    # --- reads ---------------------------------------------------------------

    def get_payment(self, payment_id: UUID, current_user: dict) -> Payment:
        self._require(current_user, PERM_VIEW_PAYMENT, "view payments")
        payment = self.db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("Payment not found")
        return self._with_names(payment)

    def list_payments(self, current_user: dict, state: Optional[str] = None) -> List[Payment]:
        self._require(current_user, PERM_VIEW_PAYMENT, "view payments")
        query = self.db.query(Payment)
        if state:
            query = query.filter(Payment.current_state == state)
        return [
            self._with_names(p)
            for p in query.order_by(Payment.created_at.desc()).limit(200).all()
        ]

    def payable_invoices(self, current_user: dict) -> List[Invoice]:
        """Approved invoices not already claimed by an open or released run.

        Offered to whoever prepares a run so the double-payment check is a
        confirmation rather than a surprise at release.
        """
        self._require(current_user, PERM_PREPARE_PAYMENT, "prepare payments")
        claimed = {
            row.invoice_id
            for row in self.db.query(PaymentLine)
            .join(Payment, Payment.id == PaymentLine.payment_id)
            .filter(Payment.current_state.in_([
                PaymentState.RELEASED.value, PaymentState.PENDING_RELEASE.value,
            ]))
            .all()
        }
        invoices = (
            self.db.query(Invoice)
            .filter(Invoice.current_state == InvoiceState.APPROVED.value)
            .order_by(Invoice.invoice_date.asc())
            .all()
        )
        return [i for i in invoices if i.id not in claimed]

    # --- writes --------------------------------------------------------------

    def prepare_payment(self, invoice_ids: List[UUID], data, current_user: dict) -> Payment:
        """Build a draft run over a set of approved invoices."""
        self._require(current_user, PERM_PREPARE_PAYMENT, "prepare payments")
        if not invoice_ids:
            raise ValueError("A payment must settle at least one invoice")

        payment = Payment(
            tenant_id=current_user["tenant_id"],
            payment_number=self._next_number(),
            payment_date=getattr(data, "payment_date", None) or utc_now().date(),
            method=getattr(data, "method", None) or "bank_transfer",
            reference=getattr(data, "reference", None),
            notes=getattr(data, "notes", None),
            current_state=PaymentState.DRAFT,
            correlation_id=new_correlation_id(),
            prepared_by=current_user["id"],
            total_amount=Decimal("0"),
        )
        self.db.add(payment)
        self.db.flush()

        total = Decimal("0")
        first_invoice = None
        try:
            for index, invoice_id in enumerate(invoice_ids, start=1):
                invoice = self._payable_invoice(invoice_id, payment.id)
                vendor = (
                    self.db.query(Vendor).filter(Vendor.id == invoice.vendor_id).first()
                    if invoice.vendor_id else None
                )
                first_invoice = first_invoice or invoice
                amount = Decimal(invoice.total_amount or 0)
                total += amount

                # Bank details are copied, not referenced: they can change after
                # release, and the file that went to the bank must remain
                # reconstructable from what was true when it was built.
                self.db.add(PaymentLine(
                    tenant_id=current_user["tenant_id"],
                    payment_id=payment.id,
                    invoice_id=invoice.id,
                    line_number=index,
                    amount=amount,
                    vendor_id=invoice.vendor_id,
                    vendor_name=invoice.vendor_name,
                    bank_account_name=getattr(vendor, "bank_account_name", None),
                    bank_account_number=getattr(vendor, "bank_account_number", None),
                    bank_name=getattr(vendor, "bank_name", None),
                    iban=getattr(vendor, "iban", None),
                    swift_code=getattr(vendor, "swift_code", None),
                ))
        except Exception:
            # The header is flushed before the invoices are checked, because the
            # duplicate-payment rule needs this run's id to exclude itself. A
            # refusal partway through therefore leaves a headerless run behind
            # unless it is undone here. Today the request-scoped session closes
            # without committing, so nothing persists — but that is the session
            # lifecycle saving us, not this function, and a mixed run naming one
            # payable invoice and one that is not is exactly the shape that
            # reaches this path.
            self.db.rollback()
            raise

        payment.total_amount = total
        # Taken from the invoices being settled when the caller does not say.
        # An amount in a bank file without a currency is not an instruction,
        # and leaving the column null produced exactly that.
        payment.currency = (
            getattr(data, "currency", None)
            or getattr(first_invoice, "currency", None)
            or Currency.PKR
        )
        self.db.add(payment)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=payment.id,
            action="prepared",
            workflow_type=OBJECT_TYPE,
            after_value={
                "payment_number": payment.payment_number,
                "invoices": len(invoice_ids),
                "total_amount": str(total),
            },
        )
        self.db.commit()
        self.db.refresh(payment)
        return self._with_names(payment)

    def submit_for_release(self, payment_id: UUID, current_user: dict) -> Payment:
        self._require(current_user, PERM_PREPARE_PAYMENT, "submit payments for release")
        payment = self._get(payment_id)

        if not transition_state(
            self.db, payment, PaymentState.PENDING_RELEASE.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=payment.id,
            action="submitted_for_release",
            workflow_step=self._state(payment),
            workflow_type=OBJECT_TYPE,
            after_value={"total_amount": str(payment.total_amount)},
        )
        # Maker-checker means somebody else has to release this, so somebody
        # else has to know it is there.
        NotificationService(self.db).notify_awaiting_action(
            payment, PERM_RELEASE_PAYMENT, "release or reject",
            exclude_user_id=payment.prepared_by,
        )
        self.db.commit()
        self.db.refresh(payment)
        return self._with_names(payment)

    def release_payment(self, payment_id: UUID, current_user: dict) -> Payment:
        """Authorise the run and settle its invoices.

        The irreversible step. Everything the system does upstream exists to
        make this moment safe, so it is the most heavily checked: a distinct
        permission, maker-checker against the preparer, and a guard that
        re-reads every line before the transition is allowed.
        """
        can_release, _delegation = resolve_permission(
            self.db, current_user, PERM_RELEASE_PAYMENT
        )
        if not can_release:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "release payments"
            )

        payment = self._get(payment_id)

        # Maker-checker. Deliberately not exempted for admins, unlike the
        # invoice rule: releasing your own payment run is the single action
        # this module exists to prevent.
        if sod.violates_self_release(payment.prepared_by, current_user):
            self._audit_block(payment, current_user, "self_release")
            raise PermissionError(
                "Segregation of duties: a payment must be released by someone "
                "other than the person who prepared it."
            )

        # Build Book line 193. DR-032 holds payments while a change is open;
        # this covers the other side of it, when the new account is live and
        # the money is about to go there for the first time.
        blocking = self._first_payment_after_bank_change(payment, current_user)
        if blocking:
            vendor_name, change = blocking
            self._audit_block(payment, current_user, "first_payment_after_bank_change")
            raise PermissionError(
                f"Segregation of duties: you changed {vendor_name}'s bank "
                "details, so the first payment to them afterwards must be "
                "released by somebody else. The second signature on the change "
                "only means something if it is not the same person again here."
            )

        if not transition_state(
            self.db, payment, PaymentState.RELEASED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")

        payment.released_by = current_user["id"]
        payment.released_at = utc_now()
        self.db.add(payment)
        self.db.flush()

        settled = []
        for line in payment.lines:
            invoice = self.db.query(Invoice).filter(Invoice.id == line.invoice_id).first()
            if not invoice:
                continue
            transition_state(self.db, invoice, InvoiceState.PAID.value, current_user["id"])
            self.db.add(invoice)
            settled.append(invoice.invoice_number)

            # The run keeps its own chain, so each invoice is told about the
            # payment on its own — otherwise the settlement would be invisible
            # from the invoice's story.
            log_audit(
                db=self.db,
                tenant_id=current_user["tenant_id"],
                user_id=current_user["id"],
                object_type="invoice",
                object_id=invoice.id,
                action="marked_paid",
                workflow_step=InvoiceState.PAID.value,
                workflow_type="invoice",
                comment=f"Settled by payment {payment.payment_number}.",
                after_value={
                    "payment_number": payment.payment_number,
                    "payment_id": str(payment.id),
                    "amount": str(line.amount),
                },
            )

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=payment.id,
            action="released",
            workflow_step=self._state(payment),
            workflow_type=OBJECT_TYPE,
            before_value={"prepared_by": str(payment.prepared_by)},
            after_value={
                "released_by": str(current_user["id"]),
                "total_amount": str(payment.total_amount),
                "invoices_settled": settled,
            },
        )

        # Opportunistic: a no-op for every tenant that has not connected an
        # accounting system, which today is all of them. See
        # JournalPostingService.enqueue for why this can never block or delay
        # a release — queued in this same transaction, so it commits with the
        # release or not at all.
        JournalPostingService(self.db).enqueue(SOURCE_PAYMENT, payment, current_user)

        self.db.commit()
        self.db.refresh(payment)
        return self._with_names(payment)

    def reject_payment(self, payment_id: UUID, reason: str, current_user: dict) -> Payment:
        can_release, _ = resolve_permission(self.db, current_user, PERM_RELEASE_PAYMENT)
        if not can_release:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "reject payments"
            )
        payment = self._get(payment_id)

        if not transition_state(
            self.db, payment, PaymentState.REJECTED.value, current_user["id"]
        ):
            raise ValueError("State transition failed")
        payment.rejection_reason = reason
        self.db.add(payment)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=payment.id,
            action="rejected",
            workflow_step=self._state(payment),
            workflow_type=OBJECT_TYPE,
            comment=reason,
        )
        self.db.commit()
        self.db.refresh(payment)
        return self._with_names(payment)

    # --- helpers -------------------------------------------------------------

    def _with_names(self, payment: Payment) -> Payment:
        """Attach preparer and releaser names for display.

        Set on the instance rather than joined in every query: it is a
        presentation concern, and resolving it here means the screen never has
        to hold users.view just to show who authorised a run.
        """
        from app.models.user import User

        ids = {payment.prepared_by, payment.released_by} - {None}
        if ids:
            people = {
                u.id: (u.full_name or u.email)
                for u in self.db.query(User).filter(User.id.in_(ids)).all()
            }
            payment.prepared_by_name = people.get(payment.prepared_by)
            payment.released_by_name = people.get(payment.released_by)
        return payment

    def _get(self, payment_id: UUID) -> Payment:
        payment = self.db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("Payment not found")
        return payment

    @staticmethod
    def _state(payment: Payment) -> str:
        return str(getattr(payment.current_state, "value", payment.current_state)).lower()

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _payable_invoice(self, invoice_id: UUID, payment_id: UUID) -> Invoice:
        """An invoice that may legitimately be added to this run."""
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError("Invoice not found")

        state = str(getattr(invoice.current_state, "value", invoice.current_state)).lower()
        if state == InvoiceState.PAID.value:
            raise ValueError(f"{invoice.invoice_number} has already been paid")
        if state != InvoiceState.APPROVED.value:
            raise ValueError(
                f"{invoice.invoice_number} is {state}; only approved invoices can be paid"
            )

        if invoice.vendor_id:
            vendor = self.db.query(Vendor).filter(Vendor.id == invoice.vendor_id).first()
            status = getattr(vendor.status, "value", vendor.status) if vendor else None
            if status != VendorStatus.ACTIVE.value:
                raise ValueError(
                    f"{invoice.invoice_number}: vendor is {status or 'missing'}, not active"
                )

            # A vendor whose bank details are mid-change is not payable — to
            # either account. Paying the old one during a disputed change is
            # not safe either: if the change is fraudulent the old account may
            # already be the attacker's, and if it is genuine the vendor is
            # expecting the new one. Holding is the only answer that is right
            # in both cases.
            from app.services.vendor_bank_service import VendorBankService

            pending = VendorBankService(self.db).pending_for_vendor(vendor.id)
            if pending:
                raise ValueError(
                    f"{invoice.invoice_number}: {vendor.legal_name} has a bank "
                    "change awaiting resolution, so payments to them are held. "
                    "Resolve it first — this is the window the control exists for."
                )

        clash = (
            self.db.query(PaymentLine)
            .join(Payment, Payment.id == PaymentLine.payment_id)
            .filter(
                PaymentLine.invoice_id == invoice_id,
                PaymentLine.payment_id != payment_id,
                Payment.current_state.in_([
                    PaymentState.RELEASED.value, PaymentState.PENDING_RELEASE.value,
                ]),
            )
            .first()
        )
        if clash:
            raise ValueError(
                f"{invoice.invoice_number} is already on another payment run"
            )
        return invoice

    def _first_payment_after_bank_change(self, payment: Payment, current_user: dict):
        """(vendor_name, change) if this run is the first payment to a vendor
        since the caller changed that vendor's bank details — otherwise None.

        "First" is measured against releases, not preparations: a run that was
        prepared and rejected never paid anybody, so it does not spend the one
        payment this rule guards. Only vendors on *this* run are considered, so
        a change to some unrelated vendor never blocks a release.

        The restriction lifts by itself once one payment has gone through with
        somebody else's signature on it. It is a gate on a single moment, not a
        standing ban on the person who maintains vendor records.
        """
        from app.models.vendor_bank_change import VendorBankChange

        vendor_ids = {line.vendor_id for line in payment.lines if line.vendor_id}
        for vendor_id in vendor_ids:
            change = (
                self.db.query(VendorBankChange)
                .filter(
                    VendorBankChange.vendor_id == vendor_id,
                    VendorBankChange.current_state == BankChangeState.EFFECTIVE.value,
                    VendorBankChange.applied_at.isnot(None),
                )
                .order_by(VendorBankChange.applied_at.desc())
                .first()
            )
            if not change:
                continue
            if not sod.violates_first_payment_after_bank_change(change, current_user):
                continue

            already_paid = (
                self.db.query(PaymentLine.id)
                .join(Payment, Payment.id == PaymentLine.payment_id)
                .filter(
                    PaymentLine.vendor_id == vendor_id,
                    PaymentLine.payment_id != payment.id,
                    Payment.current_state == PaymentState.RELEASED.value,
                    Payment.released_at.isnot(None),
                    Payment.released_at >= change.applied_at,
                )
                .first()
            )
            if already_paid:
                continue

            vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
            return (vendor.legal_name if vendor else "this vendor"), change
        return None

    def _audit_block(self, payment: Payment, current_user: dict, reason: str) -> None:
        """A refused release is committed on its own, so the attempt survives
        even though the action does not."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type=OBJECT_TYPE,
            object_id=payment.id,
            action="release_blocked",
            workflow_type=OBJECT_TYPE,
            comment=f"Blocked: {reason}",
            after_value={"reason": reason},
        )
        self.db.commit()

    def _next_number(self) -> str:
        existing = self.db.query(Payment).count()
        return f"PAY-{existing + 1:05d}"
