from abc import ABC, abstractmethod
from typing import Dict, Any
from datetime import date


class OCRProvider(ABC):
    """Abstract base class for OCR providers"""
    
    @abstractmethod
    def extract_invoice_data(self, file_path: str) -> Dict[str, Any]:
        """
        Extract invoice data from file
        
        Must return dict with these keys:
        - vendor_name: str
        - invoice_number: str
        - invoice_date: date | None
        - total_amount: float
        - tax_amount: float
        - confidence: int (0-100)
        - raw_data: dict (provider-specific response)
        """
        pass
