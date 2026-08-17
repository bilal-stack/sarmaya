from enum import Enum


class UserRole(str, Enum):
    """User roles with permissions"""
    ADMIN = "admin"
    AP_CLERK = "ap_clerk"
    MANAGER = "manager"
    CFO = "cfo"
    APPROVER = "approver"
    AUDITOR = "auditor"
    USER = "user"
    SYSTEM = "system"


class InvoiceState(str, Enum):
    """Invoice workflow states"""
    DRAFT = "draft"
    VALIDATED = "validated"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"
    CANCELLED = "cancelled"


class RequisitionState(str, Enum):
    """Purchase requisition workflow states.

    A requisition is a request, so it ends where it stops being one: `approved`
    means the need is authorised and may be sourced, and `converted` means an
    order was actually raised against it. The two are separate because an
    approved requisition that nobody ever ordered is a real and interesting
    state — it is budget someone was told they could spend and did not.
    """
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    CONVERTED = "converted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RFQState(str, Enum):
    """Request-for-quotation states.

    `closed` is the point of no return for the bidders: quoting is over and no
    quote may be added or altered after it, because from here the field is
    known. `awarded` records that a winner was chosen.
    """
    DRAFT = "draft"
    ISSUED = "issued"
    CLOSED = "closed"
    AWARDED = "awarded"
    CANCELLED = "cancelled"


class QuoteState(str, Enum):
    """States of a single vendor's quote."""
    RECEIVED = "received"
    SHORTLISTED = "shortlisted"
    AWARDED = "awarded"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class PurchaseOrderState(str, Enum):
    """Purchase order workflow states.

    A PO commits the company to spend, so it is approved before it is issued to
    the vendor: draft -> pending_approval -> approved -> issued. Receipt is
    tracked on the lines rather than as a state, because a delivery can be
    partial; `closed` is the terminal state once nothing further is expected.
    """
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    ISSUED = "issued"
    REJECTED = "rejected"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class PaymentState(str, Enum):
    """Payment run states.

    Maker-checker is the whole point: a run is prepared by one person and
    released by another. `released` is the irreversible step — it is the moment
    the instruction is considered authorised and the settled invoices become
    paid, so nothing after it edits the run.
    """
    DRAFT = "draft"
    PENDING_RELEASE = "pending_release"
    RELEASED = "released"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class BankChangeState(str, Enum):
    """States of a proposed change to a vendor's bank details.

    Deliberately not a configured workflow like invoices or orders. Those are
    business processes a tenant should be able to reshape; this is a fraud
    control, and a control a tenant can edit away in the workflow screen is a
    control in name only.

    `approved` and `effective` are separate because the cooling period is the
    point: approval starts a clock, and only when it expires may a payment use
    the new account. That window is what gives the real vendor time to notice a
    change they did not ask for.
    """
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EFFECTIVE = "effective"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class VendorStatus(str, Enum):
    """Vendor statuses"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    PENDING_VERIFICATION = "pending_verification"


class TenantIsolationLevel(str, Enum):
    """Multi-tenant isolation strategies"""
    RLS = "rls"
    SCHEMA = "schema"
    DATABASE = "database"


class SubscriptionTier(str, Enum):
    """Subscription tiers"""
    FREE = "free"
    STARTER = "starter"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


class PolicyType(str, Enum):
    """Policy types"""
    APPROVAL_LIMIT = "approval_limit"
    DOCUMENT_REQUIRED = "document_required"
    SEGREGATION_DUTY = "segregation_duty"
    VALIDATION_RULE = "validation_rule"
    BUDGET_CONTROL = "budget_control"


class WorkflowType(str, Enum):
    """Workflow types"""
    INVOICE = "invoice"
    PURCHASE_ORDER = "purchase_order"
    GRN = "grn"
    TIMESHEET = "timesheet"
    LEAVE_REQUEST = "leave_request"


class AuditAction(str, Enum):
    """Audit log actions"""
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted_for_approval"
    MARKED_PAID = "marked_paid"
    STATE_CHANGED = "state_changed"
    UPLOADED = "uploaded"


class StorageType(str, Enum):
    """File storage types"""
    LOCAL = "local"
    S3 = "s3"
    AZURE = "azure"
    GCS = "gcs"


class Currency(str, Enum):
    """Supported currencies"""
    PKR = "PKR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


class OCRProviderType(str, Enum):
    """Supported OCR providers"""
    OCR_SPACE = "ocr_space"
    AWS_TEXTRACT = "aws_textract"
    DOCUMENT_AI = "document_ai"
