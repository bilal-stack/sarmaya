from sqlalchemy.orm import Session
from typing import Dict, Any, Optional, List, Tuple
from datetime import date
from uuid import UUID
from decimal import Decimal

from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.vendor_repository import VendorRepository
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.file import File as FileModel
from app.schemas.invoice import InvoiceCreate, InvoiceUpdate
from app.core.enums import InvoiceState, VendorStatus
from app.services.workflow import transition_state
from app.services.policy import explain_approval_routing
from app.services.policy_eval import record_approval_routing_eval
from app.services.correlation import new_correlation_id
from app.services.audit import log_audit
from app.services import sod
from app.services.delegation import resolve_authority, resolve_permission
from app.services.notification_service import NotificationService
from app.core.roles import (
    has_permission,
    PERM_CREATE_INVOICE,
    PERM_UPDATE_INVOICE,
    PERM_DELETE_INVOICE,
    PERM_APPROVE_INVOICE,
    PERM_REJECT_INVOICE,
    PERM_MARK_PAID_INVOICE,
)
from app.utils.datetime_helpers import utc_now
from app.services.file_service import FileService
from app.services.ocr import extract_invoice_data_ocr
from app.core.config import settings
from app.utils.datetime_helpers import parse_date, sanitize_for_json


class InvoiceService:
    """Service layer for invoice business logic"""
    
    def __init__(self, db: Session):
        self.db = db
        self.repository = InvoiceRepository(db)
        self.vendor_repository = VendorRepository(db)
        self.file_service = FileService(db)
        self.notification_service = NotificationService(db)

    def get_invoice(self, invoice_id: UUID) -> Optional[Invoice]:
        """Get invoice by ID"""
        return self.repository.get_by_id(invoice_id)

    def _resolve_vendor(
        self,
        current_user: dict,
        vendor_id: Optional[UUID] = None,
        vendor_name: Optional[str] = None,
        auto_create: bool = False,
    ) -> Tuple[Optional[UUID], Optional[str], Optional[str]]:
        """Resolve the vendor master record for an invoice.

        Returns ``(vendor_id, canonical_name, status)``. Resolution order:
          1. explicit ``vendor_id`` — must exist (raises ValueError otherwise);
          2. case-insensitive match on ``vendor_name``;
          3. if ``auto_create``, create a PENDING_VERIFICATION vendor from the
             name so every invoice links to a vendor master record. The record
             is unusable (can't be approved/paid against) until a human with
             vendors.manage verifies and activates it.

        The vendor (when created) is only flushed, not committed, so it shares
        the invoice's transaction. Callers decide what to do with a BLOCKED
        status; this method does not reject on its own.
        """
        vendor = None
        if vendor_id:
            vendor = self.vendor_repository.get_by_id(vendor_id)
            if not vendor:
                raise ValueError("Vendor not found")
        elif vendor_name and vendor_name.strip():
            vendor = self.vendor_repository.get_by_legal_name(vendor_name)

        if vendor is None and auto_create:
            name = (vendor_name or "").strip()
            if name:
                vendor = self.vendor_repository.create(
                    Vendor(
                        tenant_id=current_user["tenant_id"],
                        legal_name=name,
                        status=VendorStatus.PENDING_VERIFICATION,
                        created_by=current_user["id"],
                    )
                )
                log_audit(
                    db=self.db,
                    tenant_id=current_user["tenant_id"],
                    user_id=current_user["id"],
                    object_type="vendor",
                    object_id=vendor.id,
                    action="created",
                    after_value={"legal_name": vendor.legal_name, "source": "invoice_upload"},
                )

        if vendor is None:
            return None, vendor_name, None
        status = vendor.status.value if hasattr(vendor.status, "value") else vendor.status
        return vendor.id, vendor.legal_name, status

    def _assert_vendor_active(
        self,
        invoice: Invoice,
        action: str,
        current_user: dict,
        audit_action: str,
    ) -> None:
        """Governance gate: block a financial action (approve / mark-paid) when
        the invoice's vendor master record is not ACTIVE.

        A vendor created from an upload starts PENDING_VERIFICATION and must be
        verified/activated by a human (vendors.manage) before money moves; a
        BLOCKED vendor is never payable. Draft/submit are deliberately not gated
        so AP work can proceed while verification happens in parallel.

        Denials are audited (``audit_action``) and committed on their own so the
        attempt to move money against an unverified vendor leaves a permanent
        trail even though the action itself is rejected.
        """
        if not invoice.vendor_id:
            self._audit_block(invoice, current_user, audit_action, "no_vendor_link")
            raise ValueError(
                f"Cannot {action}: invoice is not linked to a vendor master record"
            )

        vendor = self.vendor_repository.get_by_id(invoice.vendor_id)
        if not vendor:
            self._audit_block(invoice, current_user, audit_action, "vendor_missing")
            raise ValueError(
                f"Cannot {action}: linked vendor no longer exists"
            )

        status = vendor.status.value if hasattr(vendor.status, "value") else vendor.status
        if status != VendorStatus.ACTIVE.value:
            self._audit_block(
                invoice, current_user, audit_action, f"vendor_{status}",
                vendor_id=vendor.id, vendor_name=vendor.legal_name,
            )
            raise PermissionError(
                f"Cannot {action}: vendor '{vendor.legal_name}' is {status}. "
                "The vendor must be verified and activated before this invoice "
                "can be approved or paid."
            )

    def _audit_block(
        self,
        invoice: Invoice,
        current_user: dict,
        audit_action: str,
        reason: str,
        vendor_id: Optional[UUID] = None,
        vendor_name: Optional[str] = None,
    ) -> None:
        """Persist a governance denial as its own committed audit record."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action=audit_action,
            comment=f"Blocked: {reason}",
            workflow_type="invoice",
            after_value={
                "reason": reason,
                "vendor_id": str(vendor_id) if vendor_id else None,
                "vendor_name": vendor_name,
            },
        )
        self.repository.commit()

    def _assert_duplicate_resolved(self, invoice: Invoice) -> None:
        """Soft duplicate gate: block approval while the invoice carries an
        unacknowledged potential-duplicate flag. Cleared via resolve_duplicate,
        which records the override reason in the audit trail."""
        if invoice.potential_duplicate_id and not invoice.duplicate_acknowledged:
            raise ValueError(
                "Cannot approve: this invoice is flagged as a potential duplicate. "
                "Review and override it with a reason first."
            )

    def resolve_duplicate(
        self, invoice_id: UUID, reason: str, current_user: dict
    ) -> Invoice:
        """Override a flagged potential duplicate, recording the reviewer's
        reason in the audit trail (per spec: a fuzzy duplicate is a soft warning
        that is overridable with a logged reason). Unblocks approval."""
        invoice = self.repository.get_by_id(invoice_id)
        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_APPROVE_INVOICE):
            raise PermissionError(
                "You do not have permission to override duplicate warnings"
            )

        if not invoice.potential_duplicate_id:
            raise ValueError("Invoice has no potential duplicate to resolve")

        if not reason or not reason.strip():
            raise ValueError("An override reason is required")

        if invoice.duplicate_acknowledged:
            return invoice

        invoice.duplicate_acknowledged = True
        self.repository.update(invoice)
        self.repository.commit()
        invoice = self.repository.refresh(invoice)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="duplicate_overridden",
            comment=reason.strip(),
            workflow_type="invoice",
            after_value={"potential_duplicate_id": str(invoice.potential_duplicate_id)},
        )
        self.repository.commit()

        return invoice

    def list_invoices(
        self,
        status_filter: Optional[InvoiceState] = None,
        vendor_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Invoice], int]:
        """List invoices with filters"""
        return self.repository.list_invoices(
            status_filter=status_filter,
            vendor_name=vendor_name,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset
        )
    
    def create_manual_invoice(
        self,
        invoice_data: InvoiceCreate,
        current_user: dict
    ) -> Invoice:
        """
        Create invoice manually
        
        Business logic:
        - Check for duplicates
        - Create invoice
        - Log audit
        """
        if not has_permission(current_user["role"], PERM_CREATE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to create invoices"
            )

        # Link to a vendor master record (explicit id wins, else name match,
        # else auto-create a pending-verification vendor so vendor_id is always
        # set and dedup can key on it).
        vendor_id, vendor_name, vendor_status = self._resolve_vendor(
            current_user,
            vendor_id=invoice_data.vendor_id,
            vendor_name=invoice_data.vendor_name,
            auto_create=True,
        )
        if vendor_status == VendorStatus.BLOCKED.value:
            raise ValueError("Cannot create an invoice for a blocked vendor")

        # Check for duplicate — keyed on vendor_id so the same number under a
        # different name spelling is still caught.
        existing = self.repository.find_duplicate_by_number_and_vendor_id(
            invoice_data.invoice_number, vendor_id
        )

        if existing:
            raise ValueError("Invoice with this number already exists for this vendor")

        # Create invoice
        invoice = Invoice(
            tenant_id=current_user["tenant_id"],
            invoice_number=invoice_data.invoice_number,
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            invoice_date=invoice_data.invoice_date,
            due_date=invoice_data.due_date,
            total_amount=invoice_data.total_amount,
            tax_amount=invoice_data.tax_amount,
            description=invoice_data.description,
            current_state=InvoiceState.DRAFT.value,
            correlation_id=new_correlation_id(),  # this invoice starts a chain
            created_by=current_user["id"]
        )
        
        invoice = self.repository.create(invoice)
        # Flush, do not commit: the audit entry below belongs to the same
        # transaction as the row it describes.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="created",
            after_value={
                "invoice_number": invoice.invoice_number,
                "amount": str(invoice.total_amount)
            }
        )

        self.repository.commit()
        return invoice
    
    def update_invoice(
        self,
        invoice_id: UUID,
        invoice_data: InvoiceUpdate,
        current_user: dict
    ) -> Invoice:
        """
        Update invoice
        
        Business logic:
        - Only allow edits in draft/rejected state
        - Track changes for audit
        """
        invoice = self.repository.get_by_id(invoice_id)

        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_UPDATE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to update invoices"
            )

        # Check if editable
        if invoice.current_state not in [InvoiceState.DRAFT.value, InvoiceState.REJECTED.value]:
            raise ValueError(f"Cannot edit invoice in {invoice.current_state} state")
        
        # Store before values
        before_value = {
            "total_amount": str(invoice.total_amount),
            "vendor_name": invoice.vendor_name,
            "invoice_number": invoice.invoice_number
        }
        
        # Update fields
        for field, value in invoice_data.model_dump(exclude_unset=True).items():
            setattr(invoice, field, value)
        
        invoice = self.repository.update(invoice)
        # Flush, do not commit: the audit entry below belongs to the same
        # transaction as the change it describes.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="updated",
            before_value=before_value,
            after_value={
                "total_amount": str(invoice.total_amount),
                "vendor_name": invoice.vendor_name,
                "invoice_number": invoice.invoice_number
            }
        )

        self.repository.commit()
        return invoice
    
    def delete_invoice(self, invoice_id: UUID, current_user: dict) -> None:
        """
        Delete invoice
        
        Business logic:
        - Only allow delete in draft state
        """
        invoice = self.repository.get_by_id(invoice_id)

        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_DELETE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to delete invoices"
            )

        if invoice.current_state != InvoiceState.DRAFT.value:
            raise ValueError("Can only delete draft invoices")
        
        # Log before delete
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="deleted",
            before_value={"invoice_number": invoice.invoice_number}
        )
        
        self.repository.delete(invoice)
        self.repository.commit()
    
    def validate_invoice(
        self,
        invoice_id: UUID,
        current_user: dict
    ) -> Invoice:
        """Validate a draft invoice (draft → validated).

        The explicit field-completeness checkpoint the MVP workflow requires
        before an invoice can be submitted for approval (no skipping straight
        from draft to pending). Confirms the core fields OCR/data-entry must
        produce are present; the linked vendor's verification status is enforced
        later by the approval gate, not here.
        """
        invoice = self.repository.get_by_id(invoice_id)

        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_CREATE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to validate invoices"
            )

        if invoice.current_state != InvoiceState.DRAFT.value:
            raise ValueError(
                f"Cannot validate invoice from {invoice.current_state} state"
            )

        missing = self._missing_required_fields(invoice)
        if missing:
            raise ValueError(
                "Cannot validate invoice; missing required fields: "
                + ", ".join(missing)
            )

        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.VALIDATED.value,
            user_id=current_user["id"]
        )
        if not success:
            raise ValueError("State transition failed")

        # Flush, do not commit: the audit entry below belongs to the same
        # transaction as the transition it describes, so an invoice can never
        # change state without leaving a trail.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="validated",
            workflow_step=invoice.current_state,
            workflow_type="invoice",
        )

        self.repository.commit()
        return invoice

    @staticmethod
    def _missing_required_fields(invoice: Invoice) -> List[str]:
        """Names of the fields an invoice must carry before it can be validated."""
        missing: List[str] = []
        if not invoice.vendor_id:
            missing.append("vendor")
        if not (invoice.invoice_number or "").strip():
            missing.append("invoice_number")
        if not invoice.invoice_date:
            missing.append("invoice_date")
        if invoice.total_amount is None or invoice.total_amount <= 0:
            missing.append("total_amount")
        return missing

    def submit_for_approval(
        self,
        invoice_id: UUID,
        current_user: dict
    ) -> Tuple[Invoice, str]:
        """
        Submit invoice for approval

        Business logic:
        - Check permission (only AP_CLERK/ADMIN can submit)
        - Check state (must be validated — invoices must pass the validate step
          first; no skipping straight from draft)
        - Transition workflow
        - Determine required approver

        Returns: (invoice, required_approver_role)
        """
        invoice = self.repository.get_by_id(invoice_id)

        if not invoice:
            raise ValueError("Invoice not found")

        # Check permission
        if not has_permission(current_user["role"], PERM_CREATE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to submit invoices"
            )

        # Check state — must be validated first (no draft → pending skip)
        if invoice.current_state != InvoiceState.VALIDATED.value:
            raise ValueError(
                f"Cannot submit invoice from {invoice.current_state} state; "
                "it must be validated first"
            )
        
        # Transition state
        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.PENDING_APPROVAL.value,
            user_id=current_user["id"]
        )
        
        if not success:
            raise ValueError(f"State transition failed")
        
        # Flush, do not commit: the routing snapshot and audit entry below
        # belong to the same transaction as the transition they describe.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Determine required approver and snapshot the routing decision as it
        # stood now, so the audit trail records the policy actually applied (not
        # whatever the policy is recomputed to later).
        routing = explain_approval_routing(
            self.db,
            current_user["tenant_id"],
            float(invoice.total_amount or 0)
        )
        required_role = routing["required_role"]

        # Snapshot the evaluation itself (rule version + inputs + decision), so
        # the routing stays reproducible after the policy is edited.
        record_approval_routing_eval(
            self.db, current_user["tenant_id"], routing,
            float(invoice.total_amount or 0), "invoice", invoice.id, current_user["id"],
        )

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="submitted_for_approval",
            workflow_step=invoice.current_state,
            workflow_type="invoice",
            after_value={
                "required_role": required_role,
                "policy_name": routing["policy_name"],
                "routing_reason": routing["reason"],
            }
        )

        self.repository.commit()

        # Notify only once the transition and its trail are durable.
        self.notification_service.notify_submitted_for_approval(invoice, required_role)

        return invoice, required_role
    
    def approve_invoice(
        self,
        invoice_id: UUID,
        current_user: dict
    ) -> Invoice:
        """
        Approve invoice
        
        Business logic:
        - Check if already approved
        - Check if in correct state
        - Check role permissions and limits
        - Check policy requirements
        - Transition workflow
        """
        invoice = self.repository.get_by_id(invoice_id)
        
        if not invoice:
            raise ValueError("Invoice not found")
        
        # Check if already approved
        if invoice.current_state == InvoiceState.APPROVED.value:
            raise ValueError("Invoice is already approved")
        
        # Check state
        if invoice.current_state != InvoiceState.PENDING_APPROVAL.value:
            raise ValueError(
                f"Cannot approve invoice in '{invoice.current_state}' state"
            )

        # Authorize first — fail fast before the governance gates run (those
        # write/commit audit records, which a non-approver shouldn't be able to
        # trigger).
        can_approve, perm_delegation = resolve_permission(
            self.db, current_user, PERM_APPROVE_INVOICE
        )
        if not can_approve:
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to approve invoices"
            )

        # Segregation of duties binds the person acting, not the role they
        # borrowed: delegated authority never makes you your own checker.
        if sod.violates_self_invoice_approval(invoice, current_user):
            self._audit_block(invoice, current_user, "approval_blocked", "sod_self_approval")
            raise PermissionError(
                "Segregation of duties: you cannot approve an invoice you created."
            )

        # Governance gate: vendor must be verified/active before approval
        self._assert_vendor_active(
            invoice, "approve invoice", current_user, "approval_blocked"
        )

        # Governance gate: a flagged potential duplicate must be reviewed and
        # overridden (with a logged reason) before approval.
        self._assert_duplicate_resolved(invoice)

        # Configuration-first approval routing: the required approver role is
        # derived from the tenant's approval_limit policies, so the amount limit
        # lives in editable config, not hardcoded here. Capture the routing
        # explanation to snapshot in the audit trail at decision time.
        routing = explain_approval_routing(
            self.db,
            current_user["tenant_id"],
            float(invoice.total_amount or 0)
        )
        required_role = routing["required_role"]

        record_approval_routing_eval(
            self.db, current_user["tenant_id"], routing,
            float(invoice.total_amount or 0), "invoice", invoice.id, current_user["id"],
        )

        authorised, role_delegation = resolve_authority(self.db, current_user, required_role)
        if not authorised:
            raise PermissionError(
                f"{required_role.upper()} role required to approve this invoice"
            )
        # Whose authority actually carried the approval (None when their own).
        acting_delegation = role_delegation or perm_delegation
        
        # Transition state
        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.APPROVED.value,
            user_id=current_user["id"]
        )
        
        if not success:
            raise ValueError("State transition failed")
        
        invoice.approved_by = current_user["id"]
        invoice.approved_at = utc_now()
        
        self.repository.update(invoice)
        # Flush, do not commit: an approval and its audit entry must land
        # together — this is the record that money may move against.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="approved",
            before_value={"state": "pending_approval"},
            after_value={
                "state": "approved",
                "required_role": required_role,
                "policy_name": routing["policy_name"],
                "routing_reason": routing["reason"],
                **({
                    "acted_under_delegation": str(acting_delegation.id),
                    "delegated_authority_of": str(acting_delegation.from_user_id),
                } if acting_delegation else {}),
            },
            workflow_step="approved",
            workflow_type="invoice",
            file_id=invoice.pdf_file_id
        )

        self.repository.commit()

        # Notify only once the approval and its trail are durable.
        self.notification_service.notify_approved(invoice)

        return invoice
    
    def reject_invoice(
        self,
        invoice_id: UUID,
        reason: str,
        current_user: dict
    ) -> Invoice:
        """
        Reject invoice
        
        Business logic:
        - Check permissions
        - Check state
        - Transition workflow
        - Store rejection reason
        """
        invoice = self.repository.get_by_id(invoice_id)
        
        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_REJECT_INVOICE):
            raise PermissionError("You do not have permission to reject invoices")

        if not reason or not reason.strip():
            raise ValueError("Rejection reason is required")
        
        # Check state
        if invoice.current_state != InvoiceState.PENDING_APPROVAL.value:
            raise ValueError(
                f"Can only reject invoices in pending_approval state (current: {invoice.current_state})"
            )
        
        # Transition state
        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.REJECTED.value,
            user_id=current_user["id"]
        )
        
        if not success:
            raise ValueError("State transition failed")
        
        invoice.rejection_reason = reason.strip()
        
        self.repository.update(invoice)
        # Flush, do not commit: the rejection and its stated reason belong to
        # the same transaction.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="rejected",
            workflow_step=invoice.current_state,
            workflow_type="invoice",
            comment=reason
        )

        self.repository.commit()

        # Notify only once the rejection and its trail are durable.
        self.notification_service.notify_rejected(invoice, reason)

        return invoice
    
    def mark_as_paid(self, invoice_id: UUID, current_user: dict) -> Invoice:
        """Mark invoice as paid"""
        invoice = self.repository.get_by_id(invoice_id)

        if not invoice:
            raise ValueError("Invoice not found")

        if not has_permission(current_user["role"], PERM_MARK_PAID_INVOICE):
            raise PermissionError("You do not have permission to mark invoices as paid")

        # Governance gate: vendor must be verified/active before payment
        self._assert_vendor_active(
            invoice, "mark invoice as paid", current_user, "payment_blocked"
        )

        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.PAID.value,
            user_id=current_user["id"]
        )
        
        if not success:
            raise ValueError("State transition failed")
        
        # Flush, do not commit: marking an invoice paid is the last financial
        # step, so it must not be recordable without its audit entry.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="marked_paid"
        )

        self.repository.commit()
        return invoice
    
    def get_stats(self) -> Dict[str, Any]:
        """Get invoice statistics"""
        stats_by_status = self.repository.get_stats_by_status()
        
        stats = {
            "by_status": {},
            "total_invoices": 0,
            "total_amount": 0.0
        }
        
        for state, count, total in stats_by_status:
            stats["by_status"][state] = {
                "count": count,
                "amount": float(total or 0)
            }
            stats["total_invoices"] += count
            stats["total_amount"] += float(total or 0)
        
        # This month stats
        from datetime import date
        today = date.today()
        month_start = date(today.year, today.month, 1)
        
        month_count, month_total = self.repository.get_monthly_stats(month_start)
        
        stats["this_month"] = {
            "count": month_count,
            "amount": month_total
        }
        
        return stats
    
    def get_pending_approvals(self) -> List[Invoice]:
        """Get pending approval invoices"""
        return self.repository.get_pending_approvals()

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Headline dashboard figures: invoices awaiting approval, this month's
        volume, and the top vendors by spend. Tenant isolation is enforced by
        RLS on the request-scoped session."""
        today = date.today()
        month_start = date(today.year, today.month, 1)
        month_count, month_total = self.repository.get_monthly_stats(month_start)

        return {
            "pending_approvals": self.repository.count_by_state(
                InvoiceState.PENDING_APPROVAL.value
            ),
            "invoices_this_month": {
                "count": month_count,
                "total_amount": month_total,
            },
            "top_vendors": [
                {"vendor_name": name, "total_amount": total}
                for name, total in self.repository.get_top_vendors(limit=5)
            ],
        }

    def get_invoices_blocked_on_vendor(self, limit: int = 100) -> List[Invoice]:
        """Pending-approval invoices stuck because their vendor isn't ACTIVE yet.

        The reviewer worklist for the governance gate: each entry unblocks once
        a user with vendors.manage verifies/activates the linked vendor.
        """
        return self.repository.get_blocked_on_vendor_verification(limit)
    
    def upload_and_extract_invoice(
        self,
        filename: str,
        content: bytes,
        mime_type: str,
        current_user: dict
    ) -> Dict[str, Any]:
        """
        Upload PDF, extract data with OCR, create invoice
        
        Business logic:
        1. Validate file type
        2. Save file to storage
        3. Extract data using OCR
        4. Check for duplicates
        5. Create invoice
        6. Link file to invoice
        7. Log audit
        
        Returns:
            Upload response dict
        """
        if not has_permission(current_user["role"], PERM_CREATE_INVOICE):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to create invoices"
            )

        # Save file
        file_record, stored_path, file_hash = self.file_service.save_file(
            tenant_id=current_user["tenant_id"],
            filename=filename,
            content=content,
            mime_type=mime_type,
            object_type="invoice",
            uploaded_by=current_user["id"]
        )
        self.file_service.commit()

        try:
            return self._extract_and_create_invoice(
                file_record=file_record,
                stored_path=stored_path,
                file_hash=file_hash,
                current_user=current_user,
            )
        except Exception:
            # Clean up the orphaned file (disk + DB) before propagating, so a
            # failed OCR/creation doesn't leave a dangling upload behind.
            self.file_service.delete_file(file_record, stored_path)
            self.file_service.commit()
            raise

    def _extract_and_create_invoice(
        self,
        file_record: FileModel,
        stored_path: str,
        file_hash: str,
        current_user: dict,
    ) -> Dict[str, Any]:
        # Extract data using OCR (tenant context lets the AI enhancement be
        # recorded in the AI action log with provenance).
        try:
            ocr_result = extract_invoice_data_ocr(
                stored_path,
                db=self.db,
                tenant_id=current_user["tenant_id"],
                user_id=current_user["id"],
            )
        except Exception as e:
            raise RuntimeError(f"OCR extraction failed: {str(e)}")

        # Sanitize OCR data for JSON storage
        ocr_result_sanitized = sanitize_for_json(ocr_result)
        
        # Parse OCR results
        vendor_name = ocr_result.get("vendor_name", "Unknown Vendor")
        invoice_number = ocr_result.get("invoice_number", f"INV-{file_record.id}")
        invoice_date_str = ocr_result.get("invoice_date")
        invoice_date = parse_date(invoice_date_str) if invoice_date_str else date.today()
        total_amount = ocr_result.get("total_amount", 0.0)
        tax_amount = ocr_result.get("tax_amount", 0.0)
        confidence = ocr_result.get("confidence", 0)
        currency = ocr_result.get("currency", "PKR")
        
        # Resolve the vendor master record, auto-creating a pending-verification
        # vendor when the name doesn't match an existing one. This guarantees
        # vendor_id is always set so every duplicate check keys on it (immune to
        # name/OCR spelling drift) rather than free-text names.
        vendor_id, vendor_name, vendor_status = self._resolve_vendor(
            current_user,
            vendor_name=vendor_name,
            auto_create=True,
        )

        # Check for exact duplicate, keyed on vendor_id.
        existing = self.repository.find_duplicate_by_number_and_vendor_id(
            invoice_number=invoice_number,
            vendor_id=vendor_id,
        )

        if existing:
            return {
                "success": False,
                "error": "duplicate_invoice_number",
                "message": f"Invoice {invoice_number} already exists for {vendor_name}",
                "existing_invoice_id": str(existing.id),
                "ocr_data": ocr_result
            }

        # Check for similar invoices (fuzzy duplicate detection) — a soft
        # warning surfaced to the human approver, not a hard block.
        similar = self.repository.find_similar_by_vendor_id(
            vendor_id=vendor_id,
            invoice_date=invoice_date,
            total_amount=total_amount,
            window_days=7,
            amount_tolerance=0.05,
        )

        duplicate_warning = None
        if similar:
            from app.utils.money import money_to_float
            duplicate_warning = {
                "invoice_id": str(similar.id),
                "invoice_number": similar.invoice_number,
                "amount": money_to_float(similar.total_amount),
                "date": str(similar.invoice_date),
                "message": "Possible duplicate detected (similar amount & date)"
            }

        # Create invoice
        invoice = Invoice(
            tenant_id=current_user["tenant_id"],
            invoice_number=invoice_number,
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            tax_amount=tax_amount,
            subtotal_amount=total_amount - tax_amount if tax_amount else total_amount,
            currency=currency,
            current_state=InvoiceState.DRAFT.value,
            correlation_id=new_correlation_id(),  # this invoice starts a chain
            ocr_confidence=confidence,
            ocr_extracted_data=ocr_result_sanitized,
            line_items=ocr_result.get("line_items", []),  # ✅ ADD
            pdf_file_id=file_record.id,
            # Soft duplicate flag: created in DRAFT regardless, but approval is
            # blocked until a reviewer overrides it with a logged reason.
            potential_duplicate_id=similar.id if similar else None,
            created_by=current_user["id"]
        )
        
        invoice = self.repository.create(invoice)
        # Flush, do not commit: the file link and the upload audit entry below
        # belong to the same transaction as the invoice they describe.
        self.db.flush()
        invoice = self.repository.refresh(invoice)

        # Link file to invoice
        self.file_service.link_file_to_object(file_record, invoice.id)
        self.db.flush()

        # Log audit
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="uploaded",
            after_value={
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
                "amount": str(invoice.total_amount),
                "ocr_confidence": confidence
            },
            workflow_step=invoice.current_state,
            workflow_type="invoice",
            file_id=file_record.id,
            document_hash=file_hash,
            file_path=stored_path,
            ai_assisted=ocr_result.get("ai_enhanced", False),
            ai_provider=settings.AI_PROVIDER if ocr_result.get("ai_enhanced") else settings.OCR_PROVIDER,
            ai_confidence=confidence
        )

        # Commit last, so the invoice, its file link and the upload audit entry
        # (which carries the document hash) all land together.
        self.repository.commit()

        # Return success response
        return {
            "success": True,
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "vendor_id": str(vendor_id) if vendor_id else None,
            "vendor_name": vendor_name,
            "vendor_status": vendor_status,
            "invoice_date": str(invoice_date),
            "total_amount": total_amount,
            "tax_amount": tax_amount,
            "currency": currency,
            "current_state": invoice.current_state,
            "ocr_confidence": confidence,
            "ocr_data": ocr_result,
            "field_explanations": ocr_result.get("field_explanations"),
            "line_items": ocr_result.get("line_items", []),  # ✅ ENSURE this is present
            "duplicate_warning": duplicate_warning,
            "file_id": str(file_record.id)
        }
