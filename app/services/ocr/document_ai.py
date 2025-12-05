import logging
import re
import os
from datetime import date, datetime
from typing import Dict, Any
from google.cloud import documentai_v1 as documentai #type: ignore
from google.api_core.client_options import ClientOptions

from app.core.config import settings
from app.services.ocr.base import OCRProvider

logger = logging.getLogger(__name__)


class DocumentAIProvider(OCRProvider):
    """Google Document AI implementation"""
    
    def __init__(self):
        # Set credentials environment variable
        if settings.GOOGLE_APPLICATION_CREDENTIALS:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
        
        self.project_id = settings.GOOGLE_CLOUD_PROJECT_ID
        self.location = settings.GOOGLE_CLOUD_LOCATION or "us"
        self.processor_id = settings.GOOGLE_DOCUMENT_AI_PROCESSOR_ID
        
        # Initialize Document AI client
        opts = ClientOptions(api_endpoint=f"{self.location}-documentai.googleapis.com")
        self.client = documentai.DocumentProcessorServiceClient(client_options=opts)
    
    def extract_invoice_data(self, file_path: str) -> Dict[str, Any]:
        """Extract invoice data using Google Document AI"""
        
        try:
            # Read file
            with open(file_path, "rb") as file:
                file_content = file.read()
            
            # Configure the process request
            name = self.client.processor_path(
                self.project_id, self.location, self.processor_id
            )
            
            # Detect mime type
            mime_type = "application/pdf"
            if file_path.lower().endswith(('.png', '.jpg', '.jpeg')):
                mime_type = "image/jpeg"
            
            # Create the document object
            raw_document = documentai.RawDocument(
                content=file_content,
                mime_type=mime_type
            )
            
            # Make the API request
            request = documentai.ProcessRequest(
                name=name,
                raw_document=raw_document
            )
            
            result = self.client.process_document(request=request)
            document = result.document
            
            # Parse extracted entities
            parsed_result = self._parse_document_ai_response(document)
            print('Document AI parsed result:', parsed_result)
            return parsed_result
        
        except Exception as e:
            logger.error(f"Document AI extraction failed: {str(e)}")
            return {
                "vendor_name": "",
                "invoice_number": "",
                "invoice_date": None,
                "total_amount": 0.0,
                "tax_amount": 0.0,
                "confidence": 0,
                "raw_data": {"error": str(e)}
            }
    
    def _parse_document_ai_response(self, document) -> Dict[str, Any]:
        """Parse Document AI response and extract invoice fields"""
        
        result = {
            "vendor_name": "",
            "invoice_number": "",
            "invoice_date": None,
            "total_amount": 0.0,
            "tax_amount": 0.0,
            "currency": "PKR",  # Default currency
            "confidence": 0,
            "raw_data": {}
        }
        
        # Extract entities (Document AI returns structured entities for invoices)
        entities_dict = {}
        total_confidence = 0
        confidence_count = 0
        
        # Track values needed for tax calculation
        net_amount = 0.0
        tax_value = 0.0
        tax_is_percentage = False
        
        for entity in document.entities:
            entity_type = entity.type_
            entity_value = entity.mention_text
            confidence = entity.confidence
            
            entities_dict[entity_type] = {
                "value": entity_value,
                "confidence": confidence
            }
            
            total_confidence += confidence
            confidence_count += 1
            
            # Map to result fields
            if entity_type == "supplier_name":
                result["vendor_name"] = entity_value
            elif entity_type == "invoice_id":
                result["invoice_number"] = entity_value
            elif entity_type == "invoice_date":
                result["invoice_date"] = self._parse_date(entity_value)
            elif entity_type == "total_amount":
                result["total_amount"] = self._parse_amount(entity_value)
            elif entity_type == "net_amount":
                net_amount = self._parse_amount(entity_value)
                # Fallback if total_amount not found
                if not result["total_amount"]:
                    result["total_amount"] = net_amount
            elif entity_type == "total_tax_amount":
                tax_value = self._parse_amount(entity_value)
            elif entity_type == "currency":
                result["currency"] = entity_value.upper()  # Extract currency (USD, PKR, etc.)
        
        # Determine if tax is percentage or amount and calculate accordingly
        if tax_value > 0:
            tax_is_percentage = self._is_percentage(tax_value, entity_value if entity_type == "total_tax_amount" else "")
            
            if tax_is_percentage:
                # Calculate actual tax amount from percentage
                if net_amount > 0:
                    result["tax_amount"] = round((net_amount * tax_value) / 100, 2)
                else:
                    # Try to reverse calculate from total_amount if net_amount not available
                    # total = net + tax, where tax = net * (tax_rate/100)
                    # total = net * (1 + tax_rate/100)
                    # net = total / (1 + tax_rate/100)
                    if result["total_amount"] > 0:
                        net_amount = result["total_amount"] / (1 + tax_value / 100)
                        result["tax_amount"] = round(result["total_amount"] - net_amount, 2)
            else:
                # Tax is already an amount
                result["tax_amount"] = tax_value
        
        # Calculate average confidence
        if confidence_count > 0:
            result["confidence"] = int((total_confidence / confidence_count) * 100)
        
        # Fallback: extract from raw text if entities missing
        if not result["vendor_name"] or not result["invoice_number"]:
            fallback = self._fallback_text_extraction(document.text)
            result["vendor_name"] = result["vendor_name"] or fallback.get("vendor_name", "")
            result["invoice_number"] = result["invoice_number"] or fallback.get("invoice_number", "")
        
        result["raw_data"] = {
            "entities": entities_dict,
            "text": document.text,
            "pages": len(document.pages),
            "tax_metadata": {
                "is_percentage": tax_is_percentage,
                "raw_value": tax_value,
                "net_amount_used": net_amount
            }
        }
        
        return result
    
    def _is_percentage(self, value: float, original_text: str) -> bool:
        """Determine if a tax value is a percentage or absolute amount"""
        # Check if original text contains % symbol
        if '%' in original_text:
            return True
        
        # Heuristic: if value is <= 100 and looks like a percentage
        # Most tax rates are between 0-30%, rarely above 50%
        # Absolute tax amounts are usually much higher
        if value <= 100:
            # Additional check: value has few decimal places (typical for percentage)
            # and is a "round" number like 4, 5, 10, 15, etc.
            if value == int(value) or round(value, 2) == round(value, 1):
                return True
        
        return False
    
    def _parse_date(self, date_str: str) -> date | None:
        """Parse date string to date object"""
        if not date_str:
            return None
        
        # Try common formats
        formats = [
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%m/%d/%Y",
            "%d-%m-%Y",
            "%m-%d-%Y",
            "%B %d, %Y",
            "%d %B %Y"
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        
        return None
    
    def _parse_amount(self, amount_str: str) -> float:
        """Parse amount string to float"""
        if not amount_str:
            return 0.0
        
        try:
            # Remove currency symbols and commas
            cleaned = re.sub(r'[^\d.]', '', amount_str)
            return float(cleaned)
        except Exception:
            return 0.0
    
    def _fallback_text_extraction(self, text: str) -> Dict[str, str]:
        """Fallback regex extraction if entities missing"""
        
        result = {
            "vendor_name": "",
            "invoice_number": ""
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
        
        return result
