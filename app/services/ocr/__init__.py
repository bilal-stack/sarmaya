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


def extract_invoice_data_ocr(file_path: str, db=None, tenant_id=None, user_id=None) -> dict:
    """
    Enhanced OCR with AI-powered field extraction

    1. Run OCR (OCR.space or Document AI)
    2. Pass raw text to AI for intelligent parsing
    3. Schema-validate the AI output (Build Book: structured JSON only) —
       structurally malformed output is rejected and the raw OCR result stands
    4. Merge OCR + validated AI results

    When db/tenant_id are provided, the AI enhancement is recorded in the
    AI action log (status completed / failed_schema / error) with provenance.
    """
    import time
    from app.schemas.ai import InvoiceExtractionResult
    from app.services.ai_action_log import (
        log_ai_action, STATUS_COMPLETED, STATUS_FAILED_SCHEMA, STATUS_ERROR,
    )

    # Step 1: Get raw OCR
    ocr_provider = get_ocr_provider()
    ocr_result = ocr_provider.extract_invoice_data(file_path)

    # Step 2: Enhance with AI if enabled
    if settings.AI_ENHANCED_OCR:
        ai_provider = None
        ai_status = None
        started = time.monotonic()
        try:
            ai_provider = get_ai_provider()

            # Get raw text from OCR result
            raw_text = ocr_result.get("raw_data", {}).get("text", "")
            if not raw_text:
                # Fallback: stringify raw data
                import json
                raw_text = json.dumps(ocr_result["raw_data"])

            ai_raw = ai_provider.extract_invoice_fields(
                raw_text,
                ocr_result.get("raw_data"),
                line_items=ocr_result.get("line_items", [])  # Pass OCR line items for AI to fix
            )

            if isinstance(ai_raw, dict) and ai_raw.get("error"):
                # Provider-level failure (already logged there); OCR result stands.
                ocr_result["ai_enhanced"] = False
                ai_status = STATUS_ERROR
            else:
                # Gate: schema-validate before trusting anything the AI returned.
                ai_result = InvoiceExtractionResult.try_validate(ai_raw)
                if ai_result is None:
                    ocr_result["ai_enhanced"] = False
                    ai_status = STATUS_FAILED_SCHEMA
                else:
                    ai_status = STATUS_COMPLETED
                    # Merge results (AI takes precedence if confidence > OCR)
                    if ai_result.confidence > ocr_result.get("confidence", 0):
                        ocr_result.update({
                            "vendor_name": ai_result.vendor_name or ocr_result.get("vendor_name"),
                            "invoice_number": ai_result.invoice_number or ocr_result.get("invoice_number"),
                            "invoice_date": ai_result.invoice_date or ocr_result.get("invoice_date"),
                            "total_amount": ai_result.total_amount or ocr_result.get("total_amount"),
                            "tax_amount": ai_result.tax_amount or ocr_result.get("tax_amount"),
                            "confidence": max(ai_result.confidence, ocr_result.get("confidence", 0)),
                            "ai_enhanced": True,
                            "ai_corrections": ai_result.ai_corrections,
                        })

                    # AI fixes line items whenever it returned a valid set.
                    if ai_result.line_items:
                        ocr_result["line_items"] = ai_result.line_items
                        ocr_result["ai_enhanced"] = True
                        ocr_result["ai_corrections"] = ai_result.ai_corrections

        except Exception as e:
            import logging
            logging.error(f"AI enhancement failed, using OCR only: {e}")
            ocr_result["ai_enhanced"] = False
            ai_status = STATUS_ERROR

        # Record the AI action when we have tenant context (Build Book:
        # ai.completed / ai.failed_schema with provenance).
        if db is not None and tenant_id is not None and ai_status is not None:
            log_ai_action(
                db, tenant_id, user_id,
                action="invoice_extraction",
                status=ai_status,
                ai_provider=settings.AI_PROVIDER,
                ai_model=getattr(ai_provider, "model", None),
                prompt_version="invoice-extract-v1",
                confidence=float(ocr_result.get("confidence") or 0),
                latency_ms=int((time.monotonic() - started) * 1000),
                input_summary=f"file={file_path}",
                output_summary=(
                    f"vendor={ocr_result.get('vendor_name')} "
                    f"number={ocr_result.get('invoice_number')} "
                    f"amount={ocr_result.get('total_amount')}"
                ),
            )

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
