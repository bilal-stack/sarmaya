"""The HR records that need somebody's signature.

Build Book, Variant C. Four governed things, and each is governed for a
different reason:

  * **HeadcountRequest** — asking to hire. The control is budget: a role
    approved outside the plan is a cost nobody agreed to, and it is agreed once
    and paid for years.
  * **OnboardingTask** — accounts, devices, access, documentation, training.
    Not a checklist widget: the access tasks are the ones that matter, because
    an onboarding that provisions a system and never records who approved it is
    exactly the gap an auditor opens with. The same engine runs offboarding,
    where an unfinished task is somebody who still has a login.
  * **PayrollChangeRequest** — changing what somebody is paid. The clearest SoD
    surface in the module: raising your own salary is the failure mode, and a
    manager approving a rise for the person who approves theirs is the subtler
    one.
  * **ExpenseReimbursement** — money out, claimed by an employee. Same shape as
    an invoice, and for the same reason: a claim is a payment request with a
    person attached.

Amounts on the last two are approved against thresholds, so both carry a
`total_amount` the policy engine can read, exactly like an invoice does.
"""
from sqlalchemy import (
    Column, String, Date, DateTime, Numeric, Integer, Boolean, ForeignKey,
    UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, SoftDeleteMixin

# --- headcount request states -----------------------------------------------
HC_DRAFT = "draft"
HC_PENDING_APPROVAL = "pending_approval"
HC_APPROVED = "approved"
HC_FILLED = "filled"
HC_REJECTED = "rejected"
HC_CANCELLED = "cancelled"

# --- onboarding task ---------------------------------------------------------
TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_BLOCKED = "blocked"
TASK_NOT_APPLICABLE = "not_applicable"

TASK_STATES = (
    TASK_PENDING, TASK_IN_PROGRESS, TASK_DONE, TASK_BLOCKED, TASK_NOT_APPLICABLE,
)

#: What kind of thing a task is. `access` is called out from the rest because
#: it is the category that grants somebody the ability to do things — and the
#: one an auditor asks to see evidence for.
TASK_CATEGORY_ACCOUNT = "account"
TASK_CATEGORY_ACCESS = "access"
TASK_CATEGORY_DEVICE = "device"
TASK_CATEGORY_DOCUMENT = "document"
TASK_CATEGORY_TRAINING = "training"
TASK_CATEGORY_PAYROLL = "payroll"

TASK_CATEGORIES = (
    TASK_CATEGORY_ACCOUNT, TASK_CATEGORY_ACCESS, TASK_CATEGORY_DEVICE,
    TASK_CATEGORY_DOCUMENT, TASK_CATEGORY_TRAINING, TASK_CATEGORY_PAYROLL,
)

#: Onboarding brings access; offboarding takes it away. One engine, because the
#: hard part is identical — a list of things other departments must do, with
#: nobody chasing them — and an offboarding task left undone is worse than an
#: onboarding one: it is a person who has left and can still sign in.
FLOW_ONBOARDING = "onboarding"
FLOW_OFFBOARDING = "offboarding"

# --- payroll change ----------------------------------------------------------
PAY_DRAFT = "draft"
PAY_PENDING_APPROVAL = "pending_approval"
PAY_APPROVED = "approved"
PAY_APPLIED = "applied"
PAY_REJECTED = "rejected"
PAY_CANCELLED = "cancelled"

PAY_REASON_PROMOTION = "promotion"
PAY_REASON_MARKET = "market_adjustment"
PAY_REASON_ANNUAL = "annual_review"
PAY_REASON_ROLE_CHANGE = "role_change"
PAY_REASON_CORRECTION = "correction"

PAY_REASONS = (
    PAY_REASON_PROMOTION, PAY_REASON_MARKET, PAY_REASON_ANNUAL,
    PAY_REASON_ROLE_CHANGE, PAY_REASON_CORRECTION,
)

# --- reimbursement -----------------------------------------------------------
EXP_DRAFT = "draft"
EXP_PENDING_APPROVAL = "pending_approval"
EXP_APPROVED = "approved"
EXP_PAID = "paid"
EXP_REJECTED = "rejected"
EXP_CANCELLED = "cancelled"


class HeadcountRequest(BaseModel, SoftDeleteMixin):
    """A request to hire, checked against the plan before anybody is offered.

    `filled` is terminal and separate from `approved`: an approved request that
    has been hired against must not authorise a second hire. That is the same
    reasoning that makes a requisition terminal once converted — one approval,
    one thing.
    """
    __tablename__ = "headcount_requests"

    OBJECT_TYPE = "headcount_request"
    REFERENCE_FIELD = "request_number"
    WORKFLOW_TYPE = "headcount_request"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    request_number = Column(String(64), nullable=False, index=True)
    job_title = Column(String(255), nullable=False)
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    #: How many people. Almost always one, occasionally not, and a request for
    #: five is a very different budget conversation from a request for one.
    positions = Column(Integer, nullable=False, default=1)

    employment_type = Column(String(30), nullable=False, default="permanent")
    is_sensitive_role = Column(Boolean, nullable=False, default=False)

    #: What the role is expected to cost per year, per position. The number the
    #: budget check reads.
    annual_cost = Column(Numeric(15, 2), nullable=False, default=0)

    #: Total commitment: positions x annual cost. Stored so the approval
    #: threshold reads one field, and so the figure that was approved is the
    #: figure on the record afterwards.
    total_amount = Column(Numeric(15, 2), nullable=False, default=0)

    justification = Column(String, nullable=True)
    target_start_date = Column(Date, nullable=True)

    current_state = Column(
        String(30), nullable=False, default=HC_DRAFT, index=True,
    )
    state_entered_at = Column(DateTime, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_reason = Column(String, nullable=True)

    #: The person eventually hired against it, when there is one.
    filled_by_employee_id = Column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True,
    )
    filled_at = Column(DateTime, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    org_unit = relationship("OrgUnit", backref="headcount_requests")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "request_number", name="uq_headcount_tenant_number"
        ),
    )


class OnboardingTask(BaseModel):
    """One thing that must happen before somebody starts — or after they leave.

    Deliberately not soft-deletable: a task is either done, not applicable, or
    outstanding, and "withdrawn" is a fourth state that would let an
    inconvenient access task disappear rather than be answered.
    """
    __tablename__ = "onboarding_tasks"

    OBJECT_TYPE = "onboarding_task"
    REFERENCE_FIELD = "title"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = Column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    flow = Column(String(20), nullable=False, default=FLOW_ONBOARDING, index=True)
    category = Column(String(30), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    detail = Column(String, nullable=True)

    #: Which department owns it. The Build Book's "cross-department handoff
    #: tasks to IT and Finance": the point of the engine is that most of these
    #: are not HR's to do, and nothing else in the company would chase them.
    owning_team = Column(String(50), nullable=True, index=True)
    assigned_to = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    status = Column(String(20), nullable=False, default=TASK_PENDING, index=True)
    due_date = Column(Date, nullable=True)

    completed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    #: Why a task was skipped or blocked. Required by the service for both,
    #: because "not applicable" with no reason is how an access task gets
    #: quietly dropped.
    resolution_note = Column(String(500), nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    employee = relationship("Employee", backref="onboarding_tasks")

    __table_args__ = (
        Index("ix_onboarding_employee_status", "employee_id", "status"),
    )


class PayrollChangeRequest(BaseModel, SoftDeleteMixin):
    """A change to what somebody is paid.

    Both the old and the new figure are stored on the request. The old one is
    not decoration: an approver needs to see the size of the jump, and reading
    it from the employee record at approval time would show whatever it is
    *now* rather than what it was when the request was raised.
    """
    __tablename__ = "payroll_change_requests"

    OBJECT_TYPE = "payroll_change_request"
    REFERENCE_FIELD = "request_number"
    WORKFLOW_TYPE = "payroll_change_request"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    request_number = Column(String(64), nullable=False, index=True)
    employee_id = Column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False, index=True,
    )

    reason_code = Column(String(40), nullable=False, index=True)
    reason_note = Column(String, nullable=True)

    current_salary = Column(Numeric(15, 2), nullable=True)
    new_salary = Column(Numeric(15, 2), nullable=False)
    #: The increase, stored. What the approval threshold is measured on — a
    #: rise of 200k matters whatever the starting salary was.
    total_amount = Column(Numeric(15, 2), nullable=False, default=0)

    effective_date = Column(Date, nullable=False)

    current_state = Column(
        String(30), nullable=False, default=PAY_DRAFT, index=True,
    )
    state_entered_at = Column(DateTime, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    rejected_reason = Column(String, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    employee = relationship("Employee", backref="payroll_changes")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "request_number", name="uq_payroll_change_tenant_number"
        ),
    )


class ExpenseReimbursement(BaseModel, SoftDeleteMixin):
    """Money an employee spent that the company owes them back.

    Shaped like an invoice on purpose — it is a payment request with a person
    attached — which means the evidence requirement is the same too: a claim
    with no receipt is an assertion.
    """
    __tablename__ = "expense_reimbursements"

    OBJECT_TYPE = "expense_reimbursement"
    REFERENCE_FIELD = "claim_number"
    WORKFLOW_TYPE = "expense_reimbursement"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    claim_number = Column(String(64), nullable=False, index=True)
    employee_id = Column(
        UUID(as_uuid=True), ForeignKey("employees.id"), nullable=False, index=True,
    )
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id"), nullable=True, index=True,
    )

    category = Column(String(50), nullable=False)
    description = Column(String, nullable=True)
    total_amount = Column(Numeric(15, 2), nullable=False, default=0)
    currency = Column(String(3), nullable=True)
    incurred_date = Column(Date, nullable=False)

    #: Whether a receipt is attached. Denormalised from the file store so the
    #: submit guard is one read rather than a join, and so the reason a claim
    #: was blocked is legible on the record itself.
    has_receipt = Column(Boolean, nullable=False, default=False)

    current_state = Column(
        String(30), nullable=False, default=EXP_DRAFT, index=True,
    )
    state_entered_at = Column(DateTime, nullable=True)

    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    rejected_reason = Column(String, nullable=True)

    #: Set when an approver waived a policy rule, with their reason. The
    #: overrides dashboard already reports on exactly this field name
    #: elsewhere, so a waived expense rule joins that report for free.
    policy_override_reason = Column(String, nullable=True)

    correlation_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    employee = relationship("Employee", backref="reimbursements")
    org_unit = relationship("OrgUnit", backref="reimbursements")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "claim_number", name="uq_reimbursement_tenant_number"
        ),
        Index("ix_reimbursement_employee_state", "employee_id", "current_state"),
    )
