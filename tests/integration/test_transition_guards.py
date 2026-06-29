"""Integration tests: the workflow engine enforces configured transition guards."""
import uuid
from datetime import date

import pytest

from app.core.enums import UserRole
from app.models.workflow_state import WorkflowState
from app.models.invoice import Invoice
from app.services.workflow import transition_state

pytestmark = pytest.mark.integration


def _seed_draft_state(db, tenant_id, guards):
    db.add(WorkflowState(
        tenant_id=tenant_id, workflow_type="invoice", state_name="draft",
        state_order=1, allowed_transitions=["validated"], guards=guards,
    ))
    db.flush()


class TestTransitionGuards:
    def test_guard_blocks_transition_when_fields_missing(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _seed_draft_state(db, tenant.id, {"validated": ["required_fields_present"]})
        inv = Invoice(
            tenant_id=tenant.id, current_state="draft", invoice_number="",
            vendor_name="V", invoice_date=None, total_amount=0,
            vendor_id=None, created_by=admin["id"],
        )
        with pytest.raises(ValueError):
            transition_state(db, inv, "validated", admin["id"])
        assert inv.current_state == "draft"  # unchanged

    def test_guard_allows_transition_when_satisfied(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _seed_draft_state(db, tenant.id, {"validated": ["required_fields_present"]})
        inv = Invoice(
            tenant_id=tenant.id, current_state="draft", invoice_number="INV-1",
            vendor_name="V", invoice_date=date(2026, 1, 1), total_amount=100,
            vendor_id=uuid.uuid4(), created_by=admin["id"],
        )
        assert transition_state(db, inv, "validated", admin["id"]) is True
        assert inv.current_state == "validated"

    def test_transition_not_in_allowed_set_is_blocked(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        _seed_draft_state(db, tenant.id, {})
        inv = Invoice(
            tenant_id=tenant.id, current_state="draft", invoice_number="INV-1",
            vendor_name="V", invoice_date=date(2026, 1, 1), total_amount=100,
            vendor_id=uuid.uuid4(), created_by=admin["id"],
        )
        with pytest.raises(ValueError):
            transition_state(db, inv, "approved", admin["id"])  # not in allowed_transitions
