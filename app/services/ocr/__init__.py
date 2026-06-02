from app.core.config import settings
from app.services.ocr.base import OCRProvider
from app.services.ocr.ocr_space import OCRSpaceProvider
from app.services.ocr.aws_textract import AWSTextractProvider
from app.services.ocr.document_ai import DocumentAIProvider
from app.services.ai import get_ai_provider
from app.services.ocr.field_explainer import build_field_explanations
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
    
    1. Run OCR (OCR.space or Document AI)
    2. Pass raw text to AI for intelligent parsing
    3. AI fixes OCR errors (fragmented line items, etc.)
    4. Merge OCR + AI results
    """
    # Step 1: Get raw OCR
    ocr_provider = get_ocr_provider()
    ocr_result = ocr_provider.extract_invoice_data(file_path)
    
    # Step 2: Enhance with AI if enabled
    if settings.AI_ENHANCED_OCR:
        try:
            ai_provider = get_ai_provider()
        
            # Get raw text from OCR result
            raw_text = ocr_result.get("raw_data", {}).get("text", "")
            if not raw_text:
                # Fallback: stringify raw data
                import json
                raw_text = json.dumps(ocr_result["raw_data"])
            
            # ✅ AI extraction with line items cleaning
            ai_result = ai_provider.extract_invoice_fields(
                raw_text, 
                ocr_result.get("raw_data"),
                line_items=ocr_result.get("line_items", [])  # Pass OCR line items for AI to fix
            )
            
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
            
            # ✅ AI ALWAYS fixes line items (even if OCR confidence is higher)
            if ai_result.get("line_items"):
                ocr_result["line_items"] = ai_result["line_items"]
                ocr_result["ai_enhanced"] = True
                ocr_result["ai_corrections"] = ai_result.get("ai_corrections", {})
        
        except Exception as e:
            import logging
            logging.error(f"AI enhancement failed, using OCR only: {e}")
            ocr_result["ai_enhanced"] = False

    # Attach per-field confidence + "Why?" evidence for human review.
    fields = {**ocr_result, "currency": ocr_result.get("currency", "PKR")}
    ocr_result["field_explanations"] = build_field_explanations(
        _raw_text_of(ocr_result), fields
    )

    return ocr_result


def _raw_text_of(ocr_result: dict) -> str:
    """Best-effort recovery of the plain OCR text from a provider result."""
    raw = ocr_result.get("raw_data") or {}
    if isinstance(raw, dict):
        if raw.get("text"):
            return raw["text"]
        parsed = raw.get("ParsedResults") or []
        if parsed and isinstance(parsed, list):
            return parsed[0].get("ParsedText", "") or ""
    return ""


# Convenience exports
__all__ = ['get_ocr_provider', 'extract_invoice_data_ocr']
