"""Integration tests for the invoice next-action agent (suggestion-only).

Policy decides the permitted action from deterministic signals; the AI may only
phrase the suggestion within that gate; every run is logged to ai_action_logs.
"""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole, VendorStatus, InvoiceState
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.ai_action_log import AIActionLog
from app.agents.invoice_agent import InvoiceNextActionAgent

pytestmark = pytest.mark.integration


def _vendor(db, tenant_id, user_id, status=VendorStatus.ACTIVE):
    v = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
        status=status, created_by=user_id,
    )
    db.add(v)
    db.flush()
    return v


def _invoice(db, tenant_id, user_id, **overrides):
    fields = dict(
        id=uuid.uuid4(), tenant_id=tenant_id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="V", invoice_date=date(2026, 1, 1), total_amount=100_000,
        current_state="draft", created_by=user_id,
    )
    fields.update(overrides)
    inv = Invoice(**fields)
    db.add(inv)
    db.flush()
    return inv


def _last_log(db):
    return (
        db.query(AIActionLog)
        .filter(AIActionLog.action == "invoice_next_action")
        .order_by(AIActionLog.created_at.desc(), AIActionLog.id.desc())
        .first()
    )


class TestRulesSuggestions:
    def test_draft_missing_fields(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        inv = _invoice(db, tenant.id, user["id"], invoice_number="", total_amount=0)
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "fix_missing_fields"
        assert any("missing_fields" in sig for sig in s["signals"])
        assert s["source"] == "rules"

    def test_draft_low_ocr_confidence_is_hitl(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id, ocr_confidence=50)
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "review_extraction"
        assert _last_log(db).status == "hitl_requested"

    def test_draft_complete_suggests_validate(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id, ocr_confidence=95)
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "validate"

    def test_pending_duplicate_suggests_resolution(self, db, tenant, make_user):
        user = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id, user["id"])
        other = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id)
        inv = _invoice(
            db, tenant.id, user["id"], vendor_id=vendor.id,
            current_state="pending_approval", potential_duplicate_id=other.id,
        )
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "resolve_duplicate"

    def test_pending_unverified_vendor_suggests_verification(self, db, tenant, make_user):
        user = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id, user["id"], status=VendorStatus.PENDING_VERIFICATION)
        inv = _invoice(
            db, tenant.id, user["id"], vendor_id=vendor.id, current_state="pending_approval",
        )
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "verify_vendor"

    def test_pending_clean_suggests_approve_with_role_and_sod(self, db, tenant, make_user):
        user = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(
            db, tenant.id, user["id"], vendor_id=vendor.id, current_state="pending_approval",
        )
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "approve"
        assert s["required_role"] == "manager"  # 100k, default routing
        # The suggester created the invoice, so SoD is surfaced as a signal.
        assert "sod=creator_cannot_approve" in s["signals"]

    def test_terminal_state_has_no_action(self, db, tenant, make_user):
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id, current_state="paid")
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "none"

    def test_missing_invoice_raises(self, db, make_user):
        user = make_user(UserRole.ADMIN)
        with pytest.raises(ValueError):
            InvoiceNextActionAgent(db, user).suggest(uuid.uuid4(), use_ai=False)

    def test_state_read_as_enum_is_normalised(self, db, tenant, make_user):
        """Rows loaded from the DB return current_state as an InvoiceState
        member, whose str() is 'InvoiceState.PENDING_APPROVAL'. Regression: that
        must still match the rules, not fall through to 'none'."""
        user = make_user(UserRole.MANAGER)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(
            db, tenant.id, user["id"], vendor_id=vendor.id,
            current_state=InvoiceState.PENDING_APPROVAL,  # enum, as SQLAlchemy returns
        )
        s = InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)
        assert s["action"] == "approve"
        assert "state=pending_approval" in s["signals"]

    def test_ai_action_log_is_persisted(self, db, tenant, make_user):
        """The suggestion endpoint is read-only, so the agent must commit its own
        AI-action log or the trail is silently lost."""
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id)
        InvoiceNextActionAgent(db, user).suggest(inv.id, use_ai=False)

        logged = (
            db.query(AIActionLog)
            .filter(AIActionLog.action == "invoice_next_action",
                    AIActionLog.object_id == inv.id)
            .first()
        )
        assert logged is not None
        assert logged.status in ("completed", "hitl_requested")


class _StubAI:
    """Stands in for the provider: returns a canned chat response."""
    model = "stub-model"

    def __init__(self, response: str):
        self._response = response

    def chat(self, messages, context=None):
        return self._response


def _agent_with_ai(db, user, response: str):
    """An agent whose every model candidate returns `response`.

    Injected at the provider factory rather than by replacing the agent's
    client, so these tests now run through the real router — rendering the
    registered prompt, parsing, and validating against its schema — instead of
    around it. The gate is what is being tested, and the gate sits after all of
    that.
    """
    from app.services.ai.router import AIRouter

    router = AIRouter(provider_factory=lambda provider, model: _StubAI(response))
    return InvoiceNextActionAgent(db, user, router=router)


class TestAIGate:
    def test_valid_ai_output_is_used(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id)
        agent = _agent_with_ai(db, user, '{"action": "validate", "confidence": 0.9, "reasoning": "Fields look complete."}')
        s = agent.suggest(inv.id, use_ai=True)
        assert s["source"] == "ai"
        assert s["confidence"] == 0.9
        assert _last_log(db).status == "completed"

    def test_ai_straying_outside_gate_falls_back_to_rules(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id)
        # Policy permits "validate"; the AI tries to approve — must be discarded.
        agent = _agent_with_ai(db, user, '{"action": "approve", "confidence": 0.99, "reasoning": "Just approve it."}')
        s = agent.suggest(inv.id, use_ai=True)
        assert s["action"] == "validate"
        assert s["source"] == "rules"
        assert _last_log(db).status == "failed_schema"

    def test_malformed_ai_output_falls_back_to_rules(self, db, tenant, make_user):
        user = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, user["id"])
        inv = _invoice(db, tenant.id, user["id"], vendor_id=vendor.id)
        agent = _agent_with_ai(db, user, "I think you should probably validate it?")
        s = agent.suggest(inv.id, use_ai=True)
        assert s["action"] == "validate"
        assert s["source"] == "rules"
        assert _last_log(db).status == "failed_schema"
