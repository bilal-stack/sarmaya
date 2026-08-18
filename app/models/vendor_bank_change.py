"""Proposed changes to a vendor's bank details.

The most common invoice fraud in the world is not a forged invoice. It is a
real invoice, correctly approved, paid to the wrong account: someone changes
the vendor's bank details and waits for the next legitimate payment to go out.
Every downstream control passes, because nothing downstream is wrong — the
invoice is genuine, the approval is genuine, the release is genuine. Only the
destination changed, and no screen in the payment run shows that it changed
yesterday.

Before this existed, `PATCH /vendors/{id}` wrote bank fields directly behind
`vendors.manage`, which the AP clerk holds, and the audit entry for a vendor
update recorded only the legal name — so the change was not merely
uncontrolled, it was invisible.

A change is therefore a request, not an edit:

  * **The old values are snapshotted here**, so the trail says what it was as
    well as what it became. Reading it back does not depend on the vendor row.
  * **A second person approves it** — never the requester, with no admin
    exemption, because this is the step the fraud needs.
  * **A cooling period follows approval.** Approval starts a clock rather than
    taking effect; only when it expires may a payment use the new account.
    That window is the part that catches a compromised internal account: the
    real vendor has time to notice a change they never asked for, and the
    approver has time to confirm it by ringing a number they already had.
"""
from sqlalchemy import (
    Column, String, ForeignKey, DateTime, Index, text, Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel, UTC_NOW
from app.core.enums import BankChangeState


class VendorBankChange(BaseModel):
    __tablename__ = "vendor_bank_changes"

    OBJECT_TYPE = "vendor_bank_change"

    #: One unresolved change per vendor. Two open requests make it unclear
    #: which account was agreed to, and an approver reading one has no way to
    #: tell whether the other is the real one. The service refuses a second
    #: too; this is the guarantee that holds when two people act at once.
    #:
    #: Declared here as well as in the migration because dev and test databases
    #: are built with create_all, so a constraint living only in Alembic is
    #: absent exactly where it would first be exercised.
    #: The predicate uses the enum *member names* because that is what is stored:
#: SQLAlchemy's Enum type writes `PENDING_APPROVAL`, not the lowercase value
    #: the Python code compares against — it translates on the way in and out. A
    #: predicate written in lowercase silently matches nothing, which for a unique
    #: index means the constraint quietly does not exist.
    __table_args__ = (
        Index(
            "uq_vendor_bank_changes_one_open_per_vendor",
            "vendor_id",
            unique=True,
            postgresql_where=text(
                "current_state IN ('PENDING_APPROVAL', 'APPROVED')"
            ),
        ),
    )

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: Why the vendor says their details changed. Required: "they emailed us"
    #: is a different answer from "they sent a letter on headed paper we rang
    #: to confirm", and the approver is deciding on exactly that difference.
    reason = Column(String, nullable=False)

    # --- what is being proposed ---------------------------------------------
    new_bank_account_name = Column(String(255), nullable=True)
    new_bank_account_number = Column(String(100), nullable=True)
    new_bank_name = Column(String(255), nullable=True)
    new_iban = Column(String(50), nullable=True)
    new_swift_code = Column(String(20), nullable=True)

    # --- what it was at the moment the change was requested ------------------
    # Snapshotted rather than read from the vendor later: the point of the
    # record is to show the substitution, and after the change is applied the
    # vendor row no longer holds the old account.
    old_bank_account_name = Column(String(255), nullable=True)
    old_bank_account_number = Column(String(100), nullable=True)
    old_bank_name = Column(String(255), nullable=True)
    old_iban = Column(String(50), nullable=True)
    old_swift_code = Column(String(20), nullable=True)

    current_state = Column(
        SQLEnum(BankChangeState), nullable=False,
        default=BankChangeState.PENDING_APPROVAL,
    )

    requested_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    requested_at = Column(DateTime, server_default=UTC_NOW, nullable=False)

    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    #: When the new details may actually be paid to. Approval sets this to
    #: approval time plus the cooling period; until it passes, payments to this
    #: vendor are refused rather than sent to whichever account is current.
    effective_at = Column(DateTime, nullable=True)
    #: When the vendor row was actually updated, which is a separate fact from
    #: when it became allowed to be.
    applied_at = Column(DateTime, nullable=True)
    #: Who wrote the details onto the vendor. Applying needs only vendors.manage,
    #: so this can be a third person or the requester — and the Build Book's rule
    #: ("same person cannot change vendor bank details and approve the first
    #: payment after change") needs to know which.
    applied_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    rejection_reason = Column(String, nullable=True)

    tenant = relationship("Tenant", backref="vendor_bank_changes")
    vendor = relationship("Vendor", backref="bank_changes")
