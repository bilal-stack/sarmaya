"""
Duplicate Detection Agent with Multi-Strategy Approach

Strategies:
1. Exact match (invoice_number + vendor)
2. Fuzzy match (similar amount + date + vendor)
3. Line item comparison (deep semantic similarity)
"""

from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session

from app.services.ai import get_ai_provider
from app.agents.tools.sql_tools import SQLTools


class DuplicateDetectionAgent:
    """Agent for intelligent duplicate detection"""
    
    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.tools = SQLTools(db, tenant_id)
        self.ai = get_ai_provider()
    
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
        print(f"🔎 Duplicate check: {vendor_name} | {invoice_number} | {invoice_date} | {total_amount}")
        
        # Strategy 1: Exact invoice number match (with fuzzy vendor)
        exact_match = self.tools.check_exact_duplicate(vendor_name, invoice_number)
        if exact_match:
            print(f"✅ EXACT MATCH found: {exact_match['id']}")
            return {
                "is_duplicate": True,
                "confidence": 1.0,
                "strategy": "exact",
                "matched_invoice_id": exact_match["id"],
                "reasoning": f"Exact match: Invoice {invoice_number} already exists for {exact_match['vendor_name']}"
            }
        
        print("❌ No exact match, trying fuzzy search...")
        
        # Strategy 2: Fuzzy match with RELAXED constraints
        similar = self.tools.find_similar_invoices(
            vendor_name=vendor_name,
            invoice_date=invoice_date,
            total_amount=total_amount,
            window_days=60,  # ✅ Wider window
            amount_tolerance=0.15  # ✅ More tolerance
        )
        
        if not similar:
            print("❌ No similar invoices found")
            return {
                "is_duplicate": False,
                "confidence": 1.0,
                "strategy": "none",
                "matched_invoice_id": None,
                "reasoning": "No similar invoices found"
            }
        
        print(f"🤖 Sending {len(similar)} candidates to AI for analysis...")
        
        # Strategy 3: AI analysis of ALL candidates
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
        
        print(f"🤖 AI result: {ai_result}")
        
        return ai_result
    
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
            
            print(f"🤖 AI raw response: {response}")
            
            # ✅ Clean response (remove markdown code blocks)
            response = response.strip()
            if response.startswith("```") and response.endswith("```"):
                response = response[3:-3].strip()
            
            import json
            result = json.loads(response)
            result["strategy"] = "fuzzy_ai"
            return result
        
        except Exception as e:
            print(f"AI comparison failed: {str(e)}")
            return {
                "is_duplicate": False,
                "confidence": 0.5,
                "strategy": "fuzzy_ai_failed",
                "matched_invoice_id": None,
                "reasoning": f"AI comparison failed: {str(e)}"
            }
