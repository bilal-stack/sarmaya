from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Query, Body
from sqlalchemy.orm import Session
from datetime import date
from typing import Optional, List
from uuid import UUID
import logging
from app.api.deps import get_current_user, get_db_session
from app.schemas.invoice import (
    InvoiceResponse,
    InvoiceCreate,
    InvoiceUpdate,
    InvoiceUploadResponse,
    InvoiceListResponse
)
from app.services.invoice_service import InvoiceService
from app.core.enums import InvoiceState
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/invoices", tags=["Invoices"])


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
    service = InvoiceService(db)
    invoices, total = service.list_invoices(
        status_filter=status_filter,
        vendor_name=vendor_name,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset
    )
    return invoices


@router.get("/blocked-on-vendor", response_model=List[InvoiceListResponse])
def list_invoices_blocked_on_vendor(
    limit: int = Query(default=100, le=200),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Reviewer worklist: pending-approval invoices the governance gate is
    holding because their linked vendor is not yet verified/active.

    Activate the vendor (PATCH /vendors/{id}/status) to unblock these.
    Declared before /{invoice_id} so the literal path isn't parsed as a UUID.
    """
    return InvoiceService(db).get_invoices_blocked_on_vendor(limit)


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
    service = InvoiceService(db)
    invoice = service.get_invoice(invoice_id)
    
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
    service = InvoiceService(db)
    
    try:
        invoice = service.create_manual_invoice(invoice_data, current_user)
        return invoice
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


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
    service = InvoiceService(db)
    
    try:
        invoice = service.update_invoice(invoice_id, invoice_data, current_user)
        return invoice
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete("/{invoice_id}")
def delete_invoice(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Delete invoice (only if draft)
    """
    service = InvoiceService(db)
    
    try:
        service.delete_invoice(invoice_id, current_user)
        return {"message": "Invoice deleted successfully"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


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
    # Validate declared content type
    if not file.content_type or "pdf" not in file.content_type.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are supported"
        )

    # Read file content
    content = await file.read()

    # Enforce size limit (content_length header is client-supplied, so check the
    # actual bytes we read).
    max_bytes = settings.MAX_FILE_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {settings.MAX_FILE_SIZE_MB} MB limit"
        )

    # Verify magic bytes — the declared content type is spoofable.
    if not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not a valid PDF"
        )

    # Use service to handle upload logic
    service = InvoiceService(db)
    
    try:
        result = service.upload_and_extract_invoice(
            filename=file.filename or "invoice.pdf",
            content=content,
            mime_type=file.content_type,
            current_user=current_user
        )
        return result
    except RuntimeError:
        # OCR or processing errors — log internals, return a generic message.
        logger.exception("Invoice upload OCR/processing failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not process the uploaded invoice. Please try again."
        )
    except Exception:
        # Unexpected errors — never leak internals to the client.
        logger.exception("Unexpected error during invoice upload")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed due to an internal error."
        )


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
    Only users with CREATE_INVOICE permission can submit
    """
    service = InvoiceService(db)
    
    try:
        invoice, required_role = service.submit_for_approval(invoice_id, current_user)
        return {
            "invoice_id": str(invoice.id),
            "current_state": invoice.current_state,
            "required_approver_role": required_role
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )


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
    service = InvoiceService(db)
    
    try:
        invoice = service.approve_invoice(invoice_id, current_user)
        return {
            "invoice_id": str(invoice.id),
            "current_state": invoice.current_state,
            "approved_by": current_user["email"],
            "approved_at": invoice.approved_at.isoformat()
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )


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
    service = InvoiceService(db)
    
    try:
        invoice = service.reject_invoice(invoice_id, reason, current_user)
        return {
            "invoice_id": str(invoice.id),
            "current_state": invoice.current_state,
            "rejected_by": current_user["email"],
            "reason": reason
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )


@router.post("/{invoice_id}/mark-paid")
def mark_invoice_paid(
    invoice_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Mark invoice as paid (approved → paid)
    """
    service = InvoiceService(db)
    
    try:
        invoice = service.mark_as_paid(invoice_id, current_user)
        return {
            "invoice_id": str(invoice.id),
            "current_state": invoice.current_state
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except PermissionError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )
