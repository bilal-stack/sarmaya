"""The control matrices are a view, not a second opinion.

Build Book, Global Matrices. These grids exist to show what the rules do *not*
cover — an amount band nothing matches, a rule that can never fire, a role
holding both halves of a separation. That is only worth drawing if the drawing
is true, and the way a control diagram goes wrong is not by erroring: it is by
slowly disagreeing with the engine while continuing to render.

So the load-bearing test here is TestTheMatrixAgreesWithTheEngine, which asks
policy.evaluate_approval_role — the function that actually routes an invoice —
the same question at every amount the matrix reports on, and requires the same
answer. If somebody changes the routing rules and not the matrix, that fails.

The rest test the findings themselves: a gap is detected when one exists, an
unreachable rule is detected when one exists, and the SoD table describes the
code as it is rather than as its own docstrings describe it.
"""
import os
import uuid

import pytest

from app.core.enums import UserRole
from app.core.roles import ADMIN, ROLE_PERMISSIONS, has_permission
from app.models.policy import Policy
from app.services import sod
from app.services.matrices import (
    BARRIER_NONE, BARRIER_PERMISSIONS, BARRIER_RUNTIME, SOD_RULES,
    approval_matrix, sod_matrix,
)
from app.services.policy import evaluate_approval_role

pytestmark = pytest.mark.integration


def _policy(db, tenant_id, name, priority, threshold, operator, role):
    policy = Policy(
        id=uuid.uuid4(), tenant_id=tenant_id, policy_type="approval_limit",
        policy_name=name, priority=priority, is_active=True,
        rule_config={
            "amount_threshold": threshold,
            "operator": operator,
            "required_role": role,
        },
    )
    db.add(policy)
    db.flush()
    return policy


def _auditor(tenant):
    return {"id": str(uuid.uuid4()), "role": "auditor", "tenant_id": tenant.id}


@pytest.fixture
def two_band(db, tenant):
    """The shipped default: manager up to 250k, CFO above it."""
    _policy(db, tenant.id, "CFO over 250k", 100, 250_000, "greater_than", "cfo")
    _policy(db, tenant.id, "Manager up to 250k", 0, 0, "greater_equal", "manager")
    return tenant


class TestTheMatrixAgreesWithTheEngine:
    """The property that makes this a view rather than a reimplementation."""

    def test_every_band_matches_what_actually_routes(self, db, two_band):
        matrix = approval_matrix(db, _auditor(two_band))

        for band in matrix["bands"]:
            engine = evaluate_approval_role(db, two_band.id, band["amount"])
            assert band["required_role"] == engine, (
                f"at {band['amount']} the matrix says {band['required_role']} "
                f"and the engine routes to {engine}"
            )

    def test_it_still_agrees_when_the_rules_are_unusual(self, db, tenant):
        """Overlapping rules where priority silently picks the winner — the
        case where a hand-drawn matrix and the engine part company."""
        _policy(db, tenant.id, "Low priority, wide", 1, 0, "greater_equal", "manager")
        _policy(db, tenant.id, "High priority, wide", 50, 0, "greater_equal", "cfo")
        _policy(db, tenant.id, "Middle", 10, 100, "greater_than", "admin")

        matrix = approval_matrix(db, _auditor(tenant))

        for band in matrix["bands"]:
            assert band["required_role"] == evaluate_approval_role(
                db, tenant.id, band["amount"]
            )

    def test_a_gap_is_where_the_engine_uses_its_hardcoded_fallback(self, db, tenant):
        """`falls_back` is a claim about the engine, so check the engine."""
        _policy(db, tenant.id, "CFO over 250k", 100, 250_000, "greater_than", "cfo")

        matrix = approval_matrix(db, _auditor(tenant))
        gaps = matrix["gaps"]

        assert gaps, "an amount below 250k matches nothing and must be reported"
        for band in gaps:
            # Nothing matched, so the engine returns the split written in
            # policy.py rather than anything an admin configured.
            expected = "manager" if band["amount"] <= 250_000 else "cfo"
            assert evaluate_approval_role(db, tenant.id, band["amount"]) == expected


class TestFindingTheHoles:
    def test_the_shipped_defaults_leave_no_gap(self, db, two_band):
        """The pair covers 0 upwards, so a correctly configured tenant is
        clean. A matrix that reported a hole here would cry wolf."""
        assert approval_matrix(db, _auditor(two_band))["gaps"] == []

    def test_a_band_nobody_configured_is_reported(self, db, tenant):
        _policy(db, tenant.id, "CFO over 250k", 100, 250_000, "greater_than", "cfo")

        matrix = approval_matrix(db, _auditor(tenant))

        assert any(b["amount"] < 250_000 for b in matrix["gaps"])
        assert all(b["required_role"] is None for b in matrix["gaps"])

    def test_a_rule_that_can_never_fire_is_reported(self, db, tenant):
        """Somebody wrote a control and it does nothing: a wide rule at higher
        priority matches first at every amount the lower one would have."""
        _policy(db, tenant.id, "Catch-all", 100, 0, "greater_equal", "cfo")
        _policy(db, tenant.id, "Shadowed", 10, 500, "greater_than", "manager")

        matrix = approval_matrix(db, _auditor(tenant))

        assert matrix["unreachable_rules"] == ["Shadowed"]

    def test_a_reachable_rule_is_not_reported(self, db, two_band):
        assert approval_matrix(db, _auditor(two_band))["unreachable_rules"] == []

    def test_no_policies_at_all_is_reported_as_entirely_uncovered(self, db, tenant):
        """A tenant that was never configured routes everything by the
        fallback. Silence here would read as 'configured and fine'."""
        matrix = approval_matrix(db, _auditor(tenant))

        assert matrix["rules"] == []
        assert matrix["gaps"] == matrix["bands"]

    def test_the_missing_category_axis_is_stated_not_omitted(self, db, two_band):
        """The Build Book asks for role x amount x category. rule_config has no
        category, so the axis does not exist — and a matrix that quietly drew
        two axes would read as though the third had been considered."""
        assert approval_matrix(db, _auditor(two_band))["category_axis"] is None


class TestTheSoDTableDescribesTheCode:
    """A hand-written table drifts from the code it describes. These are the
    assertions that make it drift loudly instead of silently."""

    def test_every_rule_names_a_file_that_enforces_it(self):
        for rule in SOD_RULES:
            path = rule["enforced_at"]
            assert os.path.exists(path), f"{rule['rule']} points at {path}"
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            assert "sod." in source, (
                f"{rule['rule']} claims {path} enforces it, but that file "
                f"never calls into app/services/sod.py"
            )

    def test_every_permission_is_a_real_permission(self):
        """A typo would silently classify a separation as structural, because
        no role holds a permission that does not exist."""
        known = {p for perms in ROLE_PERMISSIONS.values() for p in perms}
        for rule in SOD_RULES:
            for half in ("first_permission", "second_permission"):
                perm = rule[half]
                if perm is not None:
                    assert perm in known, f"{rule['rule']}.{half} = {perm!r}"

    def test_the_admin_exemption_flags_match_what_the_rules_do(self):
        """The claim worth checking against behaviour rather than docstrings,
        because it is the one that decides whether a row reads 'nothing
        prevents this'."""
        admin = {"id": str(uuid.uuid4()), "role": ADMIN}

        class _Record:
            created_by = admin["id"]

        exempting = {
            "self_invoice_approval": sod.violates_self_invoice_approval,
            "self_requisition_approval": sod.violates_self_approval,
            "self_po_approval": sod.violates_self_approval,
            "self_vendor_activation": sod.violates_self_vendor_activation,
        }
        for name, check in exempting.items():
            declared = next(r for r in SOD_RULES if r["rule"] == name)["admin_exempt"]
            # Exempt means the rule declines to fire on an admin acting on
            # their own record.
            assert check(_Record(), admin) is False
            assert declared is True

        # And the money-path rules, which exempt nobody.
        assert sod.violates_self_release(admin["id"], admin) is True
        assert sod.violates_self_reconciliation(admin["id"], admin) is True
        assert sod.violates_self_bank_change_approval(admin["id"], admin) is True
        for name in ("self_release", "self_reconciliation",
                     "self_bank_change_approval"):
            declared = next(r for r in SOD_RULES if r["rule"] == name)["admin_exempt"]
            assert declared is False

    def test_rule_names_are_unique(self):
        names = [r["rule"] for r in SOD_RULES]
        assert len(names) == len(set(names))


class TestClassifyingTheBarriers:
    def test_the_two_ordinary_role_findings_partition_every_rule(self, tenant):
        """Either some ordinary role holds both halves or none does. Nothing
        may fall between, and nothing may be counted twice."""
        matrix = sod_matrix(_auditor(tenant))

        resting = {r["rule"] for r in matrix["depends_on_the_runtime_check"]}
        structural = set(matrix["separated_by_permissions"])

        assert resting | structural == {r["rule"] for r in SOD_RULES}
        assert resting & structural == set()

    def test_the_admin_finding_overlaps_the_others_rather_than_partitioning(
        self, tenant
    ):
        """The bug this replaced an assertion for. The admin exemption and an
        ordinary role holding both halves are different facts about different
        people, so a rule can be in both lists — a requisition is waived for
        an admin *and* rests on the check for a manager. Treating the three
        lists as one classification undercounted the list somebody would act
        on, and put 'nothing prevents it' on a row where, for the role named
        beside it, something does."""
        matrix = sod_matrix(_auditor(tenant))

        resting = {r["rule"] for r in matrix["depends_on_the_runtime_check"]}

        assert "self_requisition_approval" in matrix["unblocked_for_admin"]
        assert "self_requisition_approval" in resting

        row = next(
            r for r in matrix["rules"] if r["rule"] == "self_requisition_approval"
        )
        assert row["roles_with_no_barrier"] == [ADMIN]
        assert row["ordinary_roles_holding_both"] == ["manager"]

    def test_an_exempting_rule_is_reported_as_unblocked(self, tenant):
        """Admin holds every permission and the invoice rule waives itself, so
        one person can raise and approve their own invoice with no control in
        the way. Deliberate — but it should be visible, not inferred from two
        files read together."""
        matrix = sod_matrix(_auditor(tenant))

        assert "self_invoice_approval" in matrix["unblocked_for_admin"]
        row = next(r for r in matrix["rules"] if r["rule"] == "self_invoice_approval")
        assert row["roles_with_no_barrier"] == [ADMIN]
        assert row["weakest_barrier"] == BARRIER_NONE

    def test_a_money_path_rule_is_not_reported_as_unblocked(self, tenant):
        """The release rule has no exemption, so even an admin is stopped."""
        matrix = sod_matrix(_auditor(tenant))

        assert "self_release" not in matrix["unblocked_for_admin"]
        row = next(r for r in matrix["rules"] if r["rule"] == "self_release")
        assert row["roles_with_no_barrier"] == []

    def test_an_ordinary_role_holding_both_halves_is_named(self, tenant):
        """The actionable finding: manager can both request and approve a
        vendor bank change, so the runtime check is the entire separation."""
        matrix = sod_matrix(_auditor(tenant))

        row = next(
            r for r in matrix["rules"] if r["rule"] == "self_bank_change_approval"
        )
        assert "manager" in row["ordinary_roles_holding_both"]
        assert row["weakest_barrier"] == BARRIER_RUNTIME
        assert has_permission("manager", row["first_permission"])
        assert has_permission("manager", row["second_permission"])

    def test_a_structurally_separated_rule_names_nobody(self, tenant):
        """Nobody but admin holds both prepare and release, so the permission
        model separates them whether or not the check fires."""
        matrix = sod_matrix(_auditor(tenant))

        row = next(r for r in matrix["rules"] if r["rule"] == "self_release")
        assert row["ordinary_roles_holding_both"] == []
        assert row["weakest_barrier"] == BARRIER_PERMISSIONS

    def test_admin_is_stated_once_rather_than_on_every_row(self, tenant):
        """It is true of all seventeen rows, so repeating it would bury the
        rows where an ordinary role holds both."""
        matrix = sod_matrix(_auditor(tenant))

        assert matrix["admin_holds_every_permission"] is True
        assert all(ADMIN in r["roles_holding_both"] for r in matrix["rules"])
        assert all(
            ADMIN not in r["ordinary_roles_holding_both"] for r in matrix["rules"]
        )

    def test_an_identity_half_has_no_permission_to_draw(self, tenant):
        """Being the employee a claim is for is not a permission. Inventing one
        would put a role axis on a row that has none."""
        row = next(r for r in SOD_RULES if r["rule"] == "own_expense_approval")

        assert row["first_permission"] is None


class TestPermissions:
    def test_a_clerk_cannot_read_either_matrix(self, db, tenant):
        clerk = {"id": str(uuid.uuid4()), "role": "ap_clerk", "tenant_id": tenant.id}

        with pytest.raises(PermissionError):
            approval_matrix(db, clerk)
        with pytest.raises(PermissionError):
            sod_matrix(clerk)

    def test_an_auditor_can(self, db, two_band):
        auditor = _auditor(two_band)

        assert approval_matrix(db, auditor)["rules"]
        assert sod_matrix(auditor)["rules"]

    def test_the_api_refuses_a_clerk(self, client, as_user, make_user):
        as_user(make_user(UserRole.AP_CLERK))

        assert client.get("/api/v1/matrices/approval").status_code == 403
        assert client.get("/api/v1/matrices/sod").status_code == 403

    def test_the_api_serves_an_auditor(self, client, as_user, make_user):
        as_user(make_user(UserRole.AUDITOR))

        assert client.get("/api/v1/matrices/approval").status_code == 200
        body = client.get("/api/v1/matrices/sod").json()
        assert len(body["rules"]) == len(SOD_RULES)
