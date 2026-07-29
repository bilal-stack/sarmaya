"""Integration tests for the Policy Simulator.

Build Book: simulate a rule change against historical data before turning it on.
The properties that matter: it must agree with the live routing engine, and it
must change nothing.
"""
import uuid
from datetime import date, timedelta

import pytest

from app.core.enums import UserRole, VendorStatus
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.policy import Policy
from app.schemas.policy import ApprovalPolicyCreate, ApprovalRule
from app.services.policy_service import ApprovalPolicyService
from app.services.policy_simulator import PolicySimulator
from app.services.policy import explain_approval_routing
from app.utils.datetime_helpers import utc_now

pytestmark = pytest.mark.integration


def _rule(name, threshold, role, priority=100, operator="greater_than"):
    return {"policy_name": name, "priority": priority,
            "rule_config": {"amount_threshold": threshold, "operator": operator,
                            "required_role": role}}


def _matrix(cfo_above):
    """A complete proposed matrix: CFO above a threshold, manager otherwise.

    A partial proposal (one rule, no catch-all) silently falls through to the
    hardcoded default split, so realistic simulations always include the
    catch-all — as the seeded defaults do.
    """
    return [
        _rule(f"CFO over {cfo_above}", cfo_above, "cfo", priority=100),
        _rule("Manager otherwise", 0, "manager", priority=0, operator="greater_equal"),
    ]


def _invoice(db, tenant_id, user_id, amount, vendor=None, days_ago=1, duplicate_of=None):
    inv = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="V", vendor_id=vendor.id if vendor else None,
        invoice_date=date(2026, 1, 1), total_amount=amount,
        current_state="pending_approval", created_by=user_id,
        potential_duplicate_id=duplicate_of,
    )
    db.add(inv)
    db.flush()
    # created_at has a server default; age it for window tests.
    inv.created_at = (utc_now() - timedelta(days=days_ago)).replace(tzinfo=None)
    db.flush()
    return inv


def _vendor(db, tenant_id, user_id, status=VendorStatus.ACTIVE):
    v = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, legal_name=f"V-{uuid.uuid4().hex[:6]}",
               status=status, created_by=user_id)
    db.add(v)
    db.flush()
    return v


@pytest.fixture
def seeded(db, tenant, make_user):
    """Live matrix: CFO above 250k. Invoices at 100k, 300k, 600k."""
    admin = make_user(UserRole.ADMIN)
    ApprovalPolicyService(db).create_policy(
        ApprovalPolicyCreate(
            policy_name="CFO over 250k",
            rule=ApprovalRule(amount_threshold=250_000, operator="greater_than",
                              required_role="cfo"),
            priority=100,
        ),
        admin,
    )
    v = _vendor(db, tenant.id, admin["id"])
    for amount in (100_000, 300_000, 600_000):
        _invoice(db, tenant.id, admin["id"], amount, vendor=v)
    return admin


class TestRoutingShift:
    def test_raising_the_threshold_moves_work_to_manager(self, db, seeded):
        # Proposed: CFO only above 500k -> the 300k invoice moves to manager.
        result = PolicySimulator(db).simulate(
            seeded, _matrix(500_000), window_days=90
        )
        assert result["invoices_evaluated"] == 3
        assert result["changed_count"] == 1
        moved = result["changes"][0]
        assert moved["amount"] == 300_000
        assert moved["from_role"] == "cfo" and moved["to_role"] == "manager"
        assert result["net_by_role"]["manager"] == 1
        assert result["net_by_role"]["cfo"] == -1

    def test_lowering_the_threshold_moves_work_to_cfo(self, db, seeded):
        result = PolicySimulator(db).simulate(
            seeded, _matrix(50_000), window_days=90
        )
        assert result["changed_count"] == 1
        assert result["changes"][0]["to_role"] == "cfo"
        assert result["net_by_role"]["cfo"] == 1

    def test_identical_rules_produce_no_change(self, db, seeded):
        result = PolicySimulator(db).simulate(
            seeded, _matrix(250_000), window_days=90
        )
        assert result["changed_count"] == 0
        assert result["routing_before"] == result["routing_after"]

    def test_value_totals_follow_the_routing(self, db, seeded):
        result = PolicySimulator(db).simulate(
            seeded, _matrix(500_000), window_days=90
        )
        # Before: manager 100k; cfo 300k+600k. After: manager 100k+300k; cfo 600k.
        assert result["routing_before"]["value"]["cfo"] == 900_000
        assert result["routing_after"]["value"]["cfo"] == 600_000
        assert result["routing_after"]["value"]["manager"] == 400_000
        assert result["changed_value"] == 300_000


class TestAgreementWithLiveEngine:
    def test_simulating_the_current_matrix_reproduces_live_routing(self, db, tenant, seeded):
        """A simulation that disagreed with production would be worse than none."""
        current = _matrix(250_000)
        result = PolicySimulator(db).simulate(seeded, current, window_days=90)
        assert result["changed_count"] == 0
        for amount in (100_000, 300_000, 600_000):
            live = explain_approval_routing(db, tenant.id, amount)["required_role"]
            simulated = "cfo" if amount > 250_000 else "manager"
            assert live == simulated


class TestWindowAndSafety:
    def test_window_excludes_older_invoices(self, db, tenant, seeded):
        _invoice(db, tenant.id, seeded["id"], 900_000, days_ago=200)
        recent = PolicySimulator(db).simulate(seeded, [_rule("x", 250_000, "cfo")], window_days=30)
        wide = PolicySimulator(db).simulate(seeded, [_rule("x", 250_000, "cfo")], window_days=365)
        assert recent["invoices_evaluated"] == 3
        assert wide["invoices_evaluated"] == 4

    def test_simulation_writes_nothing(self, db, tenant, seeded):
        before_policies = db.query(Policy).count()
        before_invoices = db.query(Invoice).count()
        PolicySimulator(db).simulate(
            seeded, [_rule("Radical", 1, "cfo")], window_days=90
        )
        assert db.query(Policy).count() == before_policies
        assert db.query(Invoice).count() == before_invoices
        # The live rule is untouched.
        live = db.query(Policy).filter(Policy.policy_type == "approval_limit").first()
        assert live.rule_config["amount_threshold"] == 250_000

    def test_empty_proposal_falls_back_to_default_routing(self, db, seeded):
        result = PolicySimulator(db).simulate(seeded, [], window_days=90)
        # With no proposed rules the default 250k split applies — same as live.
        assert result["changed_count"] == 0

    def test_invalid_window_rejected(self, db, seeded):
        with pytest.raises(ValueError):
            PolicySimulator(db).simulate(seeded, [_rule("x", 1, "cfo")], window_days=0)

    def test_requires_policy_manage_permission(self, db, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            PolicySimulator(db).simulate(clerk, [_rule("x", 1, "cfo")], window_days=30)


class TestAutopilotWhatIf:
    def test_counts_clean_invoices_under_the_proposed_limit(self, db, tenant, seeded):
        result = PolicySimulator(db).simulate(
            seeded, [_rule("x", 250_000, "cfo")], window_days=90, autopilot_limit=150_000
        )
        # Only the 100k invoice is both clean and under the limit.
        assert result["autopilot_eligible"]["count"] == 1
        assert result["autopilot_eligible"]["limit"] == 150_000

    def test_flagged_duplicates_never_qualify(self, db, tenant, seeded):
        v = _vendor(db, tenant.id, seeded["id"])
        other = _invoice(db, tenant.id, seeded["id"], 10_000, vendor=v)
        _invoice(db, tenant.id, seeded["id"], 20_000, vendor=v, duplicate_of=other.id)
        result = PolicySimulator(db).simulate(
            seeded, [_rule("x", 250_000, "cfo")], window_days=90, autopilot_limit=1_000_000
        )
        # 3 seeded + the clean 10k one; the flagged duplicate is excluded.
        assert result["autopilot_eligible"]["count"] == 4

    def test_omitted_when_no_limit_supplied(self, db, seeded):
        result = PolicySimulator(db).simulate(seeded, [_rule("x", 1, "cfo")], window_days=90)
        assert result.get("autopilot_eligible") is None
