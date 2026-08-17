"""Changing a vendor's bank details, as a controlled act.

Build Book, A1 Controls: *vendor bank change verification with dual approval
and cooling period policy*.

The threat is specific and worth stating, because every part of this module is
shaped by it. An attacker does not need to forge an invoice. They need one line
of a vendor record changed, and then they wait: the next genuine invoice, from
a genuine vendor, properly approved and properly released, pays them instead.
Every downstream control passes because nothing downstream is wrong. Maker-
checker on the payment does not help — the release is a real release of a real
invoice — and the release screen shows a vendor name and an amount, not the
fact that the account number changed yesterday.

So the control has to sit here, and it has three parts:

  * **A request, not an edit.** The old values are snapshotted, so the record
    shows the substitution rather than only the result.
  * **A second person approves.** Never the requester, with no admin exemption:
    this is the exact step the fraud needs, and the carve-out that keeps a
    one-person tenant working would also keep a one-person fraud working.
  * **A cooling period.** Approval starts a clock rather than taking effect.
    Until it expires no payment to that vendor may go out at all — not to the
    new account, and not to the old one either, because a payment run in flight
    during a disputed change is exactly what an attacker would like to see
    complete.
"""
import logging
from datetime import timedelta
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.vendor import Vendor
from app.models.vendor_bank_change import VendorBankChange
from app.core.config import settings
from app.core.enums import BankChangeState
from app.core.roles import (
    has_permission, PERM_MANAGE_VENDORS, PERM_VIEW_VENDORS,
    PERM_APPROVE_BANK_CHANGE,
)
from app.services.audit import log_audit
from app.services import sod
from app.utils.datetime_helpers import utc_now, to_utc, make_naive

logger = logging.getLogger(__name__)

OBJECT_TYPE = "vendor_bank_change"


def _now():
    """Naive UTC, comparable with the DB's tz-naive DateTime columns.

    `utc_now()` is timezone-aware and the columns are not, so comparing the two
    raises rather than returning a wrong answer — which is the good version of
    this bug. Delegation solved it the same way; the pattern is deliberate
    rather than duplicated by accident.
    """
    return make_naive(to_utc(utc_now()))

#: The fields this module owns. `update_vendor` refuses them so there is one
#: way in, and listing them here keeps the two from drifting.
BANK_FIELDS = (
    "bank_account_name",
    "bank_account_number",
    "bank_name",
    "iban",
    "swift_code",
)

#: States in which a change is still unresolved, and therefore still a reason
#: to hold payments to the vendor.
OPEN_STATES = (BankChangeState.PENDING_APPROVAL, BankChangeState.APPROVED)


class VendorBankService:
    def __init__(self, db: Session):
        self.db = db

    # --- reads ---------------------------------------------------------------

    def list_changes(
        self, current_user: dict, vendor_id: Optional[UUID] = None,
        state: Optional[str] = None,
    ) -> List[VendorBankChange]:
        self._require(current_user, PERM_VIEW_VENDORS, "view vendors")
        query = self.db.query(VendorBankChange)
        if vendor_id:
            query = query.filter(VendorBankChange.vendor_id == vendor_id)
        if state:
            query = query.filter(VendorBankChange.current_state == state)
        return query.order_by(VendorBankChange.requested_at.desc()).limit(200).all()

    def pending_for_vendor(self, vendor_id: UUID) -> Optional[VendorBankChange]:
        """An unresolved change, if there is one.

        Used by the payment path, so it takes no permission argument — it is a
        fact about the vendor, not a screen.
        """
        return (
            self.db.query(VendorBankChange)
            .filter(
                VendorBankChange.vendor_id == vendor_id,
                VendorBankChange.current_state.in_([s.value for s in OPEN_STATES]),
            )
            .order_by(VendorBankChange.requested_at.desc())
            .first()
        )

    # --- writes --------------------------------------------------------------

    def request_change(self, vendor_id: UUID, data, current_user: dict) -> VendorBankChange:
        """Propose new bank details for a vendor."""
        self._require(current_user, PERM_MANAGE_VENDORS, "request a bank change")

        vendor = self._vendor(vendor_id)

        existing = self.pending_for_vendor(vendor_id)
        if existing:
            raise ValueError(
                f"{vendor.legal_name} already has a bank change awaiting "
                "resolution. Resolve it before proposing another — two open "
                "requests make it unclear which account was agreed to."
            )

        proposed = {
            field: getattr(data, field, None) for field in BANK_FIELDS
        }
        if not any(v for v in proposed.values()):
            raise ValueError("A bank change must propose at least one new detail")

        # Nothing to control if nothing differs; refusing keeps the queue
        # meaningful rather than filling it with no-ops an approver has to read.
        unchanged = all(
            (proposed[f] or None) == (getattr(vendor, f, None) or None)
            or proposed[f] is None
            for f in BANK_FIELDS
        )
        if unchanged:
            raise ValueError("These are already the vendor's bank details")

        change = VendorBankChange(
            tenant_id=current_user["tenant_id"],
            vendor_id=vendor.id,
            reason=data.reason,
            current_state=BankChangeState.PENDING_APPROVAL,
            requested_by=current_user["id"],
            requested_at=_now(),
            **{f"new_{f}": proposed[f] for f in BANK_FIELDS},
            **{f"old_{f}": getattr(vendor, f, None) for f in BANK_FIELDS},
        )
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=vendor.id,
            action="bank_change_requested",
            comment=data.reason,
            # Both sides recorded, because the substitution is the thing a
            # reviewer needs to see — and after this is applied the vendor row
            # no longer holds the old account.
            before_value={f: getattr(vendor, f, None) for f in BANK_FIELDS},
            after_value={
                "change_id": str(change.id),
                "proposed": proposed,
                "requested_by": str(current_user["id"]),
            },
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def approve_change(self, change_id: UUID, current_user: dict) -> VendorBankChange:
        """Agree to the change, starting the cooling period.

        Does not alter the vendor. Approval says "this account is legitimate";
        the cooling period is what makes that claim checkable before money
        moves on it.
        """
        self._require(
            current_user, PERM_APPROVE_BANK_CHANGE, "approve vendor bank changes"
        )
        change = self._get(change_id)
        self._require_state(change, BankChangeState.PENDING_APPROVAL, "approve")

        # No admin exemption. This is the step the fraud needs.
        if sod.violates_self_bank_change_approval(change.requested_by, current_user):
            self._audit_block(change, current_user, "self_approval")
            raise PermissionError(
                "Segregation of duties: a vendor bank change must be approved "
                "by someone other than the person who requested it."
            )

        hours = settings.VENDOR_BANK_CHANGE_COOLING_HOURS
        change.current_state = BankChangeState.APPROVED
        change.approved_by = current_user["id"]
        change.approved_at = _now()
        change.effective_at = change.approved_at + timedelta(hours=hours)
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=change.vendor_id,
            action="bank_change_approved",
            comment=(
                f"Approved; effective after a {hours}-hour cooling period. "
                "Payments to this vendor are held until then."
            ),
            after_value={
                "change_id": str(change.id),
                "approved_by": str(current_user["id"]),
                "effective_at": change.effective_at.isoformat(),
                "cooling_hours": hours,
            },
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def apply_change(self, change_id: UUID, current_user: dict) -> Vendor:
        """Write the approved details onto the vendor, once the clock has run.

        Separate from approval on purpose. Approving and applying in one step
        would make the cooling period a comment rather than a control, and this
        is the moment the account actually changes — so it is the moment worth
        recording as its own event.
        """
        self._require(current_user, PERM_MANAGE_VENDORS, "apply a bank change")
        change = self._get(change_id)
        self._require_state(change, BankChangeState.APPROVED, "apply")

        now = _now()
        if change.effective_at and now < change.effective_at:
            remaining = change.effective_at - now
            hours = remaining.total_seconds() / 3600
            raise ValueError(
                f"The cooling period has {hours:.1f} hours left. The wait is "
                "the control: it is the window in which the real vendor can "
                "tell you they never asked for this."
            )

        vendor = self._vendor(change.vendor_id)
        before = {f: getattr(vendor, f, None) for f in BANK_FIELDS}

        for field in BANK_FIELDS:
            proposed = getattr(change, f"new_{field}", None)
            if proposed is not None:
                setattr(vendor, field, proposed)

        change.current_state = BankChangeState.EFFECTIVE
        change.applied_at = now
        self.db.add(vendor)
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=vendor.id,
            action="bank_change_applied",
            before_value=before,
            after_value={
                "change_id": str(change.id),
                **{f: getattr(vendor, f, None) for f in BANK_FIELDS},
                "requested_by": str(change.requested_by),
                "approved_by": str(change.approved_by),
            },
        )
        self.db.commit()
        self.db.refresh(vendor)
        return vendor

    def reject_change(
        self, change_id: UUID, reason: str, current_user: dict
    ) -> VendorBankChange:
        self._require(
            current_user, PERM_APPROVE_BANK_CHANGE, "reject vendor bank changes"
        )
        change = self._get(change_id)
        if change.current_state not in OPEN_STATES:
            raise ValueError(
                f"This change is {self._state(change)} and can no longer be rejected"
            )

        change.current_state = BankChangeState.REJECTED
        change.rejection_reason = reason
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=change.vendor_id,
            action="bank_change_rejected",
            comment=reason,
            after_value={"change_id": str(change.id)},
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    def cancel_change(
        self, change_id: UUID, reason: str, current_user: dict
    ) -> VendorBankChange:
        """Withdraw a request. Available to whoever may request one."""
        self._require(current_user, PERM_MANAGE_VENDORS, "cancel a bank change")
        change = self._get(change_id)
        if change.current_state not in OPEN_STATES:
            raise ValueError(
                f"This change is {self._state(change)} and can no longer be cancelled"
            )

        change.current_state = BankChangeState.CANCELLED
        change.rejection_reason = reason
        self.db.add(change)
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=change.vendor_id,
            action="bank_change_cancelled",
            comment=reason,
            after_value={"change_id": str(change.id)},
        )
        self.db.commit()
        self.db.refresh(change)
        return change

    # --- helpers -------------------------------------------------------------

    def _vendor(self, vendor_id: UUID) -> Vendor:
        vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
        if not vendor:
            raise ValueError("Vendor not found")
        return vendor

    def _get(self, change_id: UUID) -> VendorBankChange:
        change = (
            self.db.query(VendorBankChange)
            .filter(VendorBankChange.id == change_id)
            .first()
        )
        if not change:
            raise ValueError("Bank change not found")
        return change

    @staticmethod
    def _state(change: VendorBankChange) -> str:
        return str(
            getattr(change.current_state, "value", change.current_state)
        ).lower()

    def _require_state(
        self, change: VendorBankChange, expected: BankChangeState, action: str
    ) -> None:
        if self._state(change) != expected.value:
            raise ValueError(
                f"This change is {self._state(change)}, not {expected.value}; "
                f"it cannot be {action}d."
            )

    @staticmethod
    def _require(current_user: dict, permission: str, action: str) -> None:
        if not has_permission(current_user["role"], permission):
            raise PermissionError(
                f"Role '{current_user['role']}' does not have permission to {action}"
            )

    def _audit_block(
        self, change: VendorBankChange, current_user: dict, reason: str
    ) -> None:
        """A refused approval is committed on its own, so the attempt survives
        even though the action does not — and an attempt to self-approve a bank
        change is the single most interesting line in this trail."""
        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="vendor",
            object_id=change.vendor_id,
            action="bank_change_approval_blocked",
            comment=reason,
            after_value={"change_id": str(change.id), "reason": reason},
        )
        self.db.commit()
