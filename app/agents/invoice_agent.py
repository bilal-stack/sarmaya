"""Invoice next-action agent (suggestion-only).

Decides what should happen next to an invoice — review the extraction, fix
missing fields, validate, submit, resolve a duplicate, verify the vendor,
approve, mark paid — and says why. Per the Build Book ("Agents Assist.
Policies Decide."), the agent NEVER executes a step or moves workflow state:

  * deterministic signals (state, missing fields, OCR confidence, duplicate
    flag, vendor status, approval routing) fix the policy-permitted action;
  * the AI, when used, may only phrase the suggestion within that permitted
    action — its output is schema-validated, and if it strays or returns
    malformed JSON it is discarded in favor of the rules result
    (status=failed_schema in the AI action log);
  * every run is logged to ai_action_logs with provenance, and HITL-type
    suggestions (extraction review, duplicate, vendor) log hitl_requested.
"""
import json
import logging
import time
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import InvoiceState, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.schemas.ai import InvoiceNextActionSuggestion
from app.services.ai import get_ai_provider
from app.services.ai_action_log import (
    log_ai_action,
    STATUS_COMPLETED,
    STATUS_FAILED_SCHEMA,
    STATUS_HITL,
)
from app.services.policy import explain_approval_routing

logger = logging.getLogger(__name__)

PROMPT_VERSION = "invoice-next-action-v1"

# Suggestions that require a human decision (Build Book HITL triggers).
HITL_ACTIONS = {"review_extraction", "resolve_duplicate", "verify_vendor"}


class InvoiceNextActionAgent:
    """Suggests (never executes) the next step for an invoice."""

    def __init__(self, db: Session, current_user: dict):
        self.db = db
        self.current_user = current_user
        self.ai = get_ai_provider()

    def suggest(self, invoice_id: UUID, use_ai: bool = True) -> dict:
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError("Invoice not found")

        action, reasoning, signals, required_role = self._rules(invoice)
        suggestion = InvoiceNextActionSuggestion(
            action=action,
            confidence=1.0,
            reasoning=reasoning,
            signals=signals,
            required_role=required_role,
            source="rules",
        )

        status = STATUS_HITL if action in HITL_ACTIONS else STATUS_COMPLETED
        started = time.monotonic()

        if use_ai:
            ai_suggestion = self._ai_enrich(invoice, action, signals, required_role)
            if ai_suggestion is not None:
                suggestion = ai_suggestion
            else:
                # Malformed / out-of-gate AI output: rules result stands.
                status = STATUS_FAILED_SCHEMA

        log_ai_action(
            self.db,
            invoice.tenant_id,
            self.current_user.get("id"),
            action="invoice_next_action",
            status=status,
            ai_provider=settings.AI_PROVIDER if use_ai else None,
            ai_model=getattr(self.ai, "model", None) if use_ai else None,
            prompt_version=PROMPT_VERSION if use_ai else None,
            confidence=suggestion.confidence,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_summary=f"invoice={invoice.invoice_number} state={invoice.current_state}",
            output_summary=f"{suggestion.action}: {suggestion.reasoning}",
            object_type="invoice",
            object_id=invoice.id,
        )
        # Persist the log: this is a read-only endpoint, so nothing downstream
        # commits for us and the flushed row would otherwise be discarded.
        self.db.commit()

        result = suggestion.model_dump()
        result["invoice_id"] = str(invoice.id)
        return result

    # --- deterministic layer (policies decide) -------------------------------

    def _rules(self, invoice: Invoice) -> Tuple[str, str, List[str], Optional[str]]:
        """The policy-permitted next action, with the signals that led to it."""
        # current_state is a SQLEnum column: rows loaded from the DB come back as
        # InvoiceState members (whose str() is "InvoiceState.X"), while in-memory
        # rows may still hold the raw string. Normalise to the value either way.
        raw_state = invoice.current_state or InvoiceState.DRAFT.value
        state = str(getattr(raw_state, "value", raw_state)).lower()
        signals = [f"state={state}"]

        if state == InvoiceState.DRAFT.value:
            missing = self._missing_fields(invoice)
            if missing:
                signals.append("missing_fields=" + ",".join(missing))
                return (
                    "fix_missing_fields",
                    "Required fields are missing: " + ", ".join(missing) + ".",
                    signals, None,
                )
            confidence = invoice.ocr_confidence
            threshold = settings.AI_EXTRACTION_REVIEW_THRESHOLD
            if confidence is not None and confidence < threshold:
                signals.append(f"ocr_confidence={confidence}<{threshold}")
                return (
                    "review_extraction",
                    f"OCR extraction confidence ({confidence}%) is below the review "
                    f"threshold ({threshold}%); a human should verify the extracted fields.",
                    signals, None,
                )
            if confidence is not None:
                signals.append(f"ocr_confidence={confidence}>={threshold}")
            signals.append("required_fields=complete")
            return ("validate", "All required fields are present; ready to validate.", signals, None)

        if state == InvoiceState.VALIDATED.value:
            return ("submit_for_approval", "Invoice is validated; submit it for approval.", signals, None)

        if state == InvoiceState.PENDING_APPROVAL.value:
            if invoice.potential_duplicate_id and not invoice.duplicate_acknowledged:
                signals.append(f"potential_duplicate={invoice.potential_duplicate_id}")
                return (
                    "resolve_duplicate",
                    "Flagged as a potential duplicate; a reviewer must override it "
                    "with a logged reason (or reject) before approval.",
                    signals, None,
                )
            vendor_status = self._vendor_status(invoice)
            signals.append(f"vendor_status={vendor_status}")
            if vendor_status != VendorStatus.ACTIVE.value:
                return (
                    "verify_vendor",
                    f"The linked vendor is {vendor_status}; it must be verified and "
                    "activated before this invoice can be approved.",
                    signals, None,
                )
            routing = explain_approval_routing(
                self.db, invoice.tenant_id, float(invoice.total_amount or 0)
            )
            required_role = routing["required_role"]
            signals.append(f"required_role={required_role}")
            if str(invoice.created_by) == str(self.current_user.get("id")):
                signals.append("sod=creator_cannot_approve")
            return ("approve", routing["reason"], signals, required_role)

        if state == InvoiceState.APPROVED.value:
            return ("mark_paid", "Invoice is approved; it can be marked as paid.", signals, None)

        if state == InvoiceState.REJECTED.value:
            return ("revise", "Invoice was rejected; revise it and return it to draft.", signals, None)

        return ("none", f"Invoice is in a terminal state ({state}); no next action.", signals, None)

    @staticmethod
    def _missing_fields(invoice: Invoice) -> List[str]:
        missing: List[str] = []
        if not invoice.vendor_id:
            missing.append("vendor")
        if not (invoice.invoice_number or "").strip():
            missing.append("invoice_number")
        if not invoice.invoice_date:
            missing.append("invoice_date")
        if invoice.total_amount is None or invoice.total_amount <= 0:
            missing.append("total_amount")
        return missing

    def _vendor_status(self, invoice: Invoice) -> str:
        if not invoice.vendor_id:
            return "missing"
        vendor = self.db.query(Vendor).filter(Vendor.id == invoice.vendor_id).first()
        if not vendor:
            return "missing"
        return vendor.status.value if hasattr(vendor.status, "value") else str(vendor.status)

    # --- AI layer (assists inside the gate) -----------------------------------

    def _ai_enrich(
        self, invoice: Invoice, permitted_action: str, signals: List[str],
        required_role: Optional[str],
    ) -> Optional[InvoiceNextActionSuggestion]:
        """Ask the AI to phrase the suggestion for the permitted action. Returns
        None when the output is malformed or strays outside the gate — the
        caller then keeps the deterministic result."""
        prompt = f"""You are the invoice workflow assistant. Policy has already determined the next action for this invoice; your job is to explain it clearly to the user and estimate your confidence.

Invoice: {invoice.invoice_number} | vendor: {invoice.vendor_name} | amount: {invoice.total_amount} | state: {invoice.current_state}
Signals: {json.dumps(signals)}
Permitted next action (you MUST use exactly this): {permitted_action}

Return ONLY a JSON object:
{{"action": "{permitted_action}", "confidence": 0.0, "reasoning": "one or two sentences for the user"}}
"""
        try:
            raw = self.ai.chat(messages=[{"role": "user", "content": prompt}], context=None)
            data = _parse_json(raw)
            suggestion = InvoiceNextActionSuggestion(
                action=str(data.get("action", "")),
                confidence=data.get("confidence", 0.0),
                reasoning=str(data.get("reasoning", "")).strip(),
                signals=signals,
                required_role=required_role,
                source="ai",
                ai_provider=settings.AI_PROVIDER,
                ai_model=getattr(self.ai, "model", None),
                prompt_version=PROMPT_VERSION,
            )
            # The gate: the AI may not choose a different action than policy allows.
            if suggestion.action != permitted_action or not suggestion.reasoning:
                logger.warning(
                    "AI next-action output outside gate (got %r, permitted %r)",
                    suggestion.action, permitted_action,
                )
                return None
            return suggestion
        except Exception:
            logger.exception("AI next-action enrichment failed")
            return None


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
