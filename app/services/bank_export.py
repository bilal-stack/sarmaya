"""Bank payment file export.

The system produces an instruction file; a treasury user uploads it to their
own corporate banking portal. Nothing here contacts a bank, holds credentials,
or moves money — the boundary is deliberate, and it is where responsibility
transfers to a human.

CSV today. The column set is the one ISO 20022 pain.001 needs (creditor name,
account, IBAN, BIC, amount, currency, remittance reference), so adding the XML
format later is a serialiser change rather than a data-model change.

Only a released run can be exported. Exporting a draft would put an instruction
in someone's hands that nobody authorised, which is the failure this module's
maker-checker exists to prevent.

The file's SHA-256 is recorded on the payment, so what was handed to the bank
is evidenced rather than asserted: regenerating it later and comparing hashes
shows whether anything underneath has changed.
"""
import csv
import hashlib
import io
import logging
from typing import Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.core.enums import PaymentState
from app.core.roles import has_permission, PERM_VIEW_PAYMENT
from app.services.audit import log_audit
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

#: Mirrors the fields pain.001 requires, so the XML format is a serialiser
#: change later rather than a schema change.
COLUMNS = [
    "payment_reference",
    "payment_date",
    "creditor_name",
    "creditor_account_name",
    "creditor_account_number",
    "creditor_iban",
    "creditor_bic",
    "creditor_bank",
    "amount",
    "currency",
    "remittance_information",
]


class BankExportService:
    def __init__(self, db: Session):
        self.db = db

    def export_csv(self, payment_id: UUID, current_user: dict) -> Tuple[str, str, str]:
        """Return (filename, csv_text, sha256) for a released run."""
        if not has_permission(current_user["role"], PERM_VIEW_PAYMENT):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to "
                "export payment files"
            )

        payment = self.db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise ValueError("Payment not found")

        state = str(getattr(payment.current_state, "value", payment.current_state)).lower()
        if state != PaymentState.RELEASED.value:
            raise ValueError(
                f"Cannot export a payment in {state} state. Only a released run "
                "has been authorised, and an unauthorised instruction must never "
                "reach a bank."
            )

        currency = getattr(payment.currency, "value", payment.currency) or ""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()

        for line in payment.lines:
            invoice = line.invoice
            writer.writerow({
                "payment_reference": payment.payment_number,
                "payment_date": payment.payment_date.isoformat(),
                "creditor_name": line.vendor_name,
                "creditor_account_name": line.bank_account_name or "",
                "creditor_account_number": line.bank_account_number or "",
                "creditor_iban": line.iban or "",
                "creditor_bic": line.swift_code or "",
                "creditor_bank": line.bank_name or "",
                "amount": f"{line.amount:.2f}",
                "currency": currency,
                # What the vendor needs to match the receipt to their own
                # ledger; without it they cannot tell what has been settled.
                "remittance_information": (
                    invoice.invoice_number if invoice else str(line.invoice_id)
                ),
            })

        content = buffer.getvalue()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Record the first generation. Re-exporting is allowed — files get lost
        # — but the hash of what was originally produced is not overwritten, so
        # a later export that differs is detectable.
        first_export = payment.bank_file_hash is None
        if first_export:
            payment.bank_file_hash = digest
            payment.bank_file_generated_at = utc_now()
            self.db.add(payment)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="payment",
            object_id=payment.id,
            action="bank_file_generated",
            workflow_type="payment",
            after_value={
                "payment_number": payment.payment_number,
                "lines": len(payment.lines),
                "total_amount": str(payment.total_amount),
                "sha256": digest,
                "matches_original": digest == payment.bank_file_hash,
                "first_export": first_export,
            },
        )
        self.db.commit()

        filename = f"{payment.payment_number}.csv"
        return filename, content, digest
