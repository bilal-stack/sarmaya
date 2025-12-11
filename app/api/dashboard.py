from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from datetime import date
from sqlalchemy import func
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.invoice import Invoice
from app.api.deps import get_current_user, get_db_session
from app.schemas.invoice import (
    InvoiceListResponse,
    InvoiceStats
)
from app.services.invoice_service import InvoiceService
from typing import List

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

def tenant_id_from_token(token: str) -> str:
    payload = decode_access_token(token)
    if not payload:
        raise ValueError("Invalid token")
    return payload["tenant_id"]

@router.get("/stats")
def stats(token: str = Query(...), db: Session = Depends(get_db)):
    tid = tenant_id_from_token(token)
    pending_approvals = db.query(Invoice).filter(Invoice.tenant_id == tid, Invoice.current_state == "pending_approval").count()
    today = date.today()
    month_start = date(today.year, today.month, 1)
    invoices_this_month = db.query(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total_amount), 0)).filter(
        Invoice.tenant_id == tid, Invoice.invoice_date >= month_start
    ).first()
    top_vendors = db.query(Invoice.vendor_name, func.coalesce(func.sum(Invoice.total_amount), 0).label("total")).filter(
        Invoice.tenant_id == tid
    ).group_by(Invoice.vendor_name).order_by(func.sum(Invoice.total_amount).desc()).limit(5).all()

    return {
        "pending_approvals": pending_approvals,
        "invoices_this_month": {"count": int(invoices_this_month[0] or 0), "total_amount": float(invoices_this_month[1] or 0)},
        "top_vendors": [{"vendor_name": v[0], "total_amount": float(v[1])} for v in top_vendors],
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
    service = InvoiceService(db)
    return service.get_stats()


@router.get("/pending", response_model=List[InvoiceListResponse])
def get_pending_approvals(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get all pending approval invoices
    Shortcut for dashboard
    """
    service = InvoiceService(db)
    return service.get_pending_approvals()
