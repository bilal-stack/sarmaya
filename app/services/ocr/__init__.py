from app.core.config import settings
from app.services.ocr.base import OCRProvider
from app.services.ocr.ocr_space import OCRSpaceProvider
from app.services.ocr.aws_textract import AWSTextractProvider
from app.services.ocr.document_ai import DocumentAIProvider
from app.services.ai import get_ai_provider
from app.core.enums import OCRProviderType


def get_ocr_provider() -> OCRProvider:
    """Get OCR provider based on configuration"""
    provider = settings.OCR_PROVIDER.lower()
    
    if provider == OCRProviderType.OCR_SPACE:
        return OCRSpaceProvider()
    elif provider == OCRProviderType.AWS_TEXTRACT:
        return AWSTextractProvider()
    elif provider == OCRProviderType.DOCUMENT_AI:
        return DocumentAIProvider()
    else:
        raise ValueError(f"Unknown OCR provider: {provider}")


def extract_invoice_data_ocr(file_path: str) -> dict:
    """
    Enhanced OCR with AI-powered field extraction
    
    1. Run OCR (OCR.space or Textract)
    2. Pass raw text to AI for intelligent parsing
    3. Merge OCR + AI results
    """
    # Step 1: Get raw OCR
    ocr_provider = get_ocr_provider()
    ocr_result = ocr_provider.extract_invoice_data(file_path)
    
    # Step 2: Enhance with AI if enabled
    if settings.AI_ENHANCED_OCR:
        try:
            ai_provider = get_ai_provider()
        
            # Get raw text from OCR result
            raw_text = ocr_result.get("raw_data", {}).get("ParsedText", "")
            if not raw_text and "raw_data" in ocr_result:
                # Fallback: stringify raw data
                import json
                raw_text = json.dumps(ocr_result["raw_data"])
            
            # AI extraction
            ai_result = ai_provider.extract_invoice_fields(raw_text, ocr_result.get("raw_data"))
            
            # Merge results (AI takes precedence if confidence > OCR)
            if ai_result.get("confidence", 0) > ocr_result.get("confidence", 0):
                ocr_result.update({
                    "vendor_name": ai_result.get("vendor_name") or ocr_result.get("vendor_name"),
                    "invoice_number": ai_result.get("invoice_number") or ocr_result.get("invoice_number"),
                    "invoice_date": ai_result.get("invoice_date") or ocr_result.get("invoice_date"),
                    "total_amount": ai_result.get("total_amount") or ocr_result.get("total_amount"),
                    "tax_amount": ai_result.get("tax_amount") or ocr_result.get("tax_amount"),
                    "confidence": max(ai_result.get("confidence", 0), ocr_result.get("confidence", 0)),
                    "ai_enhanced": True,
                    "ai_corrections": ai_result.get("ai_corrections", {})
                })
        
        except Exception as e:
            # Fallback to OCR-only if AI fails
            import logging
            logging.error(f"AI enhancement failed, using OCR only: {e}")
            ocr_result["ai_enhanced"] = False
    
    return ocr_result


# Convenience exports
__all__ = ['get_ocr_provider', 'extract_invoice_data_ocr']
