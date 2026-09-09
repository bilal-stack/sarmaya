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

Two of the five Build Book matrices are here. Tolerance, vendor risk and
evidence are not, because the rules they would display do not exist yet: match
tolerance is two numbers for the whole tenant with no category or vendor axis,
vendor risk is an integer score with no tiers, and evidence requirements are
measured but never declared. Drawing those would mean inventing the rules in
the drawing, which is the failure this module exists to avoid.
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
