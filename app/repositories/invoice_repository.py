from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List, Tuple
from datetime import date, timedelta
from uuid import UUID
from app.models.invoice import Invoice
from app.core.enums import InvoiceState
from app.utils.money import money_to_float


class InvoiceRepository:
    """Repository for Invoice database operations"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def get_by_id(self, invoice_id: UUID) -> Optional[Invoice]:
        """Get invoice by ID"""
        return self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
    
    def get_by_invoice_number(
        self, 
        invoice_number: str, 
        vendor_name: str
    ) -> Optional[Invoice]:
        """Get invoice by invoice number and vendor"""
        return self.db.query(Invoice).filter(
            Invoice.invoice_number == invoice_number,
            Invoice.vendor_name == vendor_name
        ).first()
    
    def list_invoices(
        self,
        status_filter: Optional[InvoiceState] = None,
        vendor_name: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Invoice], int]:
        """
        List invoices with filters and pagination
        Returns (invoices, total_count)
        """
        query = self.db.query(Invoice)
        
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
        
        return invoices, total
    
    def find_duplicates_by_number(
        self, 
        invoice_number: str, 
        vendor_name: str
    ) -> Optional[Invoice]:
        """Find exact duplicate by invoice number and vendor"""
        return self.db.query(Invoice).filter(
            Invoice.invoice_number == invoice_number,
            Invoice.vendor_name == vendor_name
        ).first()
    
    def find_similar_invoices(
        self,
        vendor_name: str,
        invoice_date: date,
        total_amount: float,
        window_days: int = 7,
        amount_tolerance: float = 0.05
    ) -> Optional[Invoice]:
        """
        Find similar invoice (first match for duplicate warning)
        
        Args:
            vendor_name: Vendor name to match
            invoice_date: Invoice date
            total_amount: Invoice total amount
            window_days: Date range ± days
            amount_tolerance: Amount tolerance (0.05 = ±5%)
        
        Returns:
            First similar invoice or None
        """
        amount_min = total_amount * (1 - amount_tolerance)
        amount_max = total_amount * (1 + amount_tolerance)
        
        return self.db.query(Invoice).filter(
            Invoice.vendor_name == vendor_name,
            Invoice.invoice_date.between(
                invoice_date - timedelta(days=window_days),
                invoice_date + timedelta(days=window_days)
            ),
            Invoice.total_amount.between(amount_min, amount_max)
        ).first()
    
    def get_pending_approvals(self, limit: int = 100) -> List[Invoice]:
        """Get all pending approval invoices"""
        return self.db.query(Invoice).filter(
            Invoice.current_state == InvoiceState.PENDING_APPROVAL.value
        ).order_by(Invoice.created_at.desc()).limit(limit).all()
    
    def get_stats_by_status(self) -> List[Tuple[str, int, float]]:
        """
        Get invoice counts and totals grouped by status
        Returns [(state, count, total_amount), ...]
        """
        results = self.db.query(
            Invoice.current_state,
            func.count(Invoice.id).label("count"),
            func.sum(Invoice.total_amount).label("total")
        ).group_by(Invoice.current_state).all()
        
        # Convert Decimal to float
        return [
            (state, count, money_to_float(total))
            for state, count, total in results
        ]
    
    def get_monthly_stats(self, month_start: date) -> Tuple[int, float]:
        """
        Get invoice count and total for current month
        Returns (count, total_amount)
        """
        result = self.db.query(
            func.count(Invoice.id),
            func.sum(Invoice.total_amount)
        ).filter(Invoice.invoice_date >= month_start).first()
        
        count = result[0] or 0
        total = money_to_float(result[1])
        
        return count, total
    
    def create(self, invoice: Invoice) -> Invoice:
        """Create new invoice"""
        self.db.add(invoice)
        self.db.flush()
        return invoice
    
    def update(self, invoice: Invoice) -> Invoice:
        """Update invoice"""
        self.db.add(invoice)
        self.db.flush()
        return invoice
    
    def delete(self, invoice: Invoice) -> None:
        """Delete invoice"""
        self.db.delete(invoice)
        self.db.flush()
    
    def commit(self) -> None:
        """Commit transaction"""
        self.db.commit()
    
    def refresh(self, invoice: Invoice) -> Invoice:
        """Refresh invoice from DB"""
        self.db.refresh(invoice)
        return invoice
