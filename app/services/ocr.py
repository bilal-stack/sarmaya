import boto3
import logging
from datetime import date
from typing import Dict, Any

logger = logging.getLogger(__name__)


def extract_invoice_data(file_path: str) -> Dict[str, Any]:
    """
    Extract invoice data using AWS Textract
    
    Returns dict with:
    - vendor_name: str
    - invoice_number: str
    - invoice_date: date
    - total_amount: float
    - tax_amount: float
    - confidence: int (0-100)
    - raw_data: dict (full Textract response)
    """
    try:
        # Initialize Textract client
        textract = boto3.client('textract', region_name='us-east-1')
        
        # Read file
        with open(file_path, 'rb') as document:
            image_bytes = document.read()
        
        # Call Textract Analyze Expense API (designed for invoices)
        response = textract.analyze_expense(
            Document={'Bytes': image_bytes}
        )
        
        # Parse response
        result = {
            "vendor_name": "",
            "invoice_number": "",
            "invoice_date": None,
            "total_amount": 0.0,
            "tax_amount": 0.0,
            "confidence": 0,
            "raw_data": response
        }
        
        if 'ExpenseDocuments' not in response or len(response['ExpenseDocuments']) == 0:
            logger.warning("No expense documents found in Textract response")
            return result
        
        # Extract summary fields
        summary_fields = response['ExpenseDocuments'][0].get('SummaryFields', [])
        
        for field in summary_fields:
            field_type = field.get('Type', {}).get('Text', '').lower()
            value_text = field.get('ValueDetection', {}).get('Text', '')
            confidence = field.get('ValueDetection', {}).get('Confidence', 0)
            
            if 'vendor' in field_type or 'name' in field_type:
                result["vendor_name"] = value_text
                result["confidence"] = max(result["confidence"], int(confidence))
            
            elif 'invoice' in field_type and 'number' in field_type:
                result["invoice_number"] = value_text
                result["confidence"] = max(result["confidence"], int(confidence))
            
            elif 'date' in field_type:
                try:
                    # Try to parse date (Textract returns ISO format usually)
                    result["invoice_date"] = date.fromisoformat(value_text)
                except Exception:
                    # Fallback: extract date manually if format varies
                    result["invoice_date"] = date.today()
            
            elif 'total' in field_type:
                try:
                    result["total_amount"] = float(value_text.replace(',', '').replace('$', '').replace('PKR', '').strip())
                except Exception:
                    pass
            
            elif 'tax' in field_type:
                try:
                    result["tax_amount"] = float(value_text.replace(',', '').replace('$', '').replace('PKR', '').strip())
                except Exception:
                    pass
        
        # Fallback: if no vendor name, extract from line items
        if not result["vendor_name"]:
            line_items = response['ExpenseDocuments'][0].get('LineItemGroups', [])
            if line_items and len(line_items) > 0:
                first_item = line_items[0].get('LineItems', [])[0] if len(line_items[0].get('LineItems', [])) > 0 else {}
                for field in first_item.get('LineItemExpenseFields', []):
                    if 'vendor' in field.get('Type', {}).get('Text', '').lower():
                        result["vendor_name"] = field.get('ValueDetection', {}).get('Text', '')
                        break
        
        logger.info(f"Textract extraction complete: {result['vendor_name']}, {result['invoice_number']}, confidence={result['confidence']}")
        
        return result
    
    except Exception as e:
        logger.error(f"Textract OCR failed: {str(e)}")
        # Return placeholder on error
        return {
            "vendor_name": "",
            "invoice_number": "",
            "invoice_date": None,
            "total_amount": 0.0,
            "tax_amount": 0.0,
            "confidence": 0,
            "error": str(e)
        }


def extract_text_from_image(path: str) -> str:
    return "extracted text (placeholder)"

def extract_text_from_pdf(path: str) -> dict:
    """Legacy placeholder for compatibility"""
    return extract_invoice_data(path)
