from argparse import OPTIONAL
from fastapi import HTTPException
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
            print("OpenAI response content:", content)
            return content
        
        except Exception as e:
            logger.error(f"OpenAI chat failed: {str(e)}")
            return f"AI error: {str(e)}"
    
    def extract_invoice_fields(self, ocr_text: str, raw_ocr_data: Optional[Dict] = None) -> Dict[str, Any]:
        """Extract invoice fields using GPT-4 structured output"""
        try:
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
    "line_items": [],
    "confidence": 85,
    "ai_corrections": {{}}
}}

OCR Text:
{ocr_text}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert invoice data extractor. Always return valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=1000
            )
            
            result_text = response.choices[0].message.content
            
            # Parse JSON response
            try:
                result = json.loads(result_text) #type: ignore
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse LLM response as JSON: {e}"
                )
            
            # Ensure required fields
            result.setdefault("vendor_name", "")
            result.setdefault("invoice_number", "")
            result.setdefault("invoice_date", None)
            result.setdefault("total_amount", 0.0)
            result.setdefault("tax_amount", 0.0)
            result.setdefault("confidence", 70)
            
            logger.info(f"AI extracted: {result['vendor_name']}, {result['invoice_number']}")
            
            return result
        
        except Exception as e:
            logger.error(f"AI invoice extraction failed: {str(e)}")
            return {
                "vendor_name": "",
                "invoice_number": "",
                "invoice_date": None,
                "total_amount": 0.0,
                "tax_amount": 0.0,
                "confidence": 0,
                "error": str(e)
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
            
            prompt = f"""Analyze if this invoice is a duplicate of any existing invoices.

Current Invoice:
- Vendor: {current_invoice.get('vendor_name')}
- Invoice Number: {current_invoice.get('invoice_number')}
- Date: {current_invoice.get('invoice_date')}
- Amount: {current_invoice.get('total_amount')}

Potentially Similar Invoices:
{json.dumps(candidate_invoices, indent=2)}

Return ONLY a JSON object:
{{
    "is_duplicate": true/false,
    "confidence": 0.0-1.0,
    "matched_invoice_id": "uuid or null",
    "similarity_score": 0.0-1.0,
    "reasoning": "brief explanation"
}}
"""
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You are an expert at detecting duplicate invoices."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=300
            )
            
            try:
                result = json.loads(response.choices[0].message.content) #type: ignore
            except json.JSONDecodeError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse LLM response as JSON: {e}"
                )
            
            return result
        
        except Exception as e:
            logger.error(f"AI duplicate detection failed: {str(e)}")
            return {
                "is_duplicate": False,
                "confidence": 0.0,
                "matched_invoice_id": None,
                "similarity_score": 0.0,
                "reasoning": f"Error: {str(e)}"
            }
    
    def query_system(self, query: str, context: Dict[str, Any]) -> str:
        """Natural language query agent"""
        try:
            # Build context-aware prompt
            system_prompt = f"""You are Sarmaya OS AI Assistant. You help users query invoices, vendors, and financial data.

User Context:
- Tenant: {context.get('tenant_name', 'Unknown')}
- Role: {context.get('role', 'user')}
- Available data: {context.get('data_summary', 'invoices, vendors, policies')}

Answer questions about:
- Pending invoices
- Invoice status
- Vendor information
- Financial summaries
- Approval requirements

If you need specific data, say "I need to query the database for..." and suggest a SQL-like filter.
"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query}
            ]
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=800
            )
            
            return response.choices[0].message.content or ""
        
        except Exception as e:
            logger.error(f"AI query failed: {str(e)}")
            return f"Sorry, I encountered an error: {str(e)}"
