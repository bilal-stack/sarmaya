"""Unit tests for the invoice workflow state machine (app/services/workflow.py).

Encodes the MVP spec's workflow:
    Draft -> Validated -> Pending Approval -> Approved -> Rejected -> Paid
with no state-skipping. Tests target the hardcoded fallback rules
(change_state / get_allowed_transitions), which apply when no per-tenant
workflow_states rows are configured. Pure logic, no real database.
"""
import pytest

from app.services.workflow import change_state, get_allowed_transitions
from app.core.enums import InvoiceState


class FakeInvoice:
    def __init__(self, state):
        self.current_state = state
        self.tenant_id = "t-1"


class _NullQuery:
    """Query stub whose .filter(...).first() always returns None,
    forcing get_allowed_transitions onto its hardcoded fallback."""
    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class FakeSession:
    def __init__(self):
        self.added = []

    def query(self, *a, **k):
        return _NullQuery()

    def add(self, obj):
        self.added.append(obj)


DRAFT = InvoiceState.DRAFT.value
VALIDATED = InvoiceState.VALIDATED.value
PENDING = InvoiceState.PENDING_APPROVAL.value
APPROVED = InvoiceState.APPROVED.value
REJECTED = InvoiceState.REJECTED.value
PAID = InvoiceState.PAID.value
CANCELLED = InvoiceState.CANCELLED.value


class TestChangeStateLegalTransitions:
    @pytest.mark.parametrize("current,target", [
        (DRAFT, VALIDATED),
        (DRAFT, CANCELLED),
        (VALIDATED, PENDING),
        (VALIDATED, CANCELLED),
        (PENDING, APPROVED),
        (PENDING, REJECTED),
        (APPROVED, PAID),
        (APPROVED, CANCELLED),
        (REJECTED, DRAFT),
    ])
    def test_allowed_transition_applies(self, current, target):
        inv = FakeInvoice(current)
        db = FakeSession()
        assert change_state(inv, target, db) is True
        assert inv.current_state == target
        assert inv in db.added


class TestChangeStateIllegalTransitions:
    @pytest.mark.parametrize("current,target", [
        (DRAFT, PENDING),       # must be validated first, no skipping
        (DRAFT, APPROVED),      # cannot skip straight to approved
        (DRAFT, PAID),          # cannot skip to paid
        (VALIDATED, APPROVED),  # must go through pending approval
        (PENDING, PAID),        # must be approved before paid
        (PENDING, DRAFT),       # no going back to draft from pending
        (APPROVED, REJECTED),   # cannot reject an approved invoice
        (REJECTED, APPROVED),   # rejected can only return to draft
    ])
    def test_illegal_transition_raises(self, current, target):
        inv = FakeInvoice(current)
        db = FakeSession()
        with pytest.raises(ValueError):
            change_state(inv, target, db)
        assert inv.current_state == current  # unchanged

    def test_terminal_paid_allows_nothing(self):
        for target in (DRAFT, APPROVED, CANCELLED, PENDING):
            with pytest.raises(ValueError):
                change_state(FakeInvoice(PAID), target, FakeSession())

    def test_terminal_cancelled_allows_nothing(self):
        for target in (DRAFT, APPROVED, PAID, PENDING):
            with pytest.raises(ValueError):
                change_state(FakeInvoice(CANCELLED), target, FakeSession())

    def test_case_insensitive_current_state(self):
        inv = FakeInvoice("DRAFT")
        assert change_state(inv, VALIDATED, FakeSession()) is True


class TestGetAllowedTransitionsFallback:
    def test_draft_transitions(self):
        result = get_allowed_transitions(FakeSession(), "t-1", DRAFT)
        assert set(result) == {VALIDATED, CANCELLED}

    def test_validated_transitions(self):
        result = get_allowed_transitions(FakeSession(), "t-1", VALIDATED)
        assert set(result) == {PENDING, CANCELLED}

    def test_pending_transitions(self):
        result = get_allowed_transitions(FakeSession(), "t-1", PENDING)
        assert set(result) == {APPROVED, REJECTED}

    def test_paid_is_terminal(self):
        assert get_allowed_transitions(FakeSession(), "t-1", PAID) == []

    def test_rejected_only_back_to_draft(self):
        assert get_allowed_transitions(FakeSession(), "t-1", REJECTED) == [DRAFT]

    def test_unknown_state_returns_empty(self):
        assert get_allowed_transitions(FakeSession(), "t-1", "bogus") == []
