"""The workflow engine reads which state machine governs a record.

`transition_state` queried `workflow_type == "invoice"` unconditionally and
fell back to the hardcoded invoice transitions when nothing was configured, and
the SLA runner scanned only invoices. A second module would therefore have been
governed by the invoice state machine without anyone noticing — the wrong
transitions would simply have been allowed.

The workflow is now declared on the model as WORKFLOW_TYPE and read from the
record, so a module that forgets to declare one fails loudly instead of
inheriting invoice rules.
"""
import uuid

import pytest

from app.core.enums import UserRole, InvoiceState, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.workflow_state import WorkflowState
from app.services.workflow import (
    transition_state, workflow_type_of, workflow_models, get_allowed_transitions,
)

pytestmark = pytest.mark.integration


class _Undeclared:
    """A record whose model forgot to say which workflow governs it."""
    tenant_id = None
    current_state = "draft"


class TestWorkflowTypeIsDeclaredByTheModel:

    def test_the_invoice_declares_its_workflow(self):
        assert workflow_type_of(Invoice()) == "invoice"

    def test_an_undeclared_model_is_refused(self):
        """Silently defaulting to 'invoice' is what made this dangerous."""
        with pytest.raises(ValueError, match="WORKFLOW_TYPE"):
            workflow_type_of(_Undeclared())

    def test_the_registry_finds_declared_workflows(self):
        registry = workflow_models()
        assert registry.get("invoice") is Invoice


class TestUnconfiguredWorkflowsDoNotInheritInvoiceRules:

    def test_a_non_invoice_workflow_must_be_configured(self, db, tenant, make_user):
        """The fallback encodes the invoice state machine. Applying it to
        another module would allow transitions never designed for it."""
        class _Order:
            WORKFLOW_TYPE = "purchase_order"

            def __init__(self, tenant_id):
                self.tenant_id = tenant_id
                self.current_state = "draft"

        with pytest.raises(ValueError, match="No workflow configured"):
            transition_state(db, _Order(tenant.id), "validated", make_user(UserRole.ADMIN)["id"])

    def test_allowed_transitions_are_empty_for_an_unconfigured_workflow(self, db, tenant):
        assert get_allowed_transitions(db, tenant.id, "draft", "purchase_order") == []

    def test_invoices_still_get_their_fallback(self, db, tenant):
        """Invoices keep the legacy behaviour, so nothing regresses for the
        module that predates the configuration table."""
        assert "validated" in get_allowed_transitions(db, tenant.id, "draft", "invoice")


class TestConfiguredWorkflowsAreHonoured:

    def test_the_declared_workflow_is_the_one_applied(
        self, db, tenant, make_user, monkeypatch
    ):
        """A record is governed by the workflow it declares, not by its table.

        Uses a real mapped record with its declared type overridden, so the
        assertion is about what the engine reads rather than about a stand-in
        class. 'issued' exists only in the purchase_order machine, so the
        transition succeeding proves that machine was the one consulted.
        """
        db.add(WorkflowState(
            id=uuid.uuid4(), tenant_id=tenant.id,
            workflow_type="purchase_order", state_name="draft",
            display_name="Draft", state_order=1,
            allowed_transitions=["issued"],
        ))
        vendor = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
                        legal_name="WF Vendor", status=VendorStatus.ACTIVE)
        db.add(vendor)
        db.flush()
        record = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            vendor_name=vendor.legal_name, invoice_number=f"WF-{uuid.uuid4().hex[:5]}",
            invoice_date="2026-07-01", total_amount=100,
            current_state=InvoiceState.DRAFT,
            created_by=make_user(UserRole.ADMIN)["id"],
        )
        db.add(record)
        db.flush()

        monkeypatch.setattr(Invoice, "WORKFLOW_TYPE", "purchase_order")

        assert transition_state(db, record, "issued", make_user(UserRole.ADMIN)["id"]) is True
        assert record.current_state == "issued"

    def test_an_invoice_cannot_use_a_purchase_order_transition(self, db, tenant, make_user):
        db.add(WorkflowState(
            id=uuid.uuid4(), tenant_id=tenant.id,
            workflow_type="purchase_order", state_name="draft",
            display_name="Draft", state_order=1,
            allowed_transitions=["issued"],
        ))
        db.flush()

        vendor = Vendor(id=uuid.uuid4(), tenant_id=tenant.id,
                        legal_name="WF Vendor", status=VendorStatus.ACTIVE)
        db.add(vendor)
        db.flush()
        invoice = Invoice(
            id=uuid.uuid4(), tenant_id=tenant.id, vendor_id=vendor.id,
            vendor_name=vendor.legal_name, invoice_number=f"WF-{uuid.uuid4().hex[:5]}",
            invoice_date="2026-07-01", total_amount=100,
            current_state=InvoiceState.DRAFT,
            created_by=make_user(UserRole.ADMIN)["id"],
        )
        db.add(invoice)
        db.flush()

        with pytest.raises(ValueError, match="Invalid state transition"):
            transition_state(db, invoice, "issued", make_user(UserRole.ADMIN)["id"])
