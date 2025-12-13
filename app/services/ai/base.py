from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional


class AIProvider(ABC):
    """Abstract base class for AI providers"""
    
    @abstractmethod
    def chat(self, messages: List[Dict[str, str]], context: Dict[str, Any] = None) -> str:
        """
        Chat completion
        
        Args:
            messages: List of {"role": "user/assistant/system", "content": "..."}
            context: Optional context (tenant_id, user_id, invoice data, etc.)
        
        Returns:
            AI response text
        """
        pass
    
    @abstractmethod
    def extract_invoice_fields(
        self, 
        ocr_text: str, 
        raw_ocr_data: Optional[Dict] = None,
        line_items: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Extract structured invoice fields from OCR text using AI
        
        Args:
            ocr_text: Raw text from OCR
            raw_ocr_data: Optional raw OCR response
            line_items: Optional OCR-extracted line items (may have errors)
        
        Returns:
            {
                "vendor_name": str,
                "invoice_number": str,
                "invoice_date": str (ISO),
                "total_amount": float,
                "tax_amount": float,
                "line_items": [...],  # AI-cleaned line items
                "confidence": int,
                "ai_corrections": {
                    "line_items_merged": ["item1", "item2"],
                    "descriptions_fixed": {"old": "new"}
                }
            }
        """
        pass
    
    @abstractmethod
    def detect_duplicate_invoices(
        self, 
        current_invoice: Dict[str, Any], 
        candidate_invoices: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        AI-powered duplicate detection with similarity scoring
        
        Args:
            current_invoice: New invoice data
            candidate_invoices: List of potentially similar invoices
        
        Returns:
            {
                "is_duplicate": bool,
                "confidence": float,
                "matched_invoice_id": str | None,
                "similarity_score": float,
                "reasoning": str
            }
        """
        pass
    
    @abstractmethod
    def query_system(self, query: str, context: Dict[str, Any]) -> str:
        """
        Natural language query for invoices/vendors/stats
        
        Args:
            query: User question ("Show me pending invoices over 100k")
            context: User context (tenant_id, role, permissions)
        
        Returns:
            Natural language response with data
        """
        pass
