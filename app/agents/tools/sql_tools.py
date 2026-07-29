"""SQL tools for AI agents"""

from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from datetime import date, timedelta
import logging
import re
from app.models.invoice import Invoice

logger = logging.getLogger(__name__)


class SQLTools:
    """SQL execution tools for AI agents"""
    
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
    
    def query_invoices(
        self,
        status: Optional[str] = None,
        vendor_name: Optional[str] = None,
        min_amount: Optional[float] = None,
        max_amount: Optional[float] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Query invoices with filters
        
        Returns:
            {
                "count": int,
                "invoices": [...]
            }
        """
        query = self.db.query(Invoice).filter(Invoice.tenant_id == self.tenant_id)
        
        if status:
            query = query.filter(Invoice.current_state == status)
        
        if vendor_name:
            query = query.filter(Invoice.vendor_name.ilike(f"%{vendor_name}%"))
        
        if min_amount is not None:
            query = query.filter(Invoice.total_amount >= min_amount)
        
        if max_amount is not None:
            query = query.filter(Invoice.total_amount <= max_amount)
        
        if start_date:
            query = query.filter(Invoice.invoice_date >= date.fromisoformat(start_date))
        
        if end_date:
            query = query.filter(Invoice.invoice_date <= date.fromisoformat(end_date))
        
        invoices = query.order_by(desc(Invoice.created_at)).limit(limit).all()
        
        return {
            "count": len(invoices),
            "invoices": [
                {
                    "id": str(inv.id),
                    "invoice_number": inv.invoice_number,
                    "vendor_name": inv.vendor_name,
                    "total_amount": float(inv.total_amount or 0),
                    "currency": inv.currency,
                    "status": inv.current_state,
                    "invoice_date": str(inv.invoice_date),
                    "line_items": inv.line_items or []
                }
                for inv in invoices
            ]
        }
    
    def check_exact_duplicate(
        self,
        vendor_name: str,
        invoice_number: str
    ) -> Optional[Dict[str, Any]]:
        """
        Check for exact invoice number duplicate
        
        ✅ ENHANCED: Now uses fuzzy vendor matching
        """
        vendor_core = self._extract_vendor_core(vendor_name)
        
        existing = self.db.query(Invoice).filter(
            Invoice.tenant_id == self.tenant_id,
            Invoice.vendor_name.ilike(f"%{vendor_core}%"),  # ✅ Fuzzy vendor
            Invoice.invoice_number == invoice_number  # Exact invoice number
        ).first()
        
        if existing:
            return {
                "id": str(existing.id),
                "invoice_number": existing.invoice_number,
                "invoice_date": str(existing.invoice_date),
                "total_amount": float(existing.total_amount or 0),
                "status": existing.current_state,
                "vendor_name": existing.vendor_name  # ✅ Return full name
            }
        return None
    
    def find_similar_invoices(
        self,
        vendor_name: str,
        invoice_date: str,
        total_amount: float,
        window_days: int = 30,
        amount_tolerance: float = 0.1
    ) -> List[Dict[str, Any]]:
        """
        Find invoices similar to given criteria
        
        ✅ ENHANCED: More lenient matching for better duplicate detection
        """
        inv_date = date.fromisoformat(invoice_date)
        amount_min = total_amount * (1 - amount_tolerance)
        amount_max = total_amount * (1 + amount_tolerance)
        
        # ✅ FUZZY VENDOR MATCHING: Extract core name
        vendor_core = self._extract_vendor_core(vendor_name)
        
        logger.debug("Searching duplicates: vendor_core=%r date=%s amount=%s (+/-%.0f%%)",
                     vendor_core, inv_date, total_amount, amount_tolerance * 100)
        
        # Query with flexible vendor matching
        similar = self.db.query(Invoice).filter(
            Invoice.tenant_id == self.tenant_id,
            # ✅ Use ILIKE for partial vendor match
            Invoice.vendor_name.ilike(f"%{vendor_core}%"),
            Invoice.invoice_date.between(
                inv_date - timedelta(days=window_days),
                inv_date + timedelta(days=window_days)
            ),
            Invoice.total_amount.between(amount_min, amount_max)
        ).limit(10).all()
        
        logger.debug("Found %d similar invoices", len(similar))
        
        results = [
            {
                "id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "invoice_date": str(inv.invoice_date),
                "total_amount": float(inv.total_amount or 0),
                "line_items": inv.line_items or [],
                "status": inv.current_state,
                "vendor_name": inv.vendor_name  # ✅ Include full vendor name for AI
            }
            for inv in similar
        ]
        
        for r in results:
            logger.debug("  candidate: %s | %s | %s | %s",
                         r["vendor_name"], r["invoice_number"], r["total_amount"], r["invoice_date"])
        
        return results
    
    def _extract_vendor_core(self, vendor_name: str) -> str:
        """
        Extract core vendor name for fuzzy matching
        
        Examples:
        - "Vorson limited" -> "vorson"
        - "ABC Corp." -> "abc"
        - "XYZ Pvt. Ltd." -> "xyz"
        """
        # Remove common suffixes
        suffixes = [
            'limited', 'ltd', 'pvt', 'corp', 'corporation', 
            'inc', 'llc', 'co', 'company', 'enterprises'
        ]
        
        name = vendor_name.lower().strip()
        
        # Remove punctuation
        name = re.sub(r'[^\w\s]', '', name)
        
        # Remove suffixes
        words = name.split()
        filtered_words = [w for w in words if w not in suffixes]
        
        # Return first word (usually the main company name)
        core = filtered_words[0] if filtered_words else name
        
        logger.debug("Vendor %r -> core %r", vendor_name, core)
        
        return core

    
    def get_vendor_statistics(self, vendor_name: str) -> Dict[str, Any]:
        """Get vendor invoice statistics"""
        result = self.db.query(
            func.count(Invoice.id).label("count"),
            func.avg(Invoice.total_amount).label("avg_amount"),
            func.sum(Invoice.total_amount).label("total_amount")
        ).filter(
            Invoice.tenant_id == self.tenant_id,
            Invoice.vendor_name == vendor_name
        ).first()
        
        return {
            "invoice_count": result.count or 0,
            "average_amount": float(result.avg_amount or 0),
            "total_amount": float(result.total_amount or 0)
        }
    
    def get_top_vendors(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get top vendors by total invoice amount"""
        results = self.db.query(
            Invoice.vendor_name,
            func.count(Invoice.id).label("invoice_count"),
            func.sum(Invoice.total_amount).label("total_amount")
        ).filter(
            Invoice.tenant_id == self.tenant_id
        ).group_by(
            Invoice.vendor_name
        ).order_by(
            desc("total_amount")
        ).limit(limit).all()
        
        return [
            {
                "vendor_name": r.vendor_name,
                "invoice_count": r.invoice_count,
                "total_amount": float(r.total_amount or 0)
            }
            for r in results
        ]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """OpenAI function calling format"""
    return [
        {
            "type": "function",
            "function": {
                "name": "query_invoices",
                "description": "Query invoices with filters. Use for questions like 'show pending invoices' or 'invoices over 100k'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["draft", "pending_approval", "approved", "rejected", "paid"],
                            "description": "Invoice status filter"
                        },
                        "vendor_name": {
                            "type": "string",
                            "description": "Filter by vendor name (partial match)"
                        },
                        "min_amount": {
                            "type": "number",
                            "description": "Minimum invoice amount"
                        },
                        "max_amount": {
                            "type": "number",
                            "description": "Maximum invoice amount"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return",
                            "default": 10
                        }
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "find_similar_invoices",
                "description": "Find invoices similar to given criteria (for duplicate detection)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vendor_name": {"type": "string"},
                        "invoice_date": {"type": "string", "format": "date"},
                        "total_amount": {"type": "number"},
                        "window_days": {"type": "integer", "default": 30},
                        "amount_tolerance": {"type": "number", "default": 0.1}
                    },
                    "required": ["vendor_name", "invoice_date", "total_amount"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_vendor_statistics",
                "description": "Get statistics for a specific vendor",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "vendor_name": {"type": "string"}
                    },
                    "required": ["vendor_name"]
                }
            }
        }
    ]
