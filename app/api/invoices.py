from venv import logger
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query, Body
from sqlalchemy.orm import Session
from datetime import date, timedelta, datetime
from typing import Optional, List
from decimal import Decimal
from uuid import UUID
from app.api.deps import get_current_user, get_db_session
from app.models.invoice import Invoice
from app.models.file import File as FileModel
from app.schemas.invoice import (
    InvoiceResponse,
    InvoiceCreate,
    InvoiceUpdate,
    InvoiceUploadResponse,
    InvoiceListResponse,
    InvoiceStats
)
from app.services.storage import save_file
from app.services.ocr import extract_invoice_data_ocr
from app.services.policy import evaluate_approval_role, check_user_can_approve, check_user_can_reject
from app.services.workflow import transition_state
from app.services.audit import log_audit
from app.core.enums import InvoiceState
from app.core.config import settings
from app.utils.datetime_helpers import utc_now, parse_date, sanitize_for_json

router = APIRouter(prefix="/invoices", tags=["Invoices"])


# ============================================
# HELPER FUNCTIONS
# ============================================

def money_to_float(v) -> float:
    """Convert Decimal to float"""
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except Exception:
        return 0.0


# ============================================
# INVOICE CRUD
# ============================================

@router.get("/", response_model=List[InvoiceListResponse])
def list_invoices(
    status_filter: Optional[InvoiceState] = None,
    vendor_name: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    List all invoices with filters
    RLS automatically filters by tenant
    """
    query = db.query(Invoice)
    
    if status_filter:
        query = query.filter(Invoice.current_state == status_filter.value)
    
    if vendor_name:
        query = query.filter(Invoice.vendor_name.ilike(f"%{vendor_name}%"))
    
    if start_date:
        query = query.filter(Invoice.invoice_date >= start_date)
    
    if end_date:
        query = query.filter(Invoice.invoice_date <= end_date)
    
    total = query.count()
    invoices = query.order_by(Invoice.created_at.desc()).offset(offset).limit(limit).all()
    
    return invoices


@router.get("/{invoice_id}", response_model=InvoiceResponse)
def get_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get single invoice details
    RLS ensures user can only access their tenant's invoices
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found"
        )
    
    return invoice


@router.post("/", response_model=InvoiceResponse, status_code=status.HTTP_201_CREATED)
def create_invoice_manual(
    invoice_data: InvoiceCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Create invoice manually (without PDF upload)
    For cases where you want to enter data directly
    """
    
    # Check for duplicate invoice number
    existing = db.query(Invoice).filter(
        Invoice.invoice_number == invoice_data.invoice_number,
        Invoice.vendor_name == invoice_data.vendor_name
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice with this number already exists for this vendor"
        )
    
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
    
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    
    # Log audit
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="created",
        after_value={"invoice_number": invoice.invoice_number, "amount": str(invoice.total_amount)}
    )
    
    return invoice


@router.put("/{invoice_id}", response_model=InvoiceResponse)
def update_invoice(
    invoice_id: UUID,
    invoice_data: InvoiceUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Update invoice (only if draft or rejected)
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found"
        )
    
    # Check if editable
    if invoice.current_state not in [InvoiceState.DRAFT.value, InvoiceState.REJECTED.value]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot edit invoice in {invoice.current_state} state"
        )
    
    # Store before values
    before_value = {
        "total_amount": str(invoice.total_amount),
        "vendor_name": invoice.vendor_name,
        "invoice_number": invoice.invoice_number
    }
    
    # Update fields
    for field, value in invoice_data.dict(exclude_unset=True).items():
        setattr(invoice, field, value)
    
    db.commit()
    db.refresh(invoice)
    
    # Log audit
    log_audit(
        db=db,
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


@router.delete("/{invoice_id}")
def delete_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Delete invoice (only if draft)
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found"
        )
    
    if invoice.current_state != InvoiceState.DRAFT.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only delete draft invoices"
        )
    
    # Log before delete
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="deleted",
        before_value={"invoice_number": invoice.invoice_number}
    )
    
    db.delete(invoice)
    db.commit()
    
    return {"message": "Invoice deleted successfully"}


# ============================================
# INVOICE UPLOAD & OCR (Main MVP Feature)
# ============================================

@router.post("/upload", response_model=InvoiceUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_invoice_with_ocr(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Upload PDF, extract data with OCR, create invoice
    
    This is the MAIN MVP feature - combines:
    1. File upload
    2. OCR extraction
    3. Duplicate detection
    4. Invoice creation
    """
    
    # Validate file type
    if not file.content_type or "pdf" not in file.content_type.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are supported"
        )
    
    # Read file content
    content = await file.read()
    
    # Save file to storage
    stored_path, file_hash = save_file(
        tenant_id=str(current_user["tenant_id"]),
        filename=file.filename or "invoice.pdf",
        content=content
    )
    
    # Create file record
    file_record = FileModel(
        tenant_id=current_user["tenant_id"],
        original_filename=file.filename or "invoice.pdf",
        stored_filename=stored_path.split("/")[-1],
        file_path=stored_path,
        mime_type=file.content_type,
        file_size=len(content),
        file_hash=file_hash,
        object_type="invoice",
        storage_type="local",
        uploaded_by=current_user["id"]
    )
    
    db.add(file_record)
    db.commit()
    db.refresh(file_record)
    
    # Extract data using OCR
    try:
        ocr_result = extract_invoice_data_ocr(stored_path)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OCR extraction failed: {str(e)}"
        )
    
    # Sanitize OCR data for JSON storage (convert date objects to strings)
    ocr_result_sanitized = sanitize_for_json(ocr_result)
    
    vendor_name = ocr_result.get("vendor_name", "Unknown Vendor")
    invoice_number = ocr_result.get("invoice_number", f"INV-{file_record.id}")
    invoice_date_str = ocr_result.get("invoice_date")
    invoice_date = parse_date(invoice_date_str) if invoice_date_str else date.today()
    total_amount = ocr_result.get("total_amount", 0.0)
    tax_amount = ocr_result.get("tax_amount", 0.0)
    confidence = ocr_result.get("confidence", 0)
    currency = ocr_result.get("currency", "PKR")  # Extract currency from OCR, default to PKR
    
    # Check for duplicate invoice number
    existing = db.query(Invoice).filter(
        Invoice.invoice_number == invoice_number,
        Invoice.vendor_name == vendor_name
    ).first()
    
    if existing:
        return {
            "success": False,
            "error": "duplicate_invoice_number",
            "message": f"Invoice {invoice_number} already exists for {vendor_name}",
            "existing_invoice_id": str(existing.id),
            "ocr_data": ocr_result
        }
    
    # Duplicate detection using helper
    window_days = 7
    amount_min = float(total_amount) * 0.95
    amount_max = float(total_amount) * 1.05
    
    similar = db.query(Invoice).filter(
        Invoice.vendor_name == vendor_name,
        Invoice.invoice_date.between(
            invoice_date - timedelta(days=window_days),
            invoice_date + timedelta(days=window_days)
        ),
        Invoice.total_amount.between(amount_min, amount_max)
    ).first()
    
    duplicate_warning = None
    if similar:
        duplicate_warning = {
            "invoice_id": str(similar.id),
            "invoice_number": similar.invoice_number,
            "amount": money_to_float(similar.total_amount),
            "date": str(similar.invoice_date),
            "message": "Possible duplicate detected (similar amount & date)"
        }
    
    # Create invoice record
    invoice = Invoice(
        tenant_id=current_user["tenant_id"],
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        invoice_date=invoice_date,
        total_amount=total_amount,
        tax_amount=tax_amount,
        subtotal_amount=total_amount - tax_amount if tax_amount else total_amount,
        currency=currency,  # Use extracted currency
        current_state=InvoiceState.DRAFT.value,
        ocr_confidence=confidence,
        ocr_extracted_data=ocr_result_sanitized,
        pdf_file_id=file_record.id,
        created_by=current_user["id"]
    )
    
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    
    # Update file record with invoice link
    file_record.object_id = invoice.id
    db.commit()
    
    # Log audit with FULL context
    log_audit(
        db=db,
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
        # NEW: Workflow context
        workflow_step=invoice.current_state,
        workflow_type="invoice",
        # NEW: File linkage
        file_id=file_record.id,
        document_hash=file_hash,
        file_path=stored_path,
        # NEW: AI assistance (OCR + enhancement)
        ai_assisted=ocr_result.get("ai_enhanced", False),
        ai_provider=settings.AI_PROVIDER if ocr_result.get("ai_enhanced") else settings.OCR_PROVIDER,
        ai_confidence=confidence,
    )
    
    return {
        "success": True,
        "invoice_id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "vendor_name": vendor_name,
        "invoice_date": str(invoice_date),
        "total_amount": total_amount,
        "tax_amount": tax_amount,
        "currency": currency,  # Return extracted currency
        "current_state": invoice.current_state,
        "ocr_confidence": confidence,
        "ocr_data": ocr_result,
        "duplicate_warning": duplicate_warning,
        "file_id": str(file_record.id)
    }


# ============================================
# WORKFLOW TRANSITIONS
# ============================================

@router.post("/{invoice_id}/submit")
def submit_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Submit invoice for approval (draft → pending_approval)
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    
    # Debug log
    print(f"Submit invoice {invoice_id}: current_state={invoice.current_state}")
    
    # Check if already in pending/approved/paid state
    if invoice.current_state not in [InvoiceState.DRAFT.value, InvoiceState.VALIDATED.value]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot submit invoice from {invoice.current_state} state (must be draft or validated)"
        )
    
    # Transition state
    success = transition_state(
        db=db,
        obj=invoice,
        target_state=InvoiceState.PENDING_APPROVAL.value,
        user_id=current_user["id"]
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot submit invoice from {invoice.current_state} state"
        )
    
    db.commit()
    db.refresh(invoice)
    
    # Determine required approver
    required_role = evaluate_approval_role(db, current_user["tenant_id"], money_to_float(invoice.total_amount))
    
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="submitted_for_approval",
        workflow_step=invoice.current_state,
        workflow_type="invoice",
        after_value={"required_role": required_role}
    )
    
    return {
        "invoice_id": str(invoice.id),
        "current_state": invoice.current_state,
        "required_approver_role": required_role
    }


@router.post("/{invoice_id}/approve")
def approve_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Approve invoice (pending_approval → approved)
    Checks role-based permissions and approval limits
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    
    # Check permission: can user approve invoices?
    if not check_user_can_approve(current_user["role"], money_to_float(invoice.total_amount)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user['role']}' does not have permission to approve invoices of this amount"
        )
    
    # Check required role from policy
    required_role = evaluate_approval_role(db, current_user["tenant_id"], money_to_float(invoice.total_amount))
    
    if required_role and current_user["role"].lower() != required_role.lower() and current_user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{required_role.upper()} role required to approve this invoice (amount: {invoice.total_amount})"
        )
    
    # Transition state
    success = transition_state(
        db=db,
        obj=invoice,
        target_state=InvoiceState.APPROVED.value,
        user_id=current_user["id"]
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot approve invoice from {invoice.current_state} state"
        )
    
    invoice.approved_by = current_user["id"]
    invoice.approved_at = utc_now()  # Use helper
    
    db.commit()
    
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="approved",
        before_value={"state": "pending_approval"},
        after_value={"state": "approved"},
        workflow_step="approved",
        workflow_type="invoice",
        file_id=invoice.pdf_file_id,
    )
    
    return {
        "invoice_id": str(invoice.id),
        "current_state": invoice.current_state,
        "approved_by": current_user["email"],
        "approved_at": invoice.approved_at.isoformat()
    }


@router.post("/{invoice_id}/reject")
def reject_invoice(
    invoice_id: UUID,
    reason: str = Body(..., embed=True),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Reject invoice (pending_approval → rejected only)
    Checks role-based permissions
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    
    # Refresh to get latest state
    db.refresh(invoice)
    
    if not reason or not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rejection reason is required"
        )
    
    # Check permission: can user reject invoices?
    if not check_user_can_reject(current_user["role"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user['role']}' does not have permission to reject invoices"
        )
    
    # Only reject from pending_approval state
    if invoice.current_state != InvoiceState.PENDING_APPROVAL.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Can only reject invoices in pending_approval state (current: {invoice.current_state}). For draft invoices, use DELETE instead."
        )
    
    # Transition state
    success = transition_state(
        db=db,
        obj=invoice,
        target_state=InvoiceState.REJECTED.value,
        user_id=current_user["id"]
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reject invoice from {invoice.current_state} state"
        )
    
    invoice.rejection_reason = reason.strip()  #type: ignore
    
    db.commit()
    db.refresh(invoice)
    
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="rejected",
        workflow_step=invoice.current_state,
        workflow_type="invoice",
        comment=reason
    )
    
    return {
        "invoice_id": str(invoice.id),
        "current_state": invoice.current_state,
        "rejected_by": current_user["email"],
        "reason": reason
    }


@router.post("/{invoice_id}/mark-paid")
def mark_invoice_paid(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Mark invoice as paid (approved → paid)
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    
    # Transition state
    success = transition_state(
        db=db,
        obj=invoice,
        target_state=InvoiceState.PAID.value,
        user_id=current_user["id"]
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot mark invoice as paid from {invoice.current_state} state"
        )
    
    db.commit()
    
    log_audit(
        db=db,
        tenant_id=current_user["tenant_id"],
        user_id=current_user["id"],
        object_type="invoice",
        object_id=invoice.id,
        action="marked_paid"
    )
    
    return {
        "invoice_id": str(invoice.id),
        "current_state": invoice.current_state
    }


# ============================================
# STATS & ANALYTICS
# ============================================

@router.get("/stats/summary", response_model=InvoiceStats)
def get_invoice_stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get invoice summary statistics
    Used by dashboard
    """
    from sqlalchemy import func
    
    # Count by status
    status_counts = db.query(
        Invoice.current_state,
        func.count(Invoice.id).label("count"),
        func.sum(Invoice.total_amount).label("total")
    ).group_by(Invoice.current_state).all()
    
    stats = {
        "by_status": {},
        "total_invoices": 0,
        "total_amount": 0.0
    }
    
    for state, count, total in status_counts:
        stats["by_status"][state] = {
            "count": count,
            "amount": money_to_float(total)
        }
        stats["total_invoices"] += count
        stats["total_amount"] += money_to_float(total)
    
    # This month stats
    today = date.today()
    month_start = date(today.year, today.month, 1)
    
    month_stats = db.query(
        func.count(Invoice.id),
        func.sum(Invoice.total_amount)
    ).filter(Invoice.invoice_date >= month_start).first()
    
    stats["this_month"] = {
        "count": month_stats[0] or 0,
        "amount": money_to_float(month_stats[1])
    }
    
    return stats


@router.get("/pending", response_model=List[InvoiceListResponse])
def get_pending_approvals(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get all pending approval invoices
    Shortcut for dashboard
    """
    invoices = db.query(Invoice).filter(
        Invoice.current_state == "pending_approval"
    ).order_by(Invoice.created_at.desc()).all()
    
    return invoices