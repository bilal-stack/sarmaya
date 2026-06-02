import logging
import smtplib
from email.message import EmailMessage
from typing import List
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import UserRole
from app.models.invoice import Invoice
from app.models.user import User

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends transactional emails for invoice workflow events.

    Delivery is best-effort: every public method swallows its own errors and
    logs them, so a failed/misconfigured SMTP server can never break an
    invoice's approval, rejection, or submission. Recipients are resolved
    within the invoice's tenant.
    """

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------ #
    # Public workflow hooks
    # ------------------------------------------------------------------ #
    def notify_submitted_for_approval(
        self, invoice: Invoice, required_role: str
    ) -> None:
        """Tell the people who can approve this invoice that it's waiting."""
        try:
            recipients = self._approver_emails(invoice.tenant_id, required_role)
            subject = f"Invoice {invoice.invoice_number} awaiting your approval"
            body = (
                f"Invoice {invoice.invoice_number} from {invoice.vendor_name} "
                f"for {invoice.total_amount} is pending approval.\n\n"
                f"Required approver: {required_role.upper()}"
            )
            self._send(recipients, subject, body)
        except Exception:  # never let notification break the workflow
            logger.exception("Failed to send submitted-for-approval notification")

    def notify_approved(self, invoice: Invoice) -> None:
        """Tell the invoice's creator that it was approved."""
        try:
            recipients = self._user_emails([invoice.created_by])
            subject = f"Invoice {invoice.invoice_number} approved"
            body = (
                f"Invoice {invoice.invoice_number} from {invoice.vendor_name} "
                f"for {invoice.total_amount} has been approved."
            )
            self._send(recipients, subject, body)
        except Exception:
            logger.exception("Failed to send approved notification")

    def notify_rejected(self, invoice: Invoice, reason: str) -> None:
        """Tell the invoice's creator that it was rejected, with the reason."""
        try:
            recipients = self._user_emails([invoice.created_by])
            subject = f"Invoice {invoice.invoice_number} rejected"
            body = (
                f"Invoice {invoice.invoice_number} from {invoice.vendor_name} "
                f"was rejected.\n\nReason: {reason}"
            )
            self._send(recipients, subject, body)
        except Exception:
            logger.exception("Failed to send rejected notification")

    # ------------------------------------------------------------------ #
    # Recipient resolution
    # ------------------------------------------------------------------ #
    def _approver_emails(self, tenant_id: UUID, required_role: str) -> List[str]:
        """Active users in the tenant who can act on the approval: anyone with
        the required role (e.g. manager/cfo) plus admins."""
        roles = {UserRole.ADMIN.value}
        if required_role:
            roles.add(required_role.lower())

        users = (
            self.db.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role.in_(roles),
            )
            .all()
        )
        return [u.email for u in users if u.email]

    def _user_emails(self, user_ids: List[UUID]) -> List[str]:
        users = (
            self.db.query(User)
            .filter(User.id.in_([uid for uid in user_ids if uid]))
            .all()
        )
        return [u.email for u in users if u.email]

    # ------------------------------------------------------------------ #
    # Delivery
    # ------------------------------------------------------------------ #
    def _send(self, to_emails: List[str], subject: str, body: str) -> None:
        recipients = [e for e in dict.fromkeys(to_emails) if e]  # dedupe, drop blanks
        if not recipients:
            logger.info("No recipients for '%s'; skipping email", subject)
            return
        for email in recipients:
            self._deliver(email, subject, body)

    def _deliver(self, to_email: str, subject: str, body: str) -> None:
        """Send a single email via SMTP. Isolated so tests can patch it and so
        one bad address doesn't stop the rest of the batch."""
        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
                server.starttls()
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
            logger.info("Sent '%s' to %s", subject, to_email)
        except Exception:
            logger.exception("SMTP delivery to %s failed", to_email)
