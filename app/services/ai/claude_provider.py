"""Anthropic Claude implementation of the AI provider interface.

Mirrors OpenAIProvider so the rest of the app is provider-agnostic — selected
via AI_PROVIDER=claude. Uses the official Anthropic SDK (Messages API). The
Messages API differs from OpenAI in two ways this module adapts:

  * the system prompt is a top-level parameter, not a message with role
    "system" (see _split_system); and
  * tools use {name, description, input_schema}, not OpenAI's
    {type:"function", function:{...}} shape (see _to_anthropic_tools).

Model defaults to claude-opus-4-8 (settings.ANTHROPIC_MODEL; set e.g.
claude-haiku-4-5 in .env for a cheaper demo). Errors are logged server-side and
returned as generic messages — internal detail is never surfaced to the client.
"""
import json
import logging
from typing import Dict, Any, List, Optional

import anthropic

from app.core.config import settings
from app.services.ai.base import AIProvider

logger = logging.getLogger(__name__)


# --- helpers ----------------------------------------------------------------

def _split_system(messages: List[Dict[str, str]]):
    """Return (system_text_or_None, anthropic_messages). Pulls any role=system
    entries out into the system param; keeps user/assistant turns. The Anthropic
    API requires the first message to be a user turn."""
    system_parts: List[str] = []
    converted: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if content:
                system_parts.append(content)
        elif role in ("user", "assistant"):
            converted.append({"role": role, "content": content})
    if not converted or converted[0]["role"] != "user":
        converted.insert(0, {"role": "user", "content": "(start of conversation)"})
    return ("\n\n".join(system_parts) or None), converted


def _text_of(message) -> str:
    """Concatenate the text blocks of an Anthropic message (skips thinking/tool)."""
    return "".join(b.text for b in message.content if b.type == "text")


def _parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    return json.loads(t)


def _to_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Convert OpenAI function-calling tool defs to Anthropic tool defs."""
    out = []
    for t in tools or []:
        fn = t.get("function", t)  # OpenAI nests under "function"; tolerate flat too
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _execute_invoice_query(db, args: Dict[str, Any], tenant_id: Optional[Any]) -> Dict[str, Any]:
    """Tenant-scoped invoice query for the AI tool call (mirrors the OpenAI
    provider). Always filters by tenant_id; a missing tenant_id yields no rows
    rather than leaking across tenants."""
    from app.models.invoice import Invoice
    from sqlalchemy import desc

    query = db.query(Invoice).filter(Invoice.tenant_id == tenant_id)
    if args.get("status"):
        query = query.filter(Invoice.current_state == args["status"])
    if args.get("vendor_name"):
        query = query.filter(Invoice.vendor_name.ilike(f"%{args['vendor_name']}%"))
    if args.get("min_amount"):
        query = query.filter(Invoice.total_amount >= args["min_amount"])
    if args.get("max_amount"):
        query = query.filter(Invoice.total_amount <= args["max_amount"])

    if args.get("sort_by") == "amount_desc":
        query = query.order_by(desc(Invoice.total_amount))
    else:
        query = query.order_by(desc(Invoice.created_at))

    limit = min(args.get("limit", 10), 50)
    invoices = query.limit(limit).all()
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
                "invoice_date": str(inv.invoice_date),
            }
            for inv in invoices
        ],
    }


# --- provider ---------------------------------------------------------------

class ClaudeProvider(AIProvider):
    """Claude (Anthropic) GPT-equivalent implementation."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.ANTHROPIC_MODEL or "claude-opus-4-8"

    def chat(self, messages: List[Dict[str, str]], context: Optional[Dict[str, Any]] = None) -> str:
        try:
            system, conv = _split_system(messages)
            if context:
                ctx = f"Context: {json.dumps(context)}"
                system = f"{ctx}\n\n{system}" if system else ctx
            kwargs: Dict[str, Any] = {"model": self.model, "max_tokens": 1500, "messages": conv}
            if system:
                kwargs["system"] = system
            resp = self.client.messages.create(**kwargs)
            return _text_of(resp)
        except Exception:
            logger.exception("Claude chat failed")
            return "The assistant is temporarily unavailable. Please try again."

    def extract_invoice_fields(
        self,
        ocr_text: str,
        raw_ocr_data: Optional[Dict] = None,
        line_items: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        try:
            line_items_context = ""
            if line_items:
                line_items_context = (
                    "\nOCR extracted these line items (may contain errors):\n"
                    f"{json.dumps(line_items, indent=2)}\n\n"
                    "IMPORTANT: Merge fragmented descriptions into complete ones."
                )
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
        {{"description": "Complete product description", "quantity": 0.0, "unit_price": 0.0, "amount": 0.0, "product_code": ""}}
    ],
    "confidence": 85,
    "ai_corrections": {{"line_items_merged": [], "descriptions_fixed": {{}}}}
}}
{line_items_context}

OCR Text:
{ocr_text}
"""
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2000,
                system="You are an expert invoice data extractor. Always return valid JSON. Merge fragmented line item descriptions intelligently.",
                messages=[{"role": "user", "content": prompt}],
            )
            result = _parse_json(_text_of(resp))
            result.setdefault("vendor_name", "")
            result.setdefault("invoice_number", "")
            result.setdefault("invoice_date", None)
            result.setdefault("total_amount", 0.0)
            result.setdefault("tax_amount", 0.0)
            result.setdefault("confidence", 70)
            result.setdefault("line_items", [])
            result.setdefault("ai_corrections", {})
            logger.info(
                "Claude extracted: %s, %s, %d line items",
                result.get("vendor_name"), result.get("invoice_number"), len(result.get("line_items", [])),
            )
            return result
        except Exception:
            logger.exception("Claude invoice extraction failed")
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
        candidate_invoices: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        try:
            if not candidate_invoices:
                return {
                    "is_duplicate": False,
                    "confidence": 1.0,
                    "matched_invoice_id": None,
                    "similarity_score": 0.0,
                    "reasoning": "No similar invoices found",
                }
            prompt = f"""Analyze if this invoice is a duplicate of any existing invoices.

Current Invoice:
- Vendor: {current_invoice.get('vendor_name')}
- Invoice Number: {current_invoice.get('invoice_number')}
- Date: {current_invoice.get('invoice_date')}
- Amount: {current_invoice.get('total_amount')}
- Line Items: {json.dumps(current_invoice.get('line_items', []), indent=2)}

Potentially Similar Invoices:
{json.dumps(candidate_invoices, indent=2)}

Return ONLY a JSON object:
{{
    "is_duplicate": true,
    "confidence": 0.0,
    "matched_invoice_id": "uuid or null",
    "similarity_score": 0.0,
    "reasoning": "brief explanation"
}}
"""
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system="You are an expert at detecting duplicate invoices. Compare all fields including line items. Always return valid JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            return _parse_json(_text_of(resp))
        except Exception:
            logger.exception("Claude duplicate detection failed")
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
        db: Optional[Any] = None,
    ) -> Dict[str, Any]:
        try:
            system, conv = _split_system(messages)
            anthropic_tools = _to_anthropic_tools(tools)
            tenant_id = (context or {}).get("tenant_id")

            kwargs: Dict[str, Any] = {"model": self.model, "max_tokens": 1500, "messages": conv}
            if system:
                kwargs["system"] = system
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools
            resp = self.client.messages.create(**kwargs)

            if resp.stop_reason == "tool_use":
                tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
                if tool_use is not None and db is not None and tool_use.name == "query_invoices":
                    result = _execute_invoice_query(db, dict(tool_use.input), tenant_id)
                    conv.append({"role": "assistant", "content": resp.content})
                    conv.append({
                        "role": "user",
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps(result),
                        }],
                    })
                    final_kwargs = {"model": self.model, "max_tokens": 1500, "messages": conv}
                    if system:
                        final_kwargs["system"] = system
                    if anthropic_tools:
                        final_kwargs["tools"] = anthropic_tools
                    final = self.client.messages.create(**final_kwargs)
                    return {
                        "content": _text_of(final),
                        "function_called": "query_invoices",
                        "function_result": result,
                    }

            return {"content": _text_of(resp), "function_called": None, "function_result": None}
        except Exception:
            logger.exception("Claude chat with tools failed")
            return {
                "content": "The assistant is temporarily unavailable. Please try again.",
                "function_called": None,
                "function_result": None,
            }
