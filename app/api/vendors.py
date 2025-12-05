from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.vendor import Vendor
from app.models.tenant import Tenant

router = APIRouter(prefix="/vendors", tags=["vendors"])

def get_tenant(db: Session, token: str) -> Tenant:
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    tenant_id = payload.get("tenant_id")
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    # RLS context will be set by middleware/deps in future; for now we use WHERE clauses
    return tenant

@router.post("/", status_code=status.HTTP_201_CREATED)
def create_vendor(
    legal_name: str,
    email: Optional[str] = None,
    bank_account_name: Optional[str] = None,
    bank_account_number: Optional[str] = None,
    status_value: str = "active",
    token: str = Query(..., description="Bearer token"),
    db: Session = Depends(get_db),
):
    tenant = get_tenant(db, token)
    existing = db.query(Vendor).filter(Vendor.tenant_id == tenant.id, Vendor.legal_name == legal_name).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vendor already exists")
    v = Vendor(
        tenant_id=tenant.id,
        legal_name=legal_name,
        email=email,
        bank_account_name=bank_account_name,
        bank_account_number=bank_account_number,
        status=status_value,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return {"id": str(v.id), "legal_name": v.legal_name, "status": v.status}

@router.get("/")
def list_vendors(
    status_filter: Optional[str] = None,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    tenant = get_tenant(db, token)
    q = db.query(Vendor).filter(Vendor.tenant_id == tenant.id)
    if status_filter:
        q = q.filter(Vendor.status == status_filter)
    rows = q.order_by(Vendor.legal_name.asc()).all()
    return [{"id": str(r.id), "legal_name": r.legal_name, "email": r.email, "status": r.status} for r in rows]

@router.get("/{vendor_id}")
def get_vendor(vendor_id: str, token: str = Query(...), db: Session = Depends(get_db)):
    tenant = get_tenant(db, token)
    v = db.query(Vendor).filter(Vendor.tenant_id == tenant.id, Vendor.id == vendor_id).first()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    return {"id": str(v.id), "legal_name": v.legal_name, "email": v.email, "status": v.status}

@router.patch("/{vendor_id}/status")
def update_vendor_status(vendor_id: str, status_value: str, token: str = Query(...), db: Session = Depends(get_db)):
    tenant = get_tenant(db, token)
    v = db.query(Vendor).filter(Vendor.tenant_id == tenant.id, Vendor.id == vendor_id).first()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    v.status = status_value
    db.commit()
    return {"id": str(v.id), "status": v.status}

@router.delete("/{vendor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vendor(vendor_id: str, token: str = Query(...), db: Session = Depends(get_db)):
    tenant = get_tenant(db, token)
    v = db.query(Vendor).filter(Vendor.tenant_id == tenant.id, Vendor.id == vendor_id).first()
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    db.delete(v)
    db.commit()
    return {}
