from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from datetime import date
from sqlalchemy import func
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.invoice import Invoice
from app.models.vendor import Vendor

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
