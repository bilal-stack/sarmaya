from sqlalchemy.orm import Session
from typing import Dict, Any, Optional, List, Tuple
from datetime import date
from uuid import UUID
from decimal import Decimal

from app.repositories.invoice_repository import InvoiceRepository
from app.models.invoice import Invoice
from app.models.file import File as FileModel
from app.schemas.invoice import InvoiceCreate, InvoiceUpdate
from app.core.enums import InvoiceState
from app.services.workflow import transition_state
from app.services.policy import evaluate_approval_role
from app.services.audit import log_audit
from app.core.roles import has_permission, can_approve_amount, PERM_CREATE_INVOICE
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
        self.file_service = FileService(db)
    
    def get_invoice(self, invoice_id: UUID) -> Optional[Invoice]:
        """Get invoice by ID"""
        return self.repository.get_by_id(invoice_id)
    
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
        # Check for duplicate
        existing = self.repository.get_by_invoice_number(
            invoice_data.invoice_number,
            invoice_data.vendor_name
        )
        
        if existing:
            raise ValueError("Invoice with this number already exists for this vendor")
        
        # Create invoice
        invoice = Invoice(
            tenant_id=current_user["tenant_id"],
            invoice_number=invoice_data.invoice_number,
            vendor_name=invoice_data.vendor_name,
            invoice_date=invoice_data.invoice_date,
            due_date=invoice_data.due_date,
            total_amount=invoice_data.total_amount,
            tax_amount=invoice_data.tax_amount,
            description=invoice_data.description,
            current_state=InvoiceState.DRAFT.value,
            created_by=current_user["id"]
        )
        
        invoice = self.repository.create(invoice)
        self.repository.commit()
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
        for field, value in invoice_data.dict(exclude_unset=True).items():
            setattr(invoice, field, value)
        
        invoice = self.repository.update(invoice)
        self.repository.commit()
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
    
    def submit_for_approval(
        self,
        invoice_id: UUID,
        current_user: dict
    ) -> Tuple[Invoice, str]:
        """
        Submit invoice for approval
        
        Business logic:
        - Check permission (only AP_CLERK/ADMIN can submit)
        - Check state (must be draft/validated)
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
        
        # Check state
        if invoice.current_state not in [InvoiceState.DRAFT.value, InvoiceState.VALIDATED.value]:
            raise ValueError(
                f"Cannot submit invoice from {invoice.current_state} state"
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
        
        self.repository.commit()
        invoice = self.repository.refresh(invoice)
        
        # Determine required approver
        required_role = evaluate_approval_role(
            self.db,
            current_user["tenant_id"],
            float(invoice.total_amount or 0)
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
            after_value={"required_role": required_role}
        )
        
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
        
        # Check approval permissions
        can_approve, error_msg = can_approve_amount(
            current_user["role"],
            float(invoice.total_amount or 0)
        )
        
        if not can_approve:
            raise PermissionError(error_msg)
        
        # Check policy requirements
        required_role = evaluate_approval_role(
            self.db,
            current_user["tenant_id"],
            float(invoice.total_amount or 0)
        )
        
        if required_role and current_user["role"].lower() != required_role.lower() and current_user["role"] != "admin":
            raise PermissionError(
                f"{required_role.upper()} role required to approve this invoice"
            )
        
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
        self.repository.commit()
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
            after_value={"state": "approved"},
            workflow_step="approved",
            workflow_type="invoice",
            file_id=invoice.pdf_file_id
        )
        
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
        self.repository.commit()
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
        
        return invoice
    
    def mark_as_paid(self, invoice_id: UUID, current_user: dict) -> Invoice:
        """Mark invoice as paid"""
        invoice = self.repository.get_by_id(invoice_id)
        
        if not invoice:
            raise ValueError("Invoice not found")
        
        success = transition_state(
            db=self.db,
            obj=invoice,
            target_state=InvoiceState.PAID.value,
            user_id=current_user["id"]
        )
        
        if not success:
            raise ValueError("State transition failed")
        
        self.repository.commit()
        invoice = self.repository.refresh(invoice)
        
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action="marked_paid"
        )
        
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
        
        # Extract data using OCR
        try:
            ocr_result = extract_invoice_data_ocr(stored_path)
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
        
        # Check for exact duplicate
        existing = self.repository.find_duplicates_by_number(
            invoice_number=invoice_number,
            vendor_name=vendor_name
        )
        
        if existing:
            return {
                "success": False,
                "error": "duplicate_invoice_number",
                "message": f"Invoice {invoice_number} already exists for {vendor_name}",
                "existing_invoice_id": str(existing.id),
                "ocr_data": ocr_result
            }
        
        # Check for similar invoices (fuzzy duplicate detection)
        similar = self.repository.find_similar_invoices(
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            window_days=7,
            amount_tolerance=0.05
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
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            tax_amount=tax_amount,
            subtotal_amount=total_amount - tax_amount if tax_amount else total_amount,
            currency=currency,
            current_state=InvoiceState.DRAFT.value,
            ocr_confidence=confidence,
            ocr_extracted_data=ocr_result_sanitized,
            pdf_file_id=file_record.id,
            created_by=current_user["id"]
        )
        
        invoice = self.repository.create(invoice)
        self.repository.commit()
        invoice = self.repository.refresh(invoice)
        
        # Link file to invoice
        self.file_service.link_file_to_object(file_record, invoice.id)
        self.file_service.commit()
        
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
        
        # Return success response
        return {
            "success": True,
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "vendor_name": vendor_name,
            "invoice_date": str(invoice_date),
            "total_amount": total_amount,
            "tax_amount": tax_amount,
            "currency": currency,
            "current_state": invoice.current_state,
            "ocr_confidence": confidence,
            "ocr_data": ocr_result,
            "duplicate_warning": duplicate_warning,
            "file_id": str(file_record.id)
        }
