"""
Query Agent - Converts natural language to SQL queries

Examples:
- "Show me pending invoices" -> query_invoices(status="pending_approval")
- "Invoices below 25000" -> query_invoices(max_amount=25000)
- "Top vendors this month" -> get_top_vendors()
"""

import logging
import json
import time
from typing import Dict, Any
from sqlalchemy.orm import Session

from app.services.ai import get_ai_provider
from app.agents.tools.sql_tools import SQLTools, get_tool_definitions
from app.services.ai_action_log import log_ai_action, STATUS_COMPLETED, STATUS_ERROR
from app.core.config import settings

logger = logging.getLogger(__name__)

PROMPT_VERSION = "nl-query-v1"


class QueryAgent:
    """Agent for natural language query to SQL"""
    
    def __init__(self, db: Session, tenant_id: str, user_context: Dict[str, Any]):
        self.db = db
        self.tenant_id = tenant_id
        self.user_context = user_context
        self.tools = SQLTools(db, tenant_id)
        self.ai = get_ai_provider()
    
    def query(self, natural_language_query: str) -> Dict[str, Any]:
        """
        Convert natural language to SQL and execute
        
        Returns:
            {
                "query": str,
                "ai_response": str,
                "data": Any,
                "function_called": str,
                "sql_executed": bool
            }
        """
        # Build system prompt
        system_prompt = f"""You are Sarmaya OS Query Agent. Convert user questions to function calls.

Available functions:
1. query_invoices - For invoice queries (status, amount, vendor filters)
2. find_similar_invoices - For finding duplicates
3. get_vendor_statistics - For vendor analytics
4. get_top_vendors - For top vendors by amount

User Context:
- Tenant: {self.user_context.get('tenant_id')}
- Role: {self.user_context.get('role')}

Examples:
- "pending invoices" -> query_invoices(status="pending_approval")
- "invoices below 25000" -> query_invoices(max_amount=25000)
- "ABC Corp invoices" -> query_invoices(vendor_name="ABC Corp")
- "top 5 vendors" -> get_top_vendors(limit=5)

Always call a function when possible. If ambiguous, ask clarifying questions.
"""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": natural_language_query}
        ]
        
        started = time.monotonic()
        try:
            # Use AI with function calling. Pass tenant context so the executed
            # tool query is scoped to this tenant (defense-in-depth with RLS).
            result = self.ai.chat_with_tools(
                messages=messages,
                context={"tenant_id": self.tenant_id},
                tools=get_tool_definitions(),
                db=self.db
            )

            self._log(
                STATUS_COMPLETED, started, natural_language_query,
                output_summary=f"function={result.get('function_called')}",
            )

            if result.get("function_called"):
                # Function was executed
                return {
                    "query": natural_language_query,
                    "ai_response": result["content"],
                    "data": result["function_result"],
                    "function_called": result["function_called"],
                    "sql_executed": True
                }
            else:
                # No function call, just conversation
                return {
                    "query": natural_language_query,
                    "ai_response": result["content"],
                    "data": None,
                    "function_called": None,
                    "sql_executed": False
                }

        except Exception:
            logger.exception("Query agent failed")
            self._log(STATUS_ERROR, started, natural_language_query, output_summary="error")
            return {
                "query": natural_language_query,
                "ai_response": "Sorry, the query could not be processed. Please try again.",
                "data": None,
                "function_called": None,
                "sql_executed": False
            }

    def _log(self, status: str, started: float, query_text: str, output_summary: str) -> None:
        """Record the AI action (Build Book: every AI action logged)."""
        log_ai_action(
            self.db, self.tenant_id, self.user_context.get("id"),
            action="nl_query",
            status=status,
            ai_provider=settings.AI_PROVIDER,
            ai_model=getattr(self.ai, "model", None),
            prompt_version=PROMPT_VERSION,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_summary=query_text,
            output_summary=output_summary,
        )
    
    def explain_query(self, natural_language_query: str) -> Dict[str, Any]:
        """Explain what SQL would be generated (dry run)"""
        prompt = f"""Explain how you would query the database for: "{natural_language_query}"

Which function would you call and with what parameters?

Return JSON:
{{
    "function": "function_name",
    "parameters": {{}},
    "explanation": "Why this query strategy"
}}
"""
        
        try:
            response = self.ai.chat(
                messages=[{"role": "user", "content": prompt}],
                context=None
            )
            return json.loads(response)
        except Exception as e:
            return {"error": str(e)}
