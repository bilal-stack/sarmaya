import requests
import logging
import re
from datetime import datetime
from typing import Dict, Any

from app.core.config import settings
from app.services.ocr.base import OCRProvider

logger = logging.getLogger(__name__)


class OCRSpaceProvider(OCRProvider):
    """OCR.space API implementation"""
    
    def __init__(self):
        self.api_key = settings.OCR_SPACE_API_KEY
        self.api_url = settings.OCR_SPACE_API_URL
    
    def extract_invoice_data(self, file_path: str) -> Dict[str, Any]:
        """Extract invoice data using OCR.space API"""
        
        try:
            # Upload file to OCR.space
            with open(file_path, 'rb') as f:
                response = requests.post(
                    self.api_url,
                    files={'file': f},
                    data={
                        'apikey': self.api_key,
                        'language': 'eng',
                        'isOverlayRequired': False,
                        'detectOrientation': True,
                        'scale': True,
                        'OCREngine': 2,  # Engine 2 is better for invoices
                    }
                )
            
            response.raise_for_status()
            ocr_result = response.json()
            
            if ocr_result.get('IsErroredOnProcessing'):
                error_msg = ocr_result.get('ErrorMessage', ['Unknown error'])[0]
                raise Exception(f"OCR.space error: {error_msg}")
            
            # Extract text
            parsed_text = ocr_result.get('ParsedResults', [{}])[0].get('ParsedText', '')
            
            # Parse invoice fields using regex/heuristics
            result = self._parse_invoice_text(parsed_text)
            result['raw_data'] = ocr_result
            result['confidence'] = int(ocr_result.get('ParsedResults', [{}])[0].get('TextOverlay', {}).get('HasOverlay', False)) * 80
            
            logger.info(f"OCR.space extraction complete: {result['vendor_name']}, {result['invoice_number']}")
            
            return result
        
        except Exception as e:
            logger.error(f"OCR.space extraction failed: {str(e)}")
            return {
                "vendor_name": "",
                "invoice_number": "",
                "invoice_date": None,
                "total_amount": 0.0,
                "tax_amount": 0.0,
                "confidence": 0,
                "raw_data": {"error": str(e)}
            }
    
    def _parse_invoice_text(self, text: str) -> Dict[str, Any]:
        """Parse extracted text to find invoice fields"""
        
        result = {
            "vendor_name": "",
            "invoice_number": "",
            "invoice_date": None,
            "total_amount": 0.0,
            "tax_amount": 0.0,
        }
        
        lines = text.split('\n')
        
        # Vendor name (usually first few lines, capitalized)
        for line in lines[:5]:
            if line.strip() and len(line.strip()) > 3 and line.strip().isupper():
                result["vendor_name"] = line.strip()
                break
        
        # Invoice number patterns
        inv_patterns = [
            r'(?:invoice|inv)\s*(?:no|number|#)?\s*:?\s*([A-Z0-9\-]+)',
            r'#\s*([A-Z0-9\-]+)',
        ]
        for pattern in inv_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                result["invoice_number"] = match.group(1)
                break
        
        # Date patterns
        date_patterns = [
            r'(?:date|dated)\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
            r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})',
        ]
        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    date_str = match.group(1)
                    # Try common formats
                    for fmt in ['%d-%m-%Y', '%d/%m/%Y', '%m-%d-%Y', '%m/%d/%Y']:
                        try:
                            result["invoice_date"] = datetime.strptime(date_str, fmt).date()
                            break
                        except ValueError:
                            continue
                    if result["invoice_date"]:
                        break
                except Exception:
                    pass
        
        # Total amount (look for largest number near 'total')
        total_patterns = [
            r'(?:total|amount due)\s*:?\s*(?:PKR|Rs\.?|₨)?\s*([\d,]+\.?\d*)',
            r'(?:PKR|Rs\.?|₨)\s*([\d,]+\.?\d*)',
        ]
        amounts = []
        for pattern in total_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for match in matches:
                try:
                    amount = float(match.replace(',', ''))
                    amounts.append(amount)
                except ValueError:
                    pass
        
        if amounts:
            result["total_amount"] = max(amounts)  # Assume total is largest amount
        
        # Tax amount
        tax_patterns = [
            r'(?:tax|gst|vat)\s*:?\s*(?:PKR|Rs\.?|₨)?\s*([\d,]+\.?\d*)',
        ]
        for pattern in tax_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    result["tax_amount"] = float(match.group(1).replace(',', ''))
                    break
                except ValueError:
                    pass
        
        return result
