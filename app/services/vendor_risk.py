"""What we actually know against a vendor, scored.

`Vendor.risk_score` has existed since the model was written and was never
assigned — an Integer with `default=0`, exposed in VendorResponse, returning
zero for every vendor in every tenant. A field shaped like an assessment that
was a constant. This computes it.

**Why a score at all, when this codebase argues against one.**
`supplier_delivery_performance` says plainly that its three measures are
"deliberately not averaged into one score", because a supplier who is always
late but never wrong needs a different conversation from one who is punctual
and sends damaged goods. That reasoning is right, and it is the reason this
module never returns a bare number.

A score is useful for one thing a list of measures cannot do: ordering. "Which
of my four hundred vendors should somebody look at first" is a real question,
and it needs one comparable figure. So the score exists to sort, and every
factor that produced it is returned alongside it — the number opens the
conversation and the factors are the conversation. `Vendor.risk_flags` (JSON,
already on the model) is where they go, which is what that column was for.

**Only measured facts.** Every factor below reads something this system
records. Nothing here infers, estimates, or scores a vendor on data we do not
have — an unscored dimension is absent rather than assumed benign, and
`unscored_dimensions` in the result says which, because a 12 computed from two
signals is not the same claim as a 12 computed from seven.

The weights are the judgement, and they are stated here rather than buried:
they follow the same ordering the controls in this system already use. Bank
detail changes weigh most because every SoD rule in `sod.py` that refuses an
admin the carve-out it grants elsewhere is a bank-change rule — the code
already treats that as the event with no downstream control behind it.
"""
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.enums import BankChangeState, VendorStatus
from app.models.inventory import VENDOR_ATTRIBUTABLE_REASONS
from app.models.inventory_control import VendorReturn
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.vendor_bank_change import VendorBankChange
from app.utils.datetime_helpers import make_naive, to_utc, utc_now

#: How far back a factor looks. A vendor that behaved badly three years ago and
#: cleanly since is not today's problem, and a score with no memory limit only
#: ever climbs.
WINDOW_DAYS = 365

#: Recent enough that the change is still the most likely explanation for a
#: payment going somewhere new. DR-032 holds payments while a change is open;
#: this is about the window after it lands.
BANK_CHANGE_RECENT_DAYS = 90

#: Below this a vendor has no track record rather than a good one.
NEW_VENDOR_DAYS = 90

#: The ceiling. Scores are compared, not summed further, so anything above 100
#: would only make the top of the range less readable.
MAX_SCORE = 100


@dataclass(frozen=True)
class Factor:
    """One thing we know, what it cost, and the evidence for it."""
    code: str
    points: int
    detail: str

    def as_dict(self) -> Dict:
        return {"code": self.code, "points": self.points, "detail": self.detail}


#: Tiers, for the Build Book's vendor risk matrix. Three bands rather than
#: five: the score is an ordering device and a band only has to answer "does
#: somebody look at this now, soon, or not". Boundaries are inclusive of the
#: lower bound.
TIERS = [(70, "high"), (35, "medium"), (0, "low")]


def tier_for(score: int) -> str:
    for floor, name in TIERS:
        if score >= floor:
            return name
    return "low"


def _now():
    return make_naive(to_utc(utc_now()))


class VendorRiskService:
    """Scores one vendor, or every vendor in the tenant.

    Read-only by default. `refresh` persists, because the score is worth
    storing: sorting four hundred vendors by a value computed on the fly means
    computing four hundred of them on every list request.
    """

    def __init__(self, db: Session):
        self.db = db

    # --- the factors -------------------------------------------------------

    def _bank_changes(self, vendor: Vendor, since) -> List[Factor]:
        """Bank detail changes, weighted hardest.

        Every rule in sod.py that refuses an admin the exemption it grants
        elsewhere is a bank-change rule, and the docstring there says why: once
        the account is changed, the next genuine invoice, genuinely approved
        and genuinely released, pays the wrong person. The code already treats
        this as the event with no downstream control behind it, so the score
        does too.
        """
        changes = (
            self.db.query(VendorBankChange.applied_at)
            .filter(
                VendorBankChange.vendor_id == vendor.id,
                VendorBankChange.current_state == BankChangeState.EFFECTIVE.value,
                VendorBankChange.applied_at.isnot(None),
                VendorBankChange.applied_at >= since,
            )
            .all()
        )
        if not changes:
            return []

        factors = []
        recent_cutoff = _now() - timedelta(days=BANK_CHANGE_RECENT_DAYS)
        newest = max(c.applied_at for c in changes)
        if newest >= recent_cutoff:
            days = (_now() - newest).days
            factors.append(Factor(
                "bank_changed_recently", 25,
                f"Bank details changed {days} day{'s' if days != 1 else ''} ago.",
            ))
        # A second change inside a year is the pattern, not the event. One is
        # a vendor changing bank; several is worth a person looking.
        if len(changes) > 1:
            factors.append(Factor(
                "bank_changed_repeatedly", 15,
                f"{len(changes)} bank changes in the last {WINDOW_DAYS} days.",
            ))
        return factors

    def _standing(self, vendor: Vendor) -> List[Factor]:
        """What the tenant has already decided about this vendor."""
        status = getattr(vendor.status, "value", vendor.status)
        if status == VendorStatus.BLOCKED.value:
            # Somebody already made this call. The score should not quietly
            # disagree with a human decision that is on the record.
            return [Factor("blocked", 40, "Blocked by a user.")]
        if status == VendorStatus.PENDING_VERIFICATION.value:
            return [Factor(
                "not_verified", 20,
                "Never verified — invoices against it are held at approval.",
            )]
        return []

    def _onboarding(self, vendor: Vendor) -> List[Factor]:
        """Missing paperwork. Not fraud, but it is how fraud stays invisible."""
        factors = []
        if not (vendor.tax_id or "").strip():
            factors.append(Factor(
                "no_tax_id", 10,
                "No tax identifier on file.",
            ))
        if not (vendor.bank_account_number or "").strip():
            factors.append(Factor(
                "no_bank_details", 5,
                "No bank details, so nothing can be paid to it yet.",
            ))
        return factors

    def _billing(self, vendor: Vendor, since) -> List[Factor]:
        """Invoices from this vendor the duplicate check flagged."""
        flagged = (
            self.db.query(func.count(Invoice.id))
            .filter(
                Invoice.vendor_id == vendor.id,
                Invoice.potential_duplicate_id.isnot(None),
                Invoice.created_at >= since,
            )
            .scalar() or 0
        )
        if not flagged:
            return []
        # Capped: ten flagged invoices is not twice the concern of five, it is
        # the same concern with more instances of it.
        points = min(15, 5 * int(flagged))
        return [Factor(
            "duplicate_invoices", points,
            f"{flagged} invoice{'s' if flagged != 1 else ''} flagged as a "
            f"possible duplicate.",
        )]

    def _goods(self, vendor: Vendor, since) -> List[Factor]:
        """Returns whose reason is the vendor's fault.

        VENDOR_ATTRIBUTABLE_REASONS exists in the inventory module precisely so
        this question can be asked — the comment there says reason codes are a
        fixed vocabulary "so they can be counted", and that "which vendor
        damages the most goods is unanswerable if every receiver types their
        own word for damaged". This is that count.
        """
        attributable = (
            self.db.query(func.count(VendorReturn.id))
            .filter(
                VendorReturn.vendor_id == vendor.id,
                VendorReturn.reason_code.in_(list(VENDOR_ATTRIBUTABLE_REASONS)),
                VendorReturn.created_at >= since,
            )
            .scalar() or 0
        )
        if not attributable:
            return []
        points = min(15, 5 * int(attributable))
        return [Factor(
            "goods_returned_vendor_fault", points,
            f"{attributable} return{'s' if attributable != 1 else ''} for a "
            f"reason attributed to the vendor.",
        )]

    def _tenure(self, vendor: Vendor) -> List[Factor]:
        """A new vendor has no track record, which is not the same as a good
        one. Standard in vendor risk scoring and worth little on its own."""
        if not vendor.created_at:
            return []
        age = (_now() - make_naive(to_utc(vendor.created_at))).days
        if age < NEW_VENDOR_DAYS:
            return [Factor(
                "new_vendor", 10,
                f"Added {age} day{'s' if age != 1 else ''} ago; no history yet.",
            )]
        return []

    #: Dimensions a full vendor-risk model would carry that this system does
    #: not measure. Reported rather than silently skipped: a score built from
    #: five signals is a different claim from one built from eight, and a
    #: reader cannot tell which they are holding unless it says.
    UNSCORED = [
        ("sanctions_screening", "No sanctions or PEP list is checked."),
        ("financial_health", "No credit or solvency data is held."),
        ("concentration", "Spend share is reported on the CFO page but is a "
                          "fact about us, not about the vendor."),
        ("delivery_timeliness", "Measured on the supply chain page, and "
                                "deliberately not folded into a single score "
                                "there — see supplier_delivery_performance."),
    ]

    # --- scoring -----------------------------------------------------------

    def score(self, vendor: Vendor) -> Dict:
        """The score, the factors that made it, and what it does not cover."""
        since = _now() - timedelta(days=WINDOW_DAYS)

        factors: List[Factor] = []
        factors += self._standing(vendor)
        factors += self._bank_changes(vendor, since)
        factors += self._onboarding(vendor)
        factors += self._billing(vendor, since)
        factors += self._goods(vendor, since)
        factors += self._tenure(vendor)

        raw = sum(f.points for f in factors)
        total = min(MAX_SCORE, raw)

        return {
            "vendor_id": str(vendor.id),
            "score": total,
            "tier": tier_for(total),
            # Kept when the cap bites, so a 100 that was really a 140 is not
            # indistinguishable from one that landed exactly on the ceiling.
            "raw_score": raw,
            "capped": raw > MAX_SCORE,
            "factors": [f.as_dict() for f in factors],
            "window_days": WINDOW_DAYS,
            "unscored_dimensions": [
                {"code": c, "detail": d} for c, d in self.UNSCORED
            ],
        }

    def refresh(self, vendor_id: UUID) -> Optional[Dict]:
        """Recompute and store. Returns None when the vendor is not visible."""
        vendor = self.db.query(Vendor).filter(Vendor.id == vendor_id).first()
        if vendor is None:
            return None

        result = self.score(vendor)
        vendor.risk_score = result["score"]
        # risk_flags is the column this belongs in — it has been on the model
        # as JSON since the beginning, for exactly this.
        vendor.risk_flags = result["factors"]
        self.db.add(vendor)
        self.db.commit()
        return result

    def refresh_all(self) -> int:
        """Rescore every vendor the caller can see. Returns how many moved."""
        changed = 0
        for vendor in self.db.query(Vendor).all():
            result = self.score(vendor)
            if vendor.risk_score != result["score"]:
                changed += 1
            vendor.risk_score = result["score"]
            vendor.risk_flags = result["factors"]
            self.db.add(vendor)
        self.db.commit()
        return changed
