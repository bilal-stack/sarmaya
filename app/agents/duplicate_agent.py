"""
Duplicate Detection Agent with Multi-Strategy Approach

Strategies:
1. Exact match (invoice_number + vendor)
2. Fuzzy match (similar amount + date + vendor)
3. Line item comparison (deep semantic similarity)
"""

import logging
import time
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session

from app.services.ai import get_ai_provider
from app.agents.tools.sql_tools import SQLTools
from app.schemas.ai import DuplicateDetectionResult
from app.services.ai_action_log import log_ai_action, STATUS_COMPLETED, STATUS_FAILED_SCHEMA
from app.core.config import settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "dup-detect-v1"


class DuplicateDetectionAgent:
    """Agent for intelligent duplicate detection"""

    def __init__(self, db: Session, tenant_id: str, user_id: Optional[str] = None):
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.tools = SQLTools(db, tenant_id)
        self.ai = get_ai_provider()

    def _finalize(self, raw: Dict[str, Any], ai_used: bool = False) -> Dict[str, Any]:
        """Schema-validate every result the agent returns. AI-produced results
        also carry model/provider provenance; malformed AI output is replaced
        with a safe non-duplicate result (see DuplicateDetectionResult)."""
        provenance = None
        if ai_used:
            provenance = {
                "ai_provider": settings.AI_PROVIDER,
                "ai_model": getattr(self.ai, "model", None),
            }
        return DuplicateDetectionResult.validated(raw, provenance).model_dump()
    
    def detect(
        self,
        vendor_name: str,
        invoice_number: str,
        invoice_date: str,
        total_amount: float,
        line_items: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Multi-strategy duplicate detection
        
        Returns:
            {
                "is_duplicate": bool,
                "confidence": float,
                "strategy": "exact" | "fuzzy" | "line_item",
                "matched_invoice_id": str | None,
                "reasoning": str
            }
        """
        logger.debug("Duplicate check: %s | %s | %s | %s", vendor_name, invoice_number, invoice_date, total_amount)
        
        # Strategy 1: Exact invoice number match (with fuzzy vendor)
        exact_match = self.tools.check_exact_duplicate(vendor_name, invoice_number)
        if exact_match:
            logger.info("Exact duplicate match found: %s", exact_match["id"])
            return self._finalize({
                "is_duplicate": True,
                "confidence": 1.0,
                "strategy": "exact",
                "matched_invoice_id": exact_match["id"],
                "reasoning": f"Exact match: Invoice {invoice_number} already exists for {exact_match['vendor_name']}"
            })
        
        logger.debug("No exact match; trying fuzzy search")
        
        # Strategy 2: Fuzzy match with RELAXED constraints
        similar = self.tools.find_similar_invoices(
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            window_days=60,  # ✅ Wider window
            amount_tolerance=0.15  # ✅ More tolerance
        )
        
        if not similar:
            logger.debug("No similar invoices found")
            return self._finalize({
                "is_duplicate": False,
                "confidence": 1.0,
                "strategy": "none",
                "matched_invoice_id": None,
                "reasoning": "No similar invoices found"
            })
        
        logger.debug("Sending %d candidates to AI for analysis", len(similar))

        # Strategy 3: AI analysis of ALL candidates
        started = time.monotonic()
        ai_result = self._ai_comprehensive_comparison(
            current_invoice={
                "vendor_name": vendor_name,
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "total_amount": total_amount,
                "line_items": line_items or []
            },
            candidates=similar
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        logger.debug("AI duplicate result: %s", ai_result)

        # AI-produced result: schema-validate and attach model/provider provenance.
        finalized = self._finalize(ai_result, ai_used=True)
        # Log the AI action (Build Book: every AI action logged with provenance).
        log_ai_action(
            self.db, self.tenant_id, self.user_id,
            action="duplicate_detection",
            status=STATUS_FAILED_SCHEMA if finalized.get("strategy") == "schema_invalid" else STATUS_COMPLETED,
            ai_provider=settings.AI_PROVIDER,
            ai_model=getattr(self.ai, "model", None),
            prompt_version=PROMPT_VERSION,
            confidence=finalized.get("confidence"),
            latency_ms=latency_ms,
            input_summary=f"{vendor_name} | {invoice_number} | {total_amount}",
            output_summary=finalized.get("reasoning"),
            object_type="invoice",
        )
        return finalized
    
    def _ai_comprehensive_comparison(
        self,
        current_invoice: Dict[str, Any],
        candidates: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        AI analyzes ALL candidates with fuzzy matching rules
        """
        import json
        
        prompt = f"""You are a duplicate detection expert. Analyze if this invoice is a duplicate.

CURRENT INVOICE:
- Vendor: {current_invoice['vendor_name']}
- Invoice Number: {current_invoice['invoice_number']}
- Date: {current_invoice['invoice_date']}
- Amount: {current_invoice['total_amount']}
- Line Items: {json.dumps(current_invoice['line_items'], indent=2)}

CANDIDATE INVOICES (similar by vendor/date/amount):
{json.dumps(candidates, indent=2)}

FUZZY MATCHING RULES:
1. **Vendor Match** - "Vorson" matches "Vorson limited" (partial match OK)
2. **Invoice Number** - "0155" matches "SU26-000155" (substring match)
3. **Amount** - Small differences could be OCR errors or partial payments
4. **Date** - Within ±60 days is suspicious
5. **Line Items** - Same products = likely duplicate

DETECTION LOGIC:
- **EXACT DUPLICATE** (confidence 0.9+) if:
  - Vendor name contains same core word AND
  - Invoice numbers match OR have common pattern AND
  - Amount matches (±15%) OR line items match
  
- **POSSIBLE DUPLICATE** (confidence 0.5-0.8) if:
  - Similar vendor AND similar date AND similar amount
  
- **NOT DUPLICATE** (confidence <0.5) if:
  - Different vendor core name OR
  - Completely different invoice number pattern OR
  - Different line items

Return ONLY valid JSON (no markdown, no code blocks):
{{
    "is_duplicate": true,
    "confidence": 0.95,
    "strategy": "fuzzy_ai",
    "matched_invoice_id": "uuid-here",
    "reasoning": "Vendor 'Vorson' matches 'Vorson limited', invoice numbers similar, date within 3 days"
}}
"""
        
        try:
            response = self.ai.chat(
                messages=[
                    {"role": "system", "content": "You are a duplicate detection expert. ALWAYS return valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                context=None
            )
            
            logger.debug("AI raw response: %s", response)
            
            # ✅ Clean response (remove markdown code blocks)
            response = response.strip()
            if response.startswith("```") and response.endswith("```"):
                response = response[3:-3].strip()
            
            import json
            result = json.loads(response)
            result["strategy"] = "fuzzy_ai"
            return result
        
        except Exception as e:
            logger.exception("AI duplicate comparison failed")
            return {
                "is_duplicate": False,
                "confidence": 0.5,
                "strategy": "fuzzy_ai_failed",
                "matched_invoice_id": None,
                "reasoning": "Automated comparison was inconclusive; please review manually."
            }
