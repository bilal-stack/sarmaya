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
from app.utils.records import describe_record

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

    def notify_sla_escalation(self, record, escalate_to_role: str, hours: int) -> None:
        """Tell the escalation role that a record has breached its SLA.

        The runner escalates every workflow, not only invoices. This assumed an
        invoice and read `invoice_number`; for anything else it raised, and the
        `except` below turned that into a log line nobody reads. The breach was
        recorded in the audit trail and the person who had to act was never
        told — which is the whole purpose of escalating.
        """
        try:
            label = describe_record(record)
            recipients = self._approver_emails(record.tenant_id, escalate_to_role)
            state = getattr(record.current_state, "value", record.current_state)
            amount = getattr(record, "total_amount", None) or getattr(
                record, "estimated_amount", None
            )
            subject = f"SLA breached: {label} awaiting action"
            body = (
                f"{label}{f' for {amount}' if amount is not None else ''} has been "
                f"waiting in {state} for more than {hours} hours.\n\n"
                f"It has been escalated to {escalate_to_role.upper()}."
            )
            self._send(recipients, subject, body)
        except Exception:
            logger.exception("Failed to send SLA escalation notification")

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
        if not settings.SMTP_ENABLED:
            # Not an error: delivery is opt-in, because this runs inside the
            # request that triggered it. Logged at info so a deployment that
            # expected mail can see immediately why none arrived, rather than
            # finding an exception swallowed further down.
            logger.info(
                "SMTP disabled; not sending '%s' to %s. Set SMTP_ENABLED=true "
                "once a mail server is configured.", subject, to_email,
            )
            return

        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM_EMAIL
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            ) as server:
                server.starttls()
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
            logger.info("Sent '%s' to %s", subject, to_email)
        except Exception:
            logger.exception("SMTP delivery to %s failed", to_email)
