from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.deps import get_current_user, get_db_session
from app.schemas.invoice import (
    InvoiceListResponse,
    InvoiceStats
)
from app.services.invoice_service import InvoiceService
from typing import List

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats")
def stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Headline dashboard figures (pending approvals, this month, top vendors).
    Authenticated via the Authorization header; tenant isolation by RLS."""
    return InvoiceService(db).get_dashboard_summary()


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
