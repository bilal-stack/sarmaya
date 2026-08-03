from sqlalchemy.orm import Session
from typing import Dict, List, Tuple
from uuid import UUID

from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.policy_repository import PolicyRepository
from app.models.policy import Policy
from app.models.invoice import Invoice
from app.models.audit_log import AuditLog
from app.services.invoice_service import InvoiceService
from app.services.workflow import transition_state
from app.services.audit import log_audit
from app.services.config_versioning import record_version, TYPE_AUTOPILOT
from app.schemas.autopilot import AutopilotConfig
from app.core.enums import InvoiceState, VendorStatus
from app.core.roles import has_permission, PERM_APPROVE_INVOICE, PERM_MANAGE_POLICIES
from app.utils.money import money_to_float
from app.utils.datetime_helpers import utc_now

POLICY_TYPE = "autopilot"
POLICY_NAME = "Autopilot settings"
ACTION_AUTO_APPROVED = "auto_approved"
ACTION_REVERTED = "auto_approval_reverted"


class AutopilotService:
    """Restricted Autopilot: opt-in, low-risk, reversible, logged auto-approval
    of pending invoices that fall within configured safe bounds. A human enables
    it, can dry-run a preview, triggers each run, and can revert any auto-approval
    that has not yet been paid.
    """

    def __init__(self, db: Session):
        self.db = db
        self.repository = InvoiceRepository(db)
        self.policy_repo = PolicyRepository(db)

    # --- configuration ------------------------------------------------------

    def get_config(self, current_user: dict) -> AutopilotConfig:
        self._require_manage(current_user)
        return self._load_config()

    def set_config(self, data: AutopilotConfig, current_user: dict) -> AutopilotConfig:
        self._require_manage(current_user)

        policy = self.policy_repo.get_by_name(POLICY_TYPE, POLICY_NAME)
        before = policy.rule_config if policy else None
        rule = data.model_dump()

        if policy:
            policy.rule_config = rule
            policy.is_active = True
            self.policy_repo.update(policy)
            change_action = "updated"
        else:
            policy = Policy(
                tenant_id=current_user["tenant_id"],
                policy_type=POLICY_TYPE,
                policy_name=POLICY_NAME,
                description="Restricted Autopilot settings.",
                rule_config=rule,
                applies_to="invoice",
                is_active=True,
                priority=0,
            )
            self.policy_repo.create(policy)
            change_action = "created"

        # Autopilot settings are a per-tenant singleton; version them under a
        # fixed key so their full edit history is preserved.
        record_version(
            self.db, current_user["tenant_id"], TYPE_AUTOPILOT, TYPE_AUTOPILOT,
            rule, change_action, current_user["id"],
        )
        # Flush, do not commit: changing what autopilot may approve without
        # a human must not be recordable separately from who changed it.
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="tenant_config",
            object_id=policy.id,
            action="autopilot_configured",
            before_value=before,
            after_value=rule,
        )
        self.policy_repo.commit()
        return AutopilotConfig(**rule)

    # --- preview / run ------------------------------------------------------

    def preview(self, current_user: dict) -> Dict:
        self._require_approve(current_user)
        cfg = self._load_config()
        candidates = self._scan(cfg)
        eligible = [c for c in candidates if c["eligible"]]
        return {
            "enabled": cfg.enabled,
            "config": cfg,
            "considered": len(candidates),
            "eligible_count": len(eligible),
            "candidates": [c.view for c in candidates],
        }

    def run(self, current_user: dict) -> Dict:
        self._require_approve(current_user)
        cfg = self._load_config()
        if not cfg.enabled:
            raise ValueError("Autopilot is disabled; enable it in configuration first")

        candidates = self._scan(cfg)
        approved: List[Dict] = []

        for cand, invoice in candidates:
            if not cand["eligible"]:
                continue
            transition_state(
                db=self.db,
                obj=invoice,
                target_state=InvoiceState.APPROVED.value,
                user_id=current_user["id"],
            )
            invoice.approved_by = current_user["id"]
            invoice.approved_at = utc_now()
            self.repository.update(invoice)
            log_audit(
                db=self.db,
                tenant_id=current_user["tenant_id"],
                user_id=current_user["id"],
                object_type="invoice",
                object_id=invoice.id,
                action=ACTION_AUTO_APPROVED,
                workflow_step=invoice.current_state,
                workflow_type="invoice",
                comment=(
                    f"Auto-approved by Restricted Autopilot: amount "
                    f"{cand['amount']:,.0f} within limit {cfg.max_auto_approve_amount:,.0f}."
                ),
                after_value={"auto": True, "limit": cfg.max_auto_approve_amount},
            )
            approved.append(cand)

        self.repository.commit()
        return {
            "enabled": cfg.enabled,
            "considered": len(candidates),
            "approved_count": len(approved),
            "approved": approved,
        }

    def revert_auto_approval(self, invoice_id: UUID, current_user: dict) -> Invoice:
        """Undo an autopilot approval that has not yet been paid, returning the
        invoice to pending approval for manual handling."""
        self._require_approve(current_user)

        invoice = self.repository.get_by_id(invoice_id)
        if not invoice:
            raise ValueError("Invoice not found")
        if invoice.current_state != InvoiceState.APPROVED.value:
            raise ValueError("Only an approved invoice can be reverted")

        was_auto = (
            self.db.query(AuditLog)
            .filter(
                AuditLog.object_type == "invoice",
                AuditLog.object_id == invoice_id,
                AuditLog.action == ACTION_AUTO_APPROVED,
            )
            .first()
        )
        if not was_auto:
            raise ValueError("This invoice was not auto-approved by Autopilot")

        invoice.current_state = InvoiceState.PENDING_APPROVAL.value
        invoice.state_entered_at = utc_now()  # restart the SLA timer
        invoice.approved_by = None
        invoice.approved_at = None
        self.repository.update(invoice)
        # Flush, do not commit: reversing an automated approval is itself a
        # governance action and must land with its record.
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="invoice",
            object_id=invoice.id,
            action=ACTION_REVERTED,
            workflow_step=invoice.current_state,
            workflow_type="invoice",
            comment="Autopilot approval reverted to pending for manual review.",
        )
        self.repository.commit()
        return invoice

    # --- internals ----------------------------------------------------------

    def _load_config(self) -> AutopilotConfig:
        policy = self.policy_repo.get_by_name(POLICY_TYPE, POLICY_NAME)
        if not policy or not policy.is_active:
            return AutopilotConfig()
        return AutopilotConfig(**(policy.rule_config or {}))

    def _scan(self, cfg: AutopilotConfig) -> List:
        """Evaluate eligibility for each pending invoice. Returns a list whose
        items are dicts (candidate view); run() also needs the Invoice, so this
        returns tuples (candidate, invoice) but is list-comprehension friendly via
        the .eligible flag on the candidate."""
        results = []
        for invoice, vendor in self.repository.get_pending_with_vendor(limit=500):
            eligible, reason = self._evaluate(cfg, invoice, vendor)
            cand = {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "vendor_name": invoice.vendor_name,
                "amount": money_to_float(invoice.total_amount),
                "eligible": eligible,
                "reason": reason,
            }
            results.append(_Candidate(cand, invoice))
        return results

    @staticmethod
    def _evaluate(cfg: AutopilotConfig, invoice: Invoice, vendor) -> Tuple[bool, str]:
        if not cfg.enabled:
            return False, "Autopilot is disabled."
        amount = money_to_float(invoice.total_amount)
        if cfg.require_no_duplicate and invoice.potential_duplicate_id and not invoice.duplicate_acknowledged:
            return False, "Flagged as a potential duplicate."
        if cfg.require_active_vendor and (vendor is None or vendor.status != VendorStatus.ACTIVE):
            vstatus = vendor.status.value if vendor else "missing"
            return False, f"Vendor is {vstatus}, not active."
        missing = InvoiceService._missing_required_fields(invoice)
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}."
        if amount > cfg.max_auto_approve_amount:
            return False, (
                f"Amount {amount:,.0f} exceeds the autopilot limit "
                f"{cfg.max_auto_approve_amount:,.0f}."
            )
        return True, (
            f"Within autopilot bounds (amount {amount:,.0f} <= "
            f"{cfg.max_auto_approve_amount:,.0f}, active vendor, no duplicate)."
        )

    @staticmethod
    def _require_approve(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_APPROVE_INVOICE):
            raise PermissionError("You do not have permission to run autopilot")

    @staticmethod
    def _require_manage(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_MANAGE_POLICIES):
            raise PermissionError("You do not have permission to configure autopilot")


class _Candidate:
    """Lightweight pairing of a candidate view dict with its Invoice, supporting
    both tuple unpacking (run) and dict access (preview)."""
    __slots__ = ("view", "invoice")

    def __init__(self, view: Dict, invoice: Invoice):
        self.view = view
        self.invoice = invoice

    def __iter__(self):
        # allows: for cand, invoice in candidates
        yield self.view
        yield self.invoice

    def __getitem__(self, key):
        return self.view[key]
