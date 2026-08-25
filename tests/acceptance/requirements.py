"""The Build Book's requirements, and what proves each one.

Andrew's Definition of Done asks for "acceptance tests (Given/When/Then)". The
literal reading is to rewrite 894 tests into Given/When/Then prose, which would
change nothing about what is verified — the tests already describe behaviour,
and reformatting them buys a shape rather than a fact.

The thing the item is actually for is **traceability**: being able to point at
a line in the spec and say which tests hold it up, and — more importantly —
being able to see the lines where the answer is "none". That second list is the
one worth having, and it does not exist anywhere else.

So this is the map, and `test_requirements_map.py` checks it. Every `tests`
entry below must match at least one test that actually collects. A renamed or
deleted test breaks the map loudly instead of leaving a document that claims
coverage which evaporated two refactors ago — which is the normal fate of a
traceability matrix maintained by hand.

`COVERED` means tests exist and are listed. `PARTIAL` means something real is
tested but the requirement is broader than what is proven. `NONE` means exactly
that, and those are the point of the document.
"""
from dataclasses import dataclass, field
from typing import List

COVERED = "covered"
PARTIAL = "partial"
NONE = "none"


@dataclass
class Requirement:
    id: str
    #: Line number in build-book.txt, so a reader can go and read the sentence
    #: in its own context rather than trusting this paraphrase.
    line: int
    text: str
    status: str
    #: Node-id prefixes. A file, a file::Class, or a full node id — whatever is
    #: the honest unit of proof for this requirement.
    tests: List[str] = field(default_factory=list)
    note: str = ""


REQUIREMENTS: List[Requirement] = [
    # --- Non-negotiables (build-book.txt lines 9-14) ------------------------
    Requirement(
        id="NN-1", line=10,
        text="Configuration-first: policies, thresholds, approval matrices and "
             "module toggles editable without code deploys.",
        status=COVERED,
        tests=[
            "tests/integration/test_config_admin.py",
            "tests/integration/test_config_versioning.py",
        ],
    ),
    Requirement(
        id="NN-2", line=11,
        text="Workflow-first: every business process is a state machine with "
             "explicit states, transitions and audit evidence.",
        status=COVERED,
        tests=[
            "tests/unit/test_workflow.py",
            "tests/unit/test_workflow_guards.py",
            "tests/integration/test_transition_guards.py",
            "tests/integration/test_workflow_generalisation.py",
        ],
    ),
    Requirement(
        id="NN-3", line=12,
        text="Strict auditability: immutable audit trail on every action, "
             "policy evaluation snapshot, evidence pack integrity hashing.",
        status=COVERED,
        tests=[
            "tests/integration/test_audit_integrity.py",
            "tests/integration/test_audit_durability.py",
            "tests/integration/test_policy_eval.py",
            "tests/integration/test_evidence_pack.py",
        ],
    ),
    Requirement(
        id="NN-4", line=13,
        text="AI is gated: structured JSON only, schema validation always, "
             "human-in-the-loop when risk or confidence thresholds require it.",
        status=COVERED,
        tests=[
            "tests/integration/test_ai_orchestration.py",
            "tests/unit/test_ai_gating.py",
            "tests/unit/test_extraction_gating.py",
        ],
    ),
    Requirement(
        id="NN-5", line=14,
        text="Multi-tenant compatible even though deploy-per-client is primary.",
        status=COVERED,
        tests=[
            "tests/integration/test_rls_isolation.py",
            "tests/integration/test_tenant_scoping.py",
            "tests/integration/test_cross_tenant_api.py",
        ],
    ),

    # --- Definition of Done (lines 15-22) ------------------------------------
    Requirement(
        id="DOD-1", line=16,
        text="All workflows implemented with state machine definitions, "
             "transition guards, SLAs and escalation rules.",
        status=COVERED,
        tests=[
            "tests/integration/test_transition_guards.py",
            "tests/integration/test_sla_escalation.py",
            "tests/integration/test_workflow_generalisation.py",
        ],
    ),
    Requirement(
        id="DOD-2", line=17,
        text="All policy checks implemented with a versioned rules schema and "
             "deterministic outputs.",
        status=COVERED,
        tests=[
            "tests/integration/test_policy_eval.py",
            "tests/integration/test_policy_simulator.py",
            "tests/integration/test_config_versioning.py",
        ],
    ),
    Requirement(
        id="DOD-3", line=18,
        text="Decision Inbox supports every work item type in that variant.",
        status=COVERED,
        tests=["tests/integration/test_decision_inbox.py"],
    ),
    Requirement(
        id="DOD-4", line=19,
        text="Reports and dashboards shipped and validated against a seed "
             "dataset.",
        status=COVERED,
        tests=["tests/integration/test_dashboards.py"],
        note="Seed dataset is scripts/seed_demo_data.py — 90 days of history.",
    ),
    Requirement(
        id="DOD-5", line=20,
        text="Integration connectors implemented with retries, idempotency, "
             "dead-letter queues and reconciliation records.",
        status=PARTIAL,
        tests=["tests/integration/test_integration_hub.py"],
        note="Built for QuickBooks Online: retries with backoff, idempotency "
             "in three layers (a unique constraint, the connector's DocNumber "
             "check, and a savepoint on enqueue), and failed posts held as "
             "dead letters that a human retries without losing the attempt "
             "history. Partial on two counts, both deliberate. 'Connectors' is "
             "plural and only one exists — Xero and SAP are deferred until the "
             "interface has been shaped by a second real integration (DR-049). "
             "And 'reconciliation records' is met only as far as the anchor: "
             "every posted entry stores the provider's own transaction id, but "
             "nothing yet reports Sarmaya's posts against the client's books "
             "side by side.",
    ),
    Requirement(
        id="DOD-6a", line=21,
        text="Test pack: acceptance tests (Given/When/Then).",
        status=PARTIAL,
        tests=["tests/acceptance/test_requirements_map.py"],
        note="Traceability exists — this map. The tests themselves are not "
             "written in Given/When/Then prose, deliberately: reformatting "
             "them would change no fact about what is verified.",
    ),
    Requirement(
        id="DOD-6b", line=21,
        text="Test pack: regression checklist.",
        status=PARTIAL,
        tests=[],
        note="No checklist document. Largely served by the suite running in "
             "full plus scripts/verify_deployment.py, which fails on the "
             "conditions a checklist would ask a human to eyeball.",
    ),
    Requirement(
        id="DOD-6c", line=21,
        text="Test pack: performance smoke tests.",
        status=COVERED,
        tests=["tests/integration/test_performance_smoke.py"],
    ),
    Requirement(
        id="DOD-7", line=22,
        text="Admin Console: configuration screens, job monitor, error monitor "
             "and audit viewer.",
        status=COVERED,
        tests=[
            "tests/integration/test_config_admin.py",
            "tests/integration/test_system_health.py",
            "tests/integration/test_audit_timeline.py",
        ],
    ),

    # --- Access controls (lines 110-114) -------------------------------------
    Requirement(
        id="AC-1", line=111,
        text="RBAC with scopes: tenant, business unit, location, cost center, "
             "project.",
        status=COVERED,
        tests=[
            "tests/integration/test_org_scopes.py",
            "tests/unit/test_roles.py",
        ],
    ),
    Requirement(
        id="AC-2", line=112,
        text="SoD enforcement: maker-checker and restricted combinations of "
             "roles.",
        status=COVERED,
        tests=[
            "tests/integration/test_sod_enforcement.py",
            "tests/unit/test_sod.py",
        ],
    ),
    Requirement(
        id="AC-3", line=113,
        text="Field-level masking for sensitive fields (bank accounts, "
             "national IDs).",
        status=COVERED,
        tests=[
            "tests/integration/test_bank_detail_masking.py",
            "tests/integration/test_hr_people.py::TestSensitiveFieldsAreMasked",
        ],
    ),
    Requirement(
        id="AC-4", line=114,
        text="Auditor role: read-only access plus audit export.",
        status=COVERED,
        tests=[
            "tests/integration/test_auth_privilege_escalation.py",
            "tests/integration/test_exports.py::TestTheEvidencePackExport",
        ],
    ),

    # --- Operational security (lines 115-119) --------------------------------
    Requirement(
        id="SEC-1", line=119,
        text="Immutable audit: guardrails to prevent hard deletes; soft delete "
             "for business objects only.",
        status=COVERED,
        tests=[
            "tests/integration/test_soft_deletes.py",
            "tests/integration/test_audit_durability.py",
        ],
    ),
    Requirement(
        id="SEC-2", line=116,
        text="Secrets management: environment injected, never stored in DB.",
        status=PARTIAL,
        tests=[],
        note="True of the code — settings come from environment — but nothing "
             "tests that a secret never reaches a column. Verified at deploy "
             "time by scripts/verify_deployment.py instead.",
    ),

    # --- SLA, escalation, delegation (lines 159-161) -------------------------
    Requirement(
        id="SLA-1", line=159,
        text="SLA timers start when a task enters a state; escalation creates "
             "or reassigns based on configured rules.",
        status=COVERED,
        tests=["tests/integration/test_sla_escalation.py"],
    ),
    Requirement(
        id="SLA-2", line=160,
        text="Delegation supports temporary assignment with start and end "
             "dates.",
        status=COVERED,
        tests=["tests/integration/test_delegation.py"],
    ),
    Requirement(
        id="SLA-3", line=161,
        text="Escalations must preserve the original approver chain for audit.",
        status=COVERED,
        tests=["tests/integration/test_sla_escalation.py"],
    ),

    # --- AI principles (lines 213-220) ---------------------------------------
    Requirement(
        id="AI-1", line=214,
        text="No free-form outputs in production paths — strict JSON validated "
             "against schemas.",
        status=COVERED,
        tests=["tests/integration/test_ai_orchestration.py"],
    ),
    Requirement(
        id="AI-2", line=215,
        text="AI never finalizes high-risk decisions without human "
             "confirmation.",
        status=COVERED,
        tests=[
            "tests/unit/test_ai_gating.py",
            "tests/integration/test_autopilot.py",
        ],
    ),
    Requirement(
        id="AI-3", line=216,
        text="AI must always attach an explainability trace.",
        status=COVERED,
        tests=[
            "tests/integration/test_ai_action_log.py",
            "tests/unit/test_field_explainer.py",
        ],
    ),
    Requirement(
        id="AI-4", line=217,
        text="Prompt and model versions stored with every AI output for "
             "reproducibility.",
        status=COVERED,
        tests=[
            "tests/integration/test_ai_action_log.py",
            "tests/integration/test_ai_orchestration.py",
        ],
    ),

    # --- Reporting (line 264) ------------------------------------------------
    Requirement(
        id="REP-1", line=264,
        text="Reporting is role-based and decision-focused; every dashboard "
             "leads to a drill-down into the Decision Inbox and audit timeline.",
        status=PARTIAL,
        tests=["tests/integration/test_dashboards.py"],
        note="Dashboards carry drill-down links and are permission-gated. The "
             "per-persona report catalogue is not built.",
    ),
    Requirement(
        id="REP-2", line=114,
        text="Audit export — getting evidence out as a file.",
        status=COVERED,
        tests=["tests/integration/test_exports.py"],
    ),

    # --- Variant C: HR OS (lines 431-450) ------------------------------------
    Requirement(
        id="C1-1", line=433,
        text="Headcount request, approvals, hiring pipeline checkpoints, "
             "offer approvals.",
        status=COVERED,
        tests=["tests/integration/test_hr_workflows.py::TestHeadcountRequests"],
    ),
    Requirement(
        id="C1-2", line=434,
        text="Onboarding task engine: accounts, devices, access, "
             "documentation, training.",
        status=COVERED,
        tests=[
            "tests/integration/test_hr_workflows.py::TestOnboardingAndOffboarding",
        ],
    ),
    Requirement(
        id="C1-3", line=435,
        text="Cross-department handoff tasks to IT and Finance with a single "
             "audit chain.",
        status=COVERED,
        tests=[
            "tests/integration/test_hr_workflows.py::TestOnboardingAndOffboarding",
        ],
    ),
    Requirement(
        id="C1-4", line=437,
        text="Budget and headcount policy checks against role and department.",
        status=PARTIAL,
        tests=["tests/integration/test_hr_workflows.py::TestHeadcountRequests"],
        note="A request must state its annual cost and cannot be approved by "
             "whoever raised it, and plan-vs-actual reports committed cost. "
             "There is no stored departmental budget to check against yet, so "
             "the control is 'the cost is visible and signed for' rather than "
             "'the cost fits the budget'.",
    ),
    Requirement(
        id="C1-5", line=438,
        text="Background verification evidence requirements for sensitive "
             "roles.",
        status=COVERED,
        tests=["tests/integration/test_hr_workflows.py::TestHeadcountRequests"],
    ),
    Requirement(
        id="C1-6", line=439,
        text="SoD for HR actions and payroll approvals.",
        status=COVERED,
        tests=[
            "tests/integration/test_hr_people.py::TestNobodySignsTheirOwnPay",
            "tests/integration/test_hr_workflows.py::TestExpenseClaims",
        ],
    ),
    Requirement(
        id="C1-7", line=444,
        text="Reports: time to hire, onboarding SLA completion, headcount plan "
             "vs actual, payroll variance and exception trends.",
        status=COVERED,
        tests=["tests/integration/test_hr_api.py::TestTheHrReports"],
    ),
    Requirement(
        id="C2-1", line=449,
        text="Payroll change requests, approvals, payroll run evidence "
             "capture, posting to finance.",
        status=PARTIAL,
        tests=["tests/integration/test_hr_people.py::TestApplyingTheChange"],
        note="Change requests, approvals and the applied trail are built. "
             "Payroll *runs* and posting to finance are not: that needs a "
             "payroll calculation engine and a general ledger, neither of "
             "which exists.",
    ),
    Requirement(
        id="C2-2", line=450,
        text="Expense reimbursements with policy rules and evidence "
             "requirements.",
        status=COVERED,
        tests=["tests/integration/test_hr_workflows.py::TestExpenseClaims"],
    ),
    Requirement(
        id="AC-3b", line=113,
        text="Field-level masking applied to national IDs and pay.",
        status=COVERED,
        tests=[
            "tests/integration/test_hr_people.py::TestSensitiveFieldsAreMasked",
            "tests/integration/test_hr_api.py::TestMaskingHoldsAtTheEdge",
        ],
    ),

    # --- Variant D: Supply Chain OS (lines 451-466) --------------------------
    Requirement(
        id="D1-1", line=453,
        text="Receiving, GRN, quality checks, putaway and stock updates.",
        status=COVERED,
        tests=[
            "tests/integration/test_inventory.py::TestReceivingPutsStockOnTheShelf",
            "tests/integration/test_inventory_quality.py",
        ],
    ),
    Requirement(
        id="D1-2", line=454,
        text="Inventory adjustments approval with thresholds and evidence.",
        status=COVERED,
        tests=[
            "tests/integration/test_inventory.py::TestAdjustmentsAreControlled",
            "tests/integration/test_inventory.py::TestDualApproval",
        ],
    ),
    Requirement(
        id="D1-3", line=455,
        text="Returns management with reason codes and vendor accountability.",
        status=COVERED,
        tests=["tests/integration/test_inventory_quality.py::TestReturns"],
    ),
    Requirement(
        id="D1-4", line=457,
        text="Adjustment thresholds with dual approval above limit.",
        status=COVERED,
        tests=["tests/integration/test_inventory.py::TestDualApproval"],
    ),
    Requirement(
        id="D1-5", line=458,
        text="SoD separation between receiver and approver.",
        status=COVERED,
        tests=[
            "tests/integration/test_inventory.py::TestAdjustmentsAreControlled",
        ],
    ),
    Requirement(
        id="D1-6", line=459,
        text="Damage and shortage evidence requirements with photos and QC notes.",
        status=PARTIAL,
        tests=[
            "tests/integration/test_inventory_quality.py::TestARejectionMustBeExplained",
        ],
        note="A rejection is refused without a reason code and a note, which is "
             "the enforceable half. Photos attach through the existing file "
             "store and are picked up by the evidence pack via correlation_id, "
             "but nothing yet *requires* one.",
    ),
    Requirement(
        id="D1-7", line=461,
        text="AI assists: exception explanations for shortages, damages, delays; "
             "suggest likely root causes and follow-up tasks.",
        status=COVERED,
        tests=["tests/integration/test_inventory_api.py::TestExplainingABadDelivery"],
    ),
    Requirement(
        id="D1-8", line=464,
        text="Reports: stock accuracy and adjustment rate; supplier delivery "
             "performance and lead time adherence; GRN to invoice latency.",
        status=COVERED,
        tests=["tests/integration/test_supply_chain_reports.py"],
    ),

    # --- Correlation (line 572) ----------------------------------------------
    Requirement(
        id="COR-1", line=572,
        text="Search must support correlation_id to reconstruct an entire "
             "story instantly.",
        status=COVERED,
        tests=["tests/integration/test_correlation.py"],
    ),
]
