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
