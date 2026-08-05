"""Google Gemini implementation of the AI provider interface.

Mirrors OpenAIProvider and ClaudeProvider so the rest of the app stays
provider-agnostic — selected via AI_PROVIDER=gemini. Uses the google-genai SDK.

Three differences from the other two are adapted here:

  * the assistant role is called "model", not "assistant" (see _to_contents);
  * the system prompt is `system_instruction` on the request config rather than
    a message in the conversation; and
  * tools are FunctionDeclarations grouped into a Tool, not OpenAI's
    {type:"function", function:{...}} shape (see _to_gemini_tools).

Where the other providers ask for JSON in the prompt and parse whatever comes
back, Gemini can be told to emit JSON natively via response_mime_type, so the
JSON-returning methods use that. The output is still parsed defensively and
still validated by the caller's schema — the Build Book requires that AI output
is never trusted on the provider's word alone.

Model defaults to gemini-2.5-flash (settings.GEMINI_MODEL). Errors are logged
server-side and returned as generic messages; internal detail is never
surfaced to the client.
"""
import json
import logging
from typing import Dict, Any, List, Optional

from google import genai
from google.genai import types

from app.core.config import settings
from app.services.ai.base import AIProvider

logger = logging.getLogger(__name__)


# --- helpers ----------------------------------------------------------------

def _split_system(messages: List[Dict[str, str]]):
    """Return (system_text_or_None, contents). Pulls role=system entries out
    into system_instruction and maps the rest onto Gemini's roles, where the
    assistant is called "model"."""
    system_parts: List[str] = []
    contents: List[types.Content] = []
    for m in messages:
        role = m.get("role")
        text = m.get("content", "")
        if role == "system":
            if text:
                system_parts.append(text)
        elif role in ("user", "assistant"):
            contents.append(
                types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part(text=text)],
                )
            )
    if not contents:
        contents.append(
            types.Content(role="user", parts=[types.Part(text="(start of conversation)")])
        )
    return ("\n\n".join(system_parts) or None), contents


def _text_of(response) -> str:
    """Concatenate the text parts of a response, tolerating replies that carry
    only a function call (response.text raises or returns None there)."""
    try:
        parts = response.candidates[0].content.parts or []
    except (AttributeError, IndexError, TypeError):
        return ""
    return "".join(p.text for p in parts if getattr(p, "text", None))


def _parse_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences.

    response_mime_type makes fences unlikely, but the same defensive parse as
    the other providers is kept: a provider setting is not a guarantee.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    return json.loads(t)


def _to_gemini_tools(tools: Optional[List[Dict[str, Any]]]) -> List[types.Tool]:
    """Convert OpenAI function-calling tool defs to Gemini FunctionDeclarations."""
    declarations = []
    for t in tools or []:
        fn = t.get("function", t)  # OpenAI nests under "function"; tolerate flat too
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=fn.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return [types.Tool(function_declarations=declarations)] if declarations else []


def _execute_invoice_query(db, args: Dict[str, Any], tenant_id: Optional[Any]) -> Dict[str, Any]:
    """Tenant-scoped invoice query for the AI tool call (mirrors the other
    providers). Always filters by tenant_id; a missing tenant_id yields no rows
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

class GeminiProvider(AIProvider):
    """Google Gemini implementation of the shared provider interface."""

    def __init__(self):
        # Fail with a message that names the setting. The SDK's own error for a
        # missing key is a bare ValueError with a docs link, surfacing as an
        # opaque 500 that says nothing about which provider or which setting.
        if not settings.GOOGLE_AI_API_KEY:
            raise ValueError(
                "AI_PROVIDER is 'gemini' but GOOGLE_AI_API_KEY is not set. "
                "Add it to .env, or switch AI_PROVIDER to 'openai' or 'claude'."
            )
        self.client = genai.Client(api_key=settings.GOOGLE_AI_API_KEY)
        self.model = settings.GEMINI_MODEL or "gemini-2.5-flash"

    def chat(self, messages: List[Dict[str, str]], context: Optional[Dict[str, Any]] = None) -> str:
        try:
            system, contents = _split_system(messages)
            if context:
                ctx = f"Context: {json.dumps(context)}"
                system = f"{ctx}\n\n{system}" if system else ctx
            resp = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=1500,
                ),
            )
            return _text_of(resp)
        except Exception:
            logger.exception("Gemini chat failed")
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
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are an expert invoice data extractor. Always return valid "
                        "JSON. Merge fragmented line item descriptions intelligently."
                    ),
                    max_output_tokens=2000,
                    response_mime_type="application/json",
                ),
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
                "Gemini extracted: %s, %s, %d line items",
                result.get("vendor_name"), result.get("invoice_number"),
                len(result.get("line_items", [])),
            )
            return result
        except Exception:
            logger.exception("Gemini invoice extraction failed")
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
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are an expert at detecting duplicate invoices. Compare all "
                        "fields including line items. Always return valid JSON."
                    ),
                    max_output_tokens=500,
                    response_mime_type="application/json",
                ),
            )
            return _parse_json(_text_of(resp))
        except Exception:
            logger.exception("Gemini duplicate detection failed")
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
            system, contents = _split_system(messages)
            gemini_tools = _to_gemini_tools(tools)
            tenant_id = (context or {}).get("tenant_id")

            config = types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=1500,
                tools=gemini_tools or None,
                # The SDK will otherwise call the tool itself. The query must run
                # through _execute_invoice_query so it stays tenant-scoped, so
                # the loop is driven here as in the other providers.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            resp = self.client.models.generate_content(
                model=self.model, contents=contents, config=config
            )

            calls = getattr(resp, "function_calls", None) or []
            call = next((c for c in calls if c.name == "query_invoices"), None)
            if call is not None and db is not None:
                result = _execute_invoice_query(db, dict(call.args or {}), tenant_id)
                contents.append(resp.candidates[0].content)
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(
                            function_response=types.FunctionResponse(
                                name=call.name, response={"result": result},
                            )
                        )],
                    )
                )
                final = self.client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                return {
                    "content": _text_of(final),
                    "function_called": "query_invoices",
                    "function_result": result,
                }

            return {"content": _text_of(resp), "function_called": None, "function_result": None}
        except Exception:
            logger.exception("Gemini chat with tools failed")
            return {
                "content": "The assistant is temporarily unavailable. Please try again.",
                "function_called": None,
                "function_result": None,
            }
