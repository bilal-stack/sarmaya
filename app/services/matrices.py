"""The control matrices: the rules as a grid rather than as a list.

Build Book, Global Matrices. Everything here is a *view* of configuration and
code that already exists and is already enforced — no rule is defined in this
file and nothing here decides anything. That is deliberate: a matrix carrying
its own copy of the rules would eventually disagree with the engine enforcing
them, and a control diagram that lies is worse than no diagram.

**Why draw them, given the rules already work.** A list answers "what rules
exist". A matrix answers "what is *not* covered", which is the question a
control review actually asks. Three findings only appear in grid form:

  * an amount band no rule matches, so routing falls through to a split
    written in code that nobody configured and nobody can see from the policy
    screen;
  * a rule that can never fire because a higher-priority rule always matches
    first — somebody wrote a control and it does nothing;
  * a role holding both halves of a separation, where the runtime check is the
    only thing standing between one person and both actions.

None of those are visible reading the rules one at a time, which is how rules
are normally read.

Each matrix has its own finding, and the findings are the point rather than
the grids:

  * approval — a band nothing routes, and a rule that can never fire;
  * segregation of duties — how much actually stands in the way of each
    separation, which for four of them is nothing at all once the actor is an
    admin;
  * vendor risk — which factors the score does not look at;
  * evidence — whether a requirement binds every caller or only an HTTP route.

Four of the five Build Book matrices are here. Tolerance is not, and the reason
is the same one that governs the rest of this module: the matching engine
applies one pair of percentages to every line of every invoice, with no
category or vendor axis to draw. A grid over it would have to invent the axes
in the drawing, which is exactly the failure this file exists to avoid. That
absence is stated on the tolerance settings screen rather than left to be
inferred from a missing page.
"""
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.roles import (
    ADMIN, ROLE_PERMISSIONS, has_permission,
    PERM_ADJUST_INVENTORY, PERM_APPROVE_ADJUSTMENT, PERM_APPROVE_BANK_CHANGE,
    PERM_APPROVE_EXPENSE, PERM_APPROVE_HEADCOUNT, PERM_APPROVE_INVOICE,
    PERM_APPROVE_PAYROLL_CHANGE, PERM_APPROVE_PO, PERM_APPROVE_REQUISITION,
    PERM_APPROVE_RETURN, PERM_CLAIM_EXPENSE, PERM_CREATE_INVOICE,
    PERM_CREATE_PO, PERM_CREATE_REQUISITION, PERM_MANAGE_RETURNS,
    PERM_MANAGE_VENDORS, PERM_PREPARE_PAYMENT, PERM_RECONCILE_PAYMENT,
    PERM_RELEASE_PAYMENT, PERM_REQUEST_HEADCOUNT, PERM_REQUEST_PAYROLL_CHANGE,
    PERM_VIEW_AUDIT,
)
from app.services.policy import _operator_matches, active_approval_rules

#: Probe amounts are taken either side of every configured threshold, because
#: that is where a gap or an overlap actually lives. Not a claim that these are
#: the only amounts that matter.
_PROBE_OFFSETS = (-1, 0, 1)


def _require_audit(current_user: dict) -> None:
    """Read with audit.view.

    These describe the shape of the controls rather than any record, which is
    precisely what somebody looking for a way around them would want. The
    audience that reads the audit trail is the right audience for this.
    """
    if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
        raise PermissionError(
            f"Role '{current_user['role']}' cannot view the control matrices"
        )


# --- 1. Approval matrix ------------------------------------------------------

def approval_matrix(db: Session, current_user: dict) -> Dict:
    """Which role must approve at which amount, and where nothing decides.

    Rules evaluate highest priority first and the first match wins, so the grid
    is built by probing amounts around each configured threshold and recording
    which rule actually fires. Probing rather than reading the rules in order
    is the point — it is the only way to see a rule that is never reached.
    """
    _require_audit(current_user)
    rules = active_approval_rules(db, current_user["tenant_id"])

    prepared = []
    for rule in rules:
        config = rule["rule_config"] or {}
        prepared.append({
            "policy_name": rule["policy_name"],
            "threshold": float(config.get("amount_threshold", 0) or 0),
            "operator": config.get("operator", "greater_equal"),
            "required_role": config.get("required_role", "manager"),
        })

    def first_match(amount: float) -> Optional[dict]:
        for rule in prepared:
            if _operator_matches(rule["operator"], amount, rule["threshold"]):
                return rule
        return None

    probes = sorted(
        {max(0.0, r["threshold"] + off) for r in prepared for off in _PROBE_OFFSETS}
        | {0.0, 1.0}
    )

    bands, fired = [], set()
    for amount in probes:
        match = first_match(amount)
        if match:
            fired.add(match["policy_name"])
        bands.append({
            "amount": amount,
            "required_role": match["required_role"] if match else None,
            "decided_by": match["policy_name"] if match else None,
            # Routing does not fail here — evaluate_approval_role falls back to
            # a threshold hardcoded in policy.py. That is a real decision, made
            # by nobody, invisible on the policy screen.
            "falls_back": match is None,
        })

    return {
        "rules": prepared,
        "bands": bands,
        "gaps": [b for b in bands if b["falls_back"]],
        # Active, configured, and decides nothing — always because something
        # above it matches first.
        "unreachable_rules": [
            r["policy_name"] for r in prepared if r["policy_name"] not in fired
        ],
        "roles_used": sorted({r["required_role"] for r in prepared}),
        #: Stated rather than quietly omitted. The Build Book asks for
        #: role x amount x category; rule_config carries no category, so that
        #: axis does not exist in the rules. Drawing it would put a control on
        #: a diagram that nothing enforces.
        "category_axis": None,
    }


# --- 2. Segregation-of-duties matrix -----------------------------------------

#: Every separation this system enforces, with the permission each half needs.
#: Compiled from the call sites, not from the rule functions: about half of
#: these are enforced by calling sod._same_person directly rather than through
#: a named violates_* helper, and a matrix listing only the named ones would
#: show seven controls where sixteen are running. A meta-test asserts every
#: `enforced_at` here is a real file and line that calls into app/services/sod.
#:
#: `first_permission = None` means the first half is not a permission at all —
#: it is being the person the record is about. Those rows have no role axis to
#: draw, and saying so is more honest than inventing one.
SOD_RULES: List[Dict] = [
    {
        "rule": "self_invoice_approval",
        "control": "Nobody approves an invoice they raised",
        "first_action": "Raise an invoice",
        "first_permission": PERM_CREATE_INVOICE,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_INVOICE,
        "admin_exempt": True,
        "enforced_at": "app/services/invoice_service.py",
    },
    {
        "rule": "self_requisition_approval",
        "control": "Nobody approves a requisition they raised",
        "first_action": "Raise a requisition",
        "first_permission": PERM_CREATE_REQUISITION,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_REQUISITION,
        "admin_exempt": True,
        "enforced_at": "app/services/requisition_service.py",
    },
    {
        "rule": "self_po_approval",
        "control": "Nobody approves a purchase order they raised",
        "first_action": "Raise a purchase order",
        "first_permission": PERM_CREATE_PO,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_PO,
        "admin_exempt": True,
        "enforced_at": "app/services/purchase_order_service.py",
    },
    {
        "rule": "self_vendor_activation",
        "control": "Nobody activates a vendor they created",
        "first_action": "Create a vendor",
        "first_permission": PERM_MANAGE_VENDORS,
        "second_action": "Activate it",
        "second_permission": PERM_MANAGE_VENDORS,
        "admin_exempt": True,
        "enforced_at": "app/services/vendor_service.py",
    },
    {
        "rule": "self_release",
        "control": "Nobody releases a payment run they prepared",
        "first_action": "Prepare a payment run",
        "first_permission": PERM_PREPARE_PAYMENT,
        "second_action": "Release it",
        "second_permission": PERM_RELEASE_PAYMENT,
        "admin_exempt": False,
        "enforced_at": "app/services/payment_service.py",
    },
    {
        "rule": "self_reconciliation",
        "control": "Nobody confirms their own payment cleared the bank",
        "first_action": "Release a payment",
        "first_permission": PERM_RELEASE_PAYMENT,
        "second_action": "Reconcile it against a statement",
        "second_permission": PERM_RECONCILE_PAYMENT,
        "admin_exempt": False,
        "enforced_at": "app/services/reconciliation.py",
    },
    {
        "rule": "self_bank_change_approval",
        "control": "Nobody approves a vendor bank change they requested",
        "first_action": "Request a bank change",
        "first_permission": PERM_MANAGE_VENDORS,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_BANK_CHANGE,
        "admin_exempt": False,
        "enforced_at": "app/services/vendor_bank_service.py",
    },
    {
        "rule": "first_payment_after_bank_change",
        "control": (
            "Whoever changed a vendor's bank details cannot release the first "
            "payment that uses them"
        ),
        "first_action": "Request or apply a vendor bank change",
        "first_permission": PERM_MANAGE_VENDORS,
        "second_action": "Release the next payment to that vendor",
        "second_permission": PERM_RELEASE_PAYMENT,
        "admin_exempt": False,
        "enforced_at": "app/services/payment_service.py",
    },
    {
        "rule": "self_expense_approval",
        "control": "Nobody approves an expense claim they entered",
        "first_action": "Enter an expense claim",
        "first_permission": PERM_CLAIM_EXPENSE,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_EXPENSE,
        "admin_exempt": False,
        "enforced_at": "app/services/expense_service.py",
    },
    {
        "rule": "own_expense_approval",
        "control": (
            "Nobody approves a claim that is theirs, even if somebody else "
            "entered it for them"
        ),
        "first_action": "Be the employee the claim is for",
        "first_permission": None,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_EXPENSE,
        "admin_exempt": False,
        "enforced_at": "app/services/expense_service.py",
    },
    {
        "rule": "self_headcount_approval",
        "control": "Nobody approves a headcount request they raised",
        "first_action": "Raise a headcount request",
        "first_permission": PERM_REQUEST_HEADCOUNT,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_HEADCOUNT,
        "admin_exempt": False,
        "enforced_at": "app/services/headcount_service.py",
    },
    {
        "rule": "self_adjustment_approval",
        "control": "Nobody approves a stock adjustment they raised",
        "first_action": "Raise a stock adjustment",
        "first_permission": PERM_ADJUST_INVENTORY,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_ADJUSTMENT,
        "admin_exempt": False,
        "enforced_at": "app/services/inventory_adjustment_service.py",
    },
    {
        "rule": "dual_adjustment_approval",
        "control": (
            "The second signature on a large write-off must come from a "
            "different person than the first"
        ),
        "first_action": "Give the first approval",
        "first_permission": PERM_APPROVE_ADJUSTMENT,
        "second_action": "Give the second approval",
        "second_permission": PERM_APPROVE_ADJUSTMENT,
        "admin_exempt": False,
        "enforced_at": "app/services/inventory_adjustment_service.py",
    },
    {
        "rule": "self_return_approval",
        "control": "Nobody approves a vendor return they raised",
        "first_action": "Raise a vendor return",
        "first_permission": PERM_MANAGE_RETURNS,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_RETURN,
        "admin_exempt": False,
        "enforced_at": "app/services/vendor_return_service.py",
    },
    {
        "rule": "self_payroll_approval",
        "control": "Nobody approves a pay change they raised",
        "first_action": "Raise a payroll change",
        "first_permission": PERM_REQUEST_PAYROLL_CHANGE,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_PAYROLL_CHANGE,
        "admin_exempt": False,
        "enforced_at": "app/services/payroll_change_service.py",
    },
    {
        "rule": "own_payroll_approval",
        "control": "Nobody approves a pay change that is for them",
        "first_action": "Be the employee the change is for",
        "first_permission": None,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_PAYROLL_CHANGE,
        "admin_exempt": False,
        "enforced_at": "app/services/payroll_change_service.py",
    },
    {
        "rule": "manager_payroll_approval",
        "control": (
            "Nobody approves a pay change for their own manager, which would "
            "let two people sign each other's rises"
        ),
        "first_action": "Be the approver's own manager",
        "first_permission": None,
        "second_action": "Approve it",
        "second_permission": PERM_APPROVE_PAYROLL_CHANGE,
        "admin_exempt": False,
        "enforced_at": "app/services/payroll_change_service.py",
    },
]

#: How much stands between one person and both halves of a separation.
#: Ordered weakest first.
BARRIER_NONE = "none"                 # holds both, and the rule waives itself
BARRIER_RUNTIME = "runtime_check"     # holds both; only the check stops them
BARRIER_PERMISSIONS = "permissions"   # cannot hold both; structural


def sod_matrix(current_user: dict) -> Dict:
    """Every separation, and how much actually stands in the way of each.

    The classification is the finding, and it comes out of one asymmetry:
    `has_permission` returns True for admin unconditionally, so admin holds
    both halves of every separation here. For most of them the runtime check
    still fires and that is fine. For the four that exempt admins, it does not
    — an admin can raise and approve their own invoice, requisition or purchase
    order, and activate their own vendor.

    That carve-out is deliberate (sod.py explains it: a one-person demo tenant
    has to function) and it is bounded — a wrongly approved invoice still meets
    every downstream control. But "deliberate" and "invisible" are different
    things, and a control review is entitled to see it stated rather than
    inferred from two files that have to be read together.

    A role separated by permissions is in the stronger position, because that
    separation does not depend on a check firing at the right moment.
    """
    _require_audit(current_user)

    roles = sorted(ROLE_PERMISSIONS)
    rows = []
    for rule in SOD_RULES:
        first_perm, second_perm = rule["first_permission"], rule["second_permission"]
        both, first_only, second_only = [], [], []
        for role in roles:
            # None means the first half is an identity, not a permission —
            # anyone can be the subject of a claim or a pay change, so every
            # role that can approve holds both halves by definition.
            can_first = True if first_perm is None else has_permission(role, first_perm)
            can_second = has_permission(role, second_perm)
            if can_first and can_second:
                both.append(role)
            elif can_first:
                first_only.append(role)
            elif can_second:
                second_only.append(role)

        # Admin is set aside here, not ignored: has_permission returns True
        # for admin unconditionally, so admin holds both halves of all
        # seventeen rows. Repeating that on every row would bury the rows
        # where an *ordinary* role holds both, which is the finding somebody
        # can act on. The admin fact is stated once, below.
        ordinary_both = [r for r in both if r != ADMIN]
        exempt = [ADMIN] if rule["admin_exempt"] else []

        rows.append({
            **rule,
            "roles_holding_both": both,
            "roles_first_only": first_only,
            "roles_second_only": second_only,
            # Roles for which nothing at all prevents the conflict: they hold
            # both permissions and the rule declines to apply to them.
            "roles_with_no_barrier": exempt,
            # Roles the rule does apply to, who hold both halves anyway. For
            # them the runtime check is the entire separation.
            "ordinary_roles_holding_both": ordinary_both,
            # Worst case on the row, for a single badge. Deliberately NOT a
            # classification of the row as a whole: a rule can be exempt for
            # an admin *and* rest on the check for a manager, and reading one
            # label as covering both is the misreading this comment exists to
            # stop. The two role lists above are the truth; this is a headline.
            "weakest_barrier": (
                BARRIER_NONE if exempt
                else BARRIER_RUNTIME if ordinary_both
                else BARRIER_PERMISSIONS
            ),
        })

    return {
        "roles": roles,
        "rules": rows,
        #: Stated once rather than on every row. It is true, it is why admin
        #: appears in every roles_holding_both, and it is the reason the
        #: exemptions below matter more than they would otherwise.
        "admin_holds_every_permission": True,
        # These three are deliberately NOT a partition. The first is about
        # admin and the other two are about everybody else, so a rule can and
        # does appear in the first and one of the others — self_requisition
        # approval is waived for an admin *and* rests on the check for a
        # manager. Making them mutually exclusive undercounted the second
        # list, which is the one somebody would act on.
        #
        # Admin holds both halves and the rule waives itself: one person can
        # do both with no control in the way at all.
        "unblocked_for_admin": [
            r["rule"] for r in rows if r["roles_with_no_barrier"]
        ],
        # An ordinary role holds both halves. The runtime check is that role's
        # entire separation — it has to fire, on every path to the action.
        "depends_on_the_runtime_check": [
            {"rule": r["rule"], "roles": r["ordinary_roles_holding_both"]}
            for r in rows if r["ordinary_roles_holding_both"]
        ],
        # No ordinary role can hold both, so the permission model separates
        # them whether or not the check fires. The stronger position.
        "separated_by_permissions": [
            r["rule"] for r in rows if not r["ordinary_roles_holding_both"]
        ],
    }


# --- 3. Vendor risk matrix ---------------------------------------------------

def vendor_risk_matrix(db: Session, current_user: dict) -> Dict:
    """Tier against factor: which vendors sit where, and what put them there.

    The third of the Build Book's five, and for most of this project it was
    unbuildable for a specific reason worth keeping on the record: `risk_score`
    was an Integer column that nothing ever assigned, so a matrix over it would
    have had to invent both the scoring rule and the bands. That is the failure
    this module exists to avoid, and it is why this arrived last.

    It is buildable now because `vendor_risk.py` computes the score from
    measured facts and returns the factors that produced it. So the grid has
    two real axes — tier down the side, factor across — and the cells are
    counts of vendors, not estimates.

    What the grid shows that the vendor list does not: **which factor is
    carrying each tier**. Ten high-risk vendors that are all high for the same
    reason is one problem with one fix. Ten that are high for ten different
    reasons is ten problems. A sorted list of scores cannot tell those apart.
    """
    _require_audit(current_user)

    from app.models.vendor import Vendor
    from app.services.vendor_risk import TIERS, VendorRiskService

    service = VendorRiskService(db)
    scored = [service.score(v) for v in db.query(Vendor).all()]

    tier_names = [name for _floor, name in TIERS]
    # Every factor the scorer can emit, taken from what it actually produced
    # rather than from a hardcoded list that could drift away from it.
    factor_codes = sorted({
        f["code"] for result in scored for f in result["factors"]
    })

    cells: Dict[str, Dict[str, int]] = {
        tier: {code: 0 for code in factor_codes} for tier in tier_names
    }
    totals = {tier: 0 for tier in tier_names}
    for result in scored:
        tier = result["tier"]
        totals[tier] += 1
        for factor in result["factors"]:
            cells[tier][factor["code"]] += 1

    # A factor nothing currently trips. Reported rather than omitted: a control
    # that never fires is either watching something that does not happen or is
    # not watching, and a grid that hides the empty column cannot show which.
    never_seen = [
        code for code in factor_codes
        if sum(cells[t][code] for t in tier_names) == 0
    ]

    return {
        "tiers": tier_names,
        "factors": factor_codes,
        "cells": cells,
        "vendors_per_tier": totals,
        "vendor_count": len(scored),
        "never_triggered": never_seen,
        #: The same disclosure vendor_risk.py makes, carried up so a reader of
        #: the grid is told what the grid does not cover.
        "unscored_dimensions": (
            scored[0]["unscored_dimensions"] if scored else []
        ),
    }
# --- 4. Evidence matrix ------------------------------------------------------

#: Where a requirement is enforced, which is what decides who it binds.
#: Ordered weakest first.
#:
#: The distinction is not pedantry. A rule in a request schema refuses an HTTP
#: caller and nothing else: another service, a script, a scheduled job or a
#: test calls the method directly and the schema is never constructed. A rule
#: in the service binds every caller there is.
LAYER_SCHEMA = "api_schema"
LAYER_SERVICE = "service"

#: The moment evidence is demanded. A rule can sit on more than one gate, and
#: the expense receipt does, which is the reason this is an axis rather than a
#: field.
GATE_RAISE = "raise"
GATE_SUBMIT = "submit"
GATE_APPROVE = "approve"
GATE_REJECT = "reject"
GATE_RECORD = "record"
GATE_FILL = "fill"
GATE_SKIP = "skip"

#: Every requirement in the system that exists to make somebody produce
#: something, compiled from the call sites that enforce it — the same way
#: SOD_RULES is, and for the same reason: read one at a time these look like
#: ordinary input validation, and the shape of the control only appears when
#: they are next to each other.
#:
#: What is deliberately NOT here: required fields that are not evidence. A
#: headcount request needs an annual cost and a job title, and an adjustment
#: needs at least one line, but those are the record being complete rather
#: than somebody being made to show their working. Including them would pad
#: the grid and make the rows that are controls harder to find.
EVIDENCE_RULES: List[Dict] = [
    {
        "rule": "rejection_reason",
        "artifact": "A written reason, non-blank after stripping",
        "required_when": "Any rejection, unconditionally",
        "gates": [GATE_REJECT],
        "waiver": None,
        # Ten workflows, one control, two depths. This row is the reason the
        # matrix exists: every one of these refuses a blank reason over HTTP,
        # so nothing here is a hole in the API — but only six of them refuse
        # one when the method is called directly.
        "workflows": [
            {"workflow": "invoice", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/invoice_service.py"},
            {"workflow": "expense_claim", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/expense_service.py"},
            {"workflow": "headcount_request", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/headcount_service.py"},
            {"workflow": "inventory_adjustment", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/inventory_adjustment_service.py"},
            {"workflow": "payroll_change", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/payroll_change_service.py"},
            {"workflow": "vendor_return", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/vendor_return_service.py"},
            {"workflow": "payment", "layer": LAYER_SCHEMA,
             "enforced_at": "app/schemas/payment.py"},
            {"workflow": "purchase_order", "layer": LAYER_SCHEMA,
             "enforced_at": "app/schemas/purchase_order.py"},
            {"workflow": "requisition", "layer": LAYER_SCHEMA,
             "enforced_at": "app/schemas/requisition.py"},
            {"workflow": "vendor_bank_change", "layer": LAYER_SCHEMA,
             "enforced_at": "app/schemas/vendor_bank_change.py"},
        ],
    },
    {
        "rule": "expense_receipt",
        "artifact": "A file attached to the claim",
        "required_when": (
            "The category is one of travel, accommodation, entertainment or "
            "equipment, or the claim totals more than 1000"
        ),
        # The only rule demanded twice. Refused at submit so the claimant
        # finds out while they still have the receipt, and again at approve so
        # attaching nothing and waiting does not work.
        "gates": [GATE_SUBMIT, GATE_APPROVE],
        "waiver": {
            "at": GATE_APPROVE,
            "needs": "A written reason",
            "recorded_as": (
                "policy_override_reason on the claim, the comment on the "
                "audit row, and policy_override in its after_value"
            ),
            "held_by": PERM_APPROVE_EXPENSE,
        },
        "workflows": [
            {"workflow": "expense_claim", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/expense_service.py"},
        ],
    },
    {
        "rule": "invoice_duplicate_override_reason",
        "artifact": "A written reason",
        "required_when": (
            "The invoice carries an unacknowledged potential-duplicate flag"
        ),
        "gates": [GATE_APPROVE],
        "waiver": None,
        "workflows": [
            {"workflow": "invoice", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/invoice_service.py"},
        ],
    },
    {
        "rule": "headcount_background_check",
        "artifact": "A cleared background check on the employee record",
        "required_when": "The request was raised as a sensitive role",
        # Checked when somebody is placed in the role, which is the first
        # point at which the question has an answer.
        "gates": [GATE_FILL],
        "waiver": None,
        "workflows": [
            {"workflow": "headcount_request", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/headcount_service.py"},
        ],
    },
    {
        "rule": "quality_rejection_reason_code",
        "artifact": "A reason code from a closed set",
        "required_when": "A quality check rejects any quantity at all",
        "gates": [GATE_RECORD],
        "waiver": None,
        "workflows": [
            {"workflow": "quality_check", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/quality_check_service.py"},
        ],
    },
    {
        "rule": "quality_rejection_note",
        "artifact": "Free text describing what was wrong",
        "required_when": "A quality check rejects any quantity at all",
        # Separate from the code on purpose: the code says the category and
        # the note is the evidence. One does not substitute for the other.
        "gates": [GATE_RECORD],
        "waiver": None,
        "workflows": [
            {"workflow": "quality_check", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/quality_check_service.py"},
        ],
    },
    {
        "rule": "inventory_adjustment_reason_code",
        "artifact": "A reason code from a closed set",
        "required_when": "Every adjustment, at the point it is raised",
        "gates": [GATE_RAISE],
        "waiver": None,
        "workflows": [
            {"workflow": "inventory_adjustment", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/inventory_adjustment_service.py"},
        ],
    },
    {
        "rule": "onboarding_skip_note",
        "artifact": "A note",
        "required_when": (
            "A task is marked not applicable or blocked rather than done"
        ),
        "gates": [GATE_SKIP],
        "waiver": None,
        "workflows": [
            {"workflow": "onboarding", "layer": LAYER_SERVICE,
             "enforced_at": "app/services/onboarding_service.py"},
        ],
    },
]

#: Asked for by the Build Book and enforced by nothing. Declared here rather
#: than left as an absence, because an evidence matrix listing only what is
#: enforced reads as though the list were complete.
#:
#: Photographs are the case. Files attach to a quality check through the
#: ordinary store and the evidence pack collects them by correlation_id, so a
#: photograph taken is a photograph kept — but no path anywhere refuses a
#: damage or shortage rejection for having none. The word does not appear in
#: app/ outside two comments saying exactly that.
EVIDENCE_ASKED_FOR_BUT_NOT_REQUIRED: List[Dict] = [
    {
        "requirement": "photograph_on_damage_or_shortage",
        "asked_by": "build-book.txt line 459",
        "status": "attachable, collected if attached, never demanded",
        "would_be_enforced_at": "app/services/quality_check_service.py",
    },
]


def evidence_matrix(current_user: dict) -> Dict:
    """What the system makes somebody produce, when, and who the rule binds.

    Takes no db session, for the same reason sod_matrix does not: every answer
    here is a function of the rules in code, identical for every tenant.
    Nothing reads a record.

    The finding is the layer. Ten workflows refuse a blank rejection reason,
    and all ten refuse it over HTTP, so an API client sees one consistent
    control. Four of them enforce it only in the request schema, so the same
    control evaporates the moment the method is called from anywhere that is
    not a route — another service, a scheduled job, a migration, a test. That
    is invisible reading either layer on its own, which is the whole reason
    for putting them side by side.
    """
    _require_audit(current_user)

    rows = []
    for rule in EVIDENCE_RULES:
        layers = {w["layer"] for w in rule["workflows"]}
        rows.append({
            **rule,
            #: The weakest layer any workflow enforces this at, because a
            #: control is as binding as its loosest application.
            "weakest_layer": (
                LAYER_SCHEMA if LAYER_SCHEMA in layers else LAYER_SERVICE
            ),
            "enforced_at_mixed_depths": len(layers) > 1,
            "waivable": rule["waiver"] is not None,
        })

    return {
        "gates": [
            GATE_RAISE, GATE_SUBMIT, GATE_APPROVE, GATE_REJECT,
            GATE_RECORD, GATE_FILL, GATE_SKIP,
        ],
        "rules": rows,
        # Binds every caller, not only a route. The stronger position, and
        # what the other list should look like.
        "bound_at_the_service": [
            r["rule"] for r in rows if r["weakest_layer"] == LAYER_SERVICE
        ],
        # Enforced in a request schema and nowhere deeper: refused over HTTP,
        # not refused when the method is called directly. Named per workflow
        # rather than per rule, because for rejection_reason it is true of
        # four workflows out of ten, and naming the rule alone would say the
        # control is missing when it is mostly present.
        "bound_only_at_the_api": [
            {"rule": r["rule"], "workflow": w["workflow"],
             "enforced_at": w["enforced_at"]}
            for r in rows for w in r["workflows"]
            if w["layer"] == LAYER_SCHEMA
        ],
        # A permission holder can step past it, and the step is itself
        # evidenced. Not a weakness — a waiver on the record is the point —
        # but it is the row a control review asks about first.
        "waivable_with_a_recorded_reason": [
            {"rule": r["rule"], "at": r["waiver"]["at"],
             "recorded_as": r["waiver"]["recorded_as"]}
            for r in rows if r["waiver"]
        ],
        # Stated rather than implied by an absent row.
        "asked_for_but_not_required": EVIDENCE_ASKED_FOR_BUT_NOT_REQUIRED,
    }
