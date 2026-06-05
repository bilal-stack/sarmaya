"""The AI invoice query tool must be scoped to the caller's tenant.

OpenAIProvider._execute_invoice_query is the function the LLM's tool call runs.
It must filter by tenant_id (defense-in-depth alongside RLS), so the assistant
can never surface another tenant's invoices — and a missing tenant_id must yield
nothing rather than leak everything.
"""
import uuid
from datetime import date

import pytest

from app.models.tenant import Tenant
from app.models.user import User
from app.models.invoice import Invoice
from app.core.enums import UserRole
from app.services.ai.openai_provider import OpenAIProvider

pytestmark = pytest.mark.integration


def _provider() -> OpenAIProvider:
    # Skip __init__ so we don't construct a real OpenAI client (no API key needed).
    return OpenAIProvider.__new__(OpenAIProvider)


def _tenant_with_invoice(db, number: str):
    t = Tenant(id=uuid.uuid4(), name=number, slug=f"{number}-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.flush()
    u = User(id=uuid.uuid4(), tenant_id=t.id, email=f"{uuid.uuid4().hex[:6]}@x.io",
             password="x", role=UserRole.ADMIN, is_active=True)
    db.add(u)
    db.flush()
    inv = Invoice(
        id=uuid.uuid4(), tenant_id=t.id, invoice_number=number, vendor_name="V",
        invoice_date=date(2026, 1, 1), total_amount=100, current_state="draft",
        created_by=u.id,
    )
    db.add(inv)
    db.flush()
    return t


class TestAIQueryTenantScope:
    def test_query_returns_only_callers_tenant(self, db):
        t_a = _tenant_with_invoice(db, "INV-A")
        _tenant_with_invoice(db, "INV-B")  # a different tenant's invoice

        result = _provider()._execute_invoice_query(db, {}, str(t_a.id))

        numbers = {inv["invoice_number"] for inv in result["invoices"]}
        assert numbers == {"INV-A"}

    def test_missing_tenant_returns_nothing(self, db):
        _tenant_with_invoice(db, "INV-A")

        result = _provider()._execute_invoice_query(db, {}, None)

        assert result["count"] == 0
        assert result["invoices"] == []
