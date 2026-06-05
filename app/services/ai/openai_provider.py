from openai import OpenAI
import json
import logging
from typing import Dict, Any, List, Optional
from app.core.config import settings
from app.services.ai.base import AIProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(AIProvider):
    """OpenAI GPT implementation"""
    
    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.model = settings.OPENAI_MODEL or "gpt-4"
    
    def chat(self, messages: List[Dict[str, str]], context: Optional[Dict[str, Any]] = None) -> str:
        """Chat completion with GPT"""
        try:
            # Add system context if provided
            if context:
                system_msg = f"Context: {json.dumps(context)}"
                messages.insert(0, {"role": "system", "content": system_msg})
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=1500
            )
            
            content = response.choices[0].message.content or ""
            return content

        except Exception:
            # Log internals server-side; never return raw error text to the client.
            logger.exception("OpenAI chat failed")
            return "The assistant is temporarily unavailable. Please try again."
    
    def extract_invoice_fields(
        self, 
        ocr_text: str, 
        raw_ocr_data: Optional[Dict] = None,
        line_items: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Extract invoice fields using GPT-4 structured output"""
        try:
            # ✅ Build enhanced prompt with OCR line items for fixing
            line_items_context = ""
            if line_items:
                import json
                line_items_context = f"""
OCR extracted these line items (may contain errors):
{json.dumps(line_items, indent=2)}

IMPORTANT: Fix any fragmented descriptions. For example:
- "Proof Torch for Helmet Red" + "Color" should merge to "Proof Torch for Helmet Red Color"
- "Yellow Size: 41 to 46" + "Hard Toe" should merge to "Yellow Size: 41 to 46 - Hard Toe"
"""
            
            prompt = f"""Extract invoice data from this OCR text. Return ONLY a JSON object with these exact keys:
{{
    "vendor_name": "extracted vendor name",
    "invoice_number": "extracted invoice number",
    "invoice_date": "YYYY-MM-DD",
    "due_date": "YYYY-MM-DD or null",
    "total_amount": 0.0,
    "tax_amount": 0.0,
    "subtotal_amount": 0.0,
    "currency": "PKR",
    "line_items": [
        {{
            "description": "Complete product description (merge fragments)",
            "quantity": 0.0,
            "unit_price": 0.0,
            "amount": 0.0,
            "product_code": ""
        }}
    ],
    "confidence": 85,
    "ai_corrections": {{
        "line_items_merged": ["list of merged items"],
        "descriptions_fixed": {{"old": "new"}}
    }}
}}

{line_items_context}

OCR Text:
{ocr_text}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert invoice data extractor. Always return valid JSON. Merge fragmented line item descriptions intelligently."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=2000  # Increased for line items
            )
            
            result_text = response.choices[0].message.content
            
            # Parse JSON response
            try:
                result = json.loads(result_text) #type: ignore
            except json.JSONDecodeError:
                raise ValueError("LLM did not return valid JSON")
            
            # Ensure required fields
            result.setdefault("vendor_name", "")
            result.setdefault("invoice_number", "")
            result.setdefault("invoice_date", None)
            result.setdefault("total_amount", 0.0)
            result.setdefault("tax_amount", 0.0)
            result.setdefault("confidence", 70)
            result.setdefault("line_items", [])
            result.setdefault("ai_corrections", {})
            
            logger.info(f"AI extracted: {result['vendor_name']}, {result['invoice_number']}, {len(result['line_items'])} line items")
            
            return result
        
        except Exception:
            logger.exception("AI invoice extraction failed")
            return {
                "vendor_name": "",
                "invoice_number": "",
                "invoice_date": None,
                "total_amount": 0.0,
                "tax_amount": 0.0,
                "confidence": 0,
                "line_items": [],
                "error": "AI extraction failed",
            }
    
    def detect_duplicate_invoices(
        self, 
        current_invoice: Dict[str, Any], 
        candidate_invoices: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """AI-powered duplicate detection"""
        try:
            if not candidate_invoices:
                return {
                    "is_duplicate": False,
                    "confidence": 1.0,
                    "matched_invoice_id": None,
                    "similarity_score": 0.0,
                    "reasoning": "No similar invoices found"
                }
            
            # Format line items for better readability
            current_items = current_invoice.get('line_items', [])
            current_items_str = json.dumps(current_items, indent=2) if current_items else "No line items"
            
            candidates_with_items = []
            for candidate in candidate_invoices:
                items = candidate.get('line_items', [])
                candidates_with_items.append({
                    "id": candidate.get("id"),
                    "invoice_number": candidate.get("invoice_number"),
                    "invoice_date": candidate.get("invoice_date"),
                    "total_amount": candidate.get("total_amount"),
                    "line_items": items
                })
            
            prompt = f"""Analyze if this invoice is a duplicate of any existing invoices.

Current Invoice:
- Vendor: {current_invoice.get('vendor_name')}
- Invoice Number: {current_invoice.get('invoice_number')}
- Date: {current_invoice.get('invoice_date')}
- Amount: {current_invoice.get('total_amount')}
- Line Items: {current_items_str}

Potentially Similar Invoices:
{json.dumps(candidates_with_items, indent=2)}

Consider:
1. Exact invoice number match = very high duplicate probability
2. Same vendor + date + amount + line items = duplicate
3. Similar line items but different invoice number = possible resubmission
4. Different line items = likely not duplicate even if amounts match

Return ONLY a JSON object:
{{
    "is_duplicate": true/false,
    "confidence": 0.0-1.0,
    "matched_invoice_id": "uuid or null",
    "similarity_score": 0.0-1.0,
    "reasoning": "brief explanation (mention if line items were compared)"
}}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert at detecting duplicate invoices. Compare all fields including line items for accurate detection."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=500  # Increased for line item reasoning
            )
            
            try:
                result = json.loads(response.choices[0].message.content) #type: ignore
            except json.JSONDecodeError:
                raise ValueError("LLM did not return valid JSON")

            return result

        except Exception:
            logger.exception("AI duplicate detection failed")
            return {
                "is_duplicate": False,
                "confidence": 0.0,
                "matched_invoice_id": None,
                "similarity_score": 0.0,
                "reasoning": "Duplicate analysis is temporarily unavailable.",
            }
    
    def chat_with_tools(
        self,
        messages: List[Dict[str, str]],
        context: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        db: Optional[Any] = None
    ) -> Dict[str, Any]:
        """Chat with function calling support"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=1500,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None
            )
            
            message = response.choices[0].message
            
            # If AI wants to call a function
            if message.tool_calls:
                tool_call = message.tool_calls[0]
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)
                
                # Execute the function — scope to the caller's tenant.
                if db and function_name == "query_invoices":
                    tenant_id = (context or {}).get("tenant_id")
                    result = self._execute_invoice_query(db, function_args, tenant_id)
                    
                    # Add function result to messages
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result)
                    })
                    
                    # Get final AI response
                    final_response = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        temperature=0.7
                    )
                    
                    return {
                        "content": final_response.choices[0].message.content,
                        "function_called": function_name,
                        "function_result": result
                    }
            
            # No function call, return regular response
            return {
                "content": message.content or "",
                "function_called": None,
                "function_result": None
            }
        
        except Exception:
            logger.exception("OpenAI chat with tools failed")
            return {
                "content": "The assistant is temporarily unavailable. Please try again.",
                "function_called": None,
                "function_result": None
            }

    def _execute_invoice_query(self, db, args: Dict[str, Any], tenant_id: Optional[Any] = None) -> Dict[str, Any]:
        """Execute invoice query based on AI parameters.

        Always scoped to the caller's tenant: defense-in-depth alongside RLS, so
        the AI tool can never read another tenant's invoices even if the session
        is not RLS-bound. A missing tenant_id yields no rows rather than a leak.
        """
        from app.models.invoice import Invoice
        from sqlalchemy import desc

        query = db.query(Invoice).filter(Invoice.tenant_id == tenant_id)

        # Apply filters
        if args.get("status"):
            query = query.filter(Invoice.current_state == args["status"])
        
        if args.get("vendor_name"):
            query = query.filter(Invoice.vendor_name.ilike(f"%{args['vendor_name']}%"))
        
        if args.get("min_amount"):
            query = query.filter(Invoice.total_amount >= args["min_amount"])
        
        if args.get("max_amount"):
            query = query.filter(Invoice.total_amount <= args["max_amount"])
        
        # Sorting
        if args.get("sort_by") == "amount_desc":
            query = query.order_by(desc(Invoice.total_amount))
        else:
            query = query.order_by(desc(Invoice.created_at))
        
        # Limit
        limit = min(args.get("limit", 10), 50)
        invoices = query.limit(limit).all()
        
        # Format results
        return {
            "count": len(invoices),
            "invoices": [
                {
                    "id": str(inv.id),
                    "invoice_number": inv.invoice_number,
                    "vendor_name": inv.vendor_name,
                    "total_amount": float(inv.total_amount or 0),
                    "currency": inv.currency,
                    "status": inv.current_state,
                    "invoice_date": str(inv.invoice_date)
                }
                for inv in invoices
            ]
        }
