"""Scoring a vendor on what we actually know about it.

`Vendor.risk_score` was an Integer with `default=0` that nothing ever assigned
— exposed in VendorResponse, returning zero for every vendor in every tenant.
These tests exist because the replacement must not be the same failure wearing
a computation: a number that looks like an assessment and is really a habit.

So each factor is tested for the thing it claims, and
TestTheScoreExplainsItself covers the property the whole design rests on. This
codebase already argues against collapsing vendor signals into one number —
supplier_delivery_performance says its three measures are "deliberately not
averaged into one score" — and the answer to that objection is that this score
never travels without its factors. If the factors ever stop coming back, the
score becomes exactly the thing that docstring warns about.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.enums import (
    BankChangeState, InvoiceState, UserRole, VendorStatus,
)
from app.models.inventory import (
    REASON_COUNT_CORRECTION, REASON_DAMAGED, StockLocation,
)
from app.models.inventory_control import VendorReturn
from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.vendor_bank_change import VendorBankChange
from app.services.vendor_risk import (
    MAX_SCORE, VendorRiskService, tier_for,
)
from app.utils.datetime_helpers import make_naive, to_utc, utc_now

pytestmark = pytest.mark.integration


def _now():
    return make_naive(to_utc(utc_now()))


def _vendor(db, tenant_id, **kw):
    """A vendor with nothing against it: verified, papers on file, not new."""
    defaults = dict(
        legal_name=f"V-{uuid.uuid4().hex[:6]}",
        status=VendorStatus.ACTIVE,
        tax_id="TAX-1234",
        bank_account_number="0001",
    )
    defaults.update(kw)
    vendor = Vendor(id=uuid.uuid4(), tenant_id=tenant_id, **defaults)
    db.add(vendor)
    db.flush()
    # created_at is server-defaulted; aged past the new-vendor window so tenure
    # does not quietly contribute to every other test's expected total.
    vendor.created_at = _now() - timedelta(days=400)
    db.flush()
    return vendor


def _bank_change(db, tenant_id, vendor, user, days_ago, state=BankChangeState.EFFECTIVE):
    change = VendorBankChange(
        id=uuid.uuid4(), tenant_id=tenant_id, vendor_id=vendor.id,
        current_state=state.value, requested_by=user["id"],
        reason="Vendor moved bank.",
        applied_at=_now() - timedelta(days=days_ago),
    )
    db.add(change)
    db.flush()
    return change


def _invoice(db, tenant_id, vendor, user, **kw):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id,
        invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name=vendor.legal_name, vendor_id=vendor.id,
        invoice_date=date.today(), total_amount=Decimal("100"),
        current_state=InvoiceState.PENDING_APPROVAL.value,
        created_by=user["id"], **kw,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _flagged_invoice(db, tenant_id, vendor, user):
    """potential_duplicate_id is a real foreign key, so the invoice it points
    at has to exist — a flagged invoice is meaningless without the one it was
    flagged against."""
    original = _invoice(db, tenant_id, vendor, user)
    return _invoice(db, tenant_id, vendor, user,
                    potential_duplicate_id=original.id)


def _return(db, tenant_id, vendor, user, reason):
    location = StockLocation(
        id=uuid.uuid4(), tenant_id=tenant_id,
        code=f"L-{uuid.uuid4().hex[:5]}", name="Main",
    )
    db.add(location)
    db.flush()
    ret = VendorReturn(
        id=uuid.uuid4(), tenant_id=tenant_id, vendor_id=vendor.id,
        return_number=f"RET-{uuid.uuid4().hex[:6]}", location_id=location.id,
        reason_code=reason, created_by=user["id"],
    )
    db.add(ret)
    db.flush()
    return ret


def _codes(result):
    return {f["code"] for f in result["factors"]}


def _points(result, code):
    return next(f["points"] for f in result["factors"] if f["code"] == code)


class TestACleanVendorScoresZero:
    def test_nothing_against_it_is_nothing_against_it(self, db, tenant, make_user):
        """The baseline that makes every other number mean something. If a
        clean vendor scored 8, nobody could read the scale."""
        vendor = _vendor(db, tenant.id)

        result = VendorRiskService(db).score(vendor)

        assert result["score"] == 0
        assert result["factors"] == []
        assert result["tier"] == "low"


class TestBankChangesWeighHeaviest:
    def test_a_recent_change_scores(self, db, tenant, make_user):
        """Every SoD rule that refuses an admin the carve-out it grants
        elsewhere is a bank-change rule. The score follows the code."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _bank_change(db, tenant.id, vendor, user, days_ago=10)

        result = VendorRiskService(db).score(vendor)

        assert "bank_changed_recently" in _codes(result)
        assert _points(result, "bank_changed_recently") == 25

    def test_an_old_change_does_not(self, db, tenant, make_user):
        """A score with no memory limit only ever climbs."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _bank_change(db, tenant.id, vendor, user, days_ago=200)

        result = VendorRiskService(db).score(vendor)

        assert "bank_changed_recently" not in _codes(result)

    def test_repeated_changes_are_their_own_factor(self, db, tenant, make_user):
        """One change is a vendor changing bank. Several is a pattern."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _bank_change(db, tenant.id, vendor, user, days_ago=10)
        _bank_change(db, tenant.id, vendor, user, days_ago=200)

        result = VendorRiskService(db).score(vendor)

        assert "bank_changed_repeatedly" in _codes(result)

    def test_a_change_that_never_took_effect_does_not_count(
        self, db, tenant, make_user
    ):
        """A rejected request is the control working, not a risk event.
        Counting it would score a vendor for something that did not happen."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _bank_change(db, tenant.id, vendor, user, days_ago=5,
                     state=BankChangeState.REJECTED)

        assert VendorRiskService(db).score(vendor)["score"] == 0


class TestStandingAndPaperwork:
    def test_blocked_scores_highest(self, db, tenant, make_user):
        """Somebody already made this call. The score must not quietly
        disagree with a decision that is on the record."""
        vendor = _vendor(db, tenant.id, status=VendorStatus.BLOCKED)

        result = VendorRiskService(db).score(vendor)

        assert _points(result, "blocked") == 40
        assert result["tier"] == "medium"

    def test_unverified_scores(self, db, tenant, make_user):
        vendor = _vendor(db, tenant.id, status=VendorStatus.PENDING_VERIFICATION)

        assert "not_verified" in _codes(VendorRiskService(db).score(vendor))

    def test_missing_tax_id_scores(self, db, tenant, make_user):
        """Not fraud. But it is how fraud stays invisible."""
        vendor = _vendor(db, tenant.id, tax_id=None)

        assert "no_tax_id" in _codes(VendorRiskService(db).score(vendor))

    def test_whitespace_is_not_a_tax_id(self, db, tenant, make_user):
        """A field somebody spacebarred past is empty."""
        vendor = _vendor(db, tenant.id, tax_id="   ")

        assert "no_tax_id" in _codes(VendorRiskService(db).score(vendor))


class TestBillingAndGoods:
    def test_flagged_duplicates_score(self, db, tenant, make_user):
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _flagged_invoice(db, tenant.id, vendor, user)

        assert "duplicate_invoices" in _codes(VendorRiskService(db).score(vendor))

    def test_duplicates_are_capped(self, db, tenant, make_user):
        """Ten flagged invoices is not twice the concern of five — it is the
        same concern with more instances. Without a cap one noisy vendor
        saturates the scale and every other signal stops mattering."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        for _ in range(10):
            _flagged_invoice(db, tenant.id, vendor, user)

        result = VendorRiskService(db).score(vendor)

        assert _points(result, "duplicate_invoices") == 15

    def test_a_vendor_attributable_return_scores(self, db, tenant, make_user):
        """VENDOR_ATTRIBUTABLE_REASONS exists so this question can be asked."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _return(db, tenant.id, vendor, user, REASON_DAMAGED)

        assert "goods_returned_vendor_fault" in _codes(
            VendorRiskService(db).score(vendor)
        )

    def test_our_own_mistake_does_not_score_the_vendor(
        self, db, tenant, make_user
    ):
        """A count correction is our error. Scoring a supplier for our own
        stocktaking is how a risk number stops meaning anything."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id)
        _return(db, tenant.id, vendor, user, REASON_COUNT_CORRECTION)

        assert VendorRiskService(db).score(vendor)["score"] == 0


class TestTenure:
    def test_a_new_vendor_scores_a_little(self, db, tenant, make_user):
        """No track record is not the same as a good one."""
        vendor = _vendor(db, tenant.id)
        vendor.created_at = _now() - timedelta(days=5)
        db.flush()

        assert "new_vendor" in _codes(VendorRiskService(db).score(vendor))

    def test_an_established_vendor_does_not(self, db, tenant, make_user):
        assert "new_vendor" not in _codes(
            VendorRiskService(db).score(_vendor(db, tenant.id))
        )


class TestTheScoreExplainsItself:
    """The property the whole design rests on. See the module docstring."""

    def test_every_point_is_attributable(self, db, tenant, make_user):
        """The score is the sum of its factors and nothing else. A score with
        unexplained points in it is the bare number this codebase already
        argues against."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, tax_id=None,
                         status=VendorStatus.PENDING_VERIFICATION)
        _bank_change(db, tenant.id, vendor, user, days_ago=3)
        _flagged_invoice(db, tenant.id, vendor, user)

        result = VendorRiskService(db).score(vendor)

        assert result["score"] == sum(f["points"] for f in result["factors"])
        assert result["score"] > 0

    def test_it_says_what_it_does_not_cover(self, db, tenant, make_user):
        """A 12 from two signals is not the same claim as a 12 from seven, and
        a reader cannot tell which they are holding unless it says."""
        result = VendorRiskService(db).score(_vendor(db, tenant.id))

        codes = {d["code"] for d in result["unscored_dimensions"]}
        assert "sanctions_screening" in codes
        assert "financial_health" in codes

    def test_the_cap_is_visible(self, db, tenant, make_user):
        """A 100 that was really a 140 should not be indistinguishable from
        one that landed exactly on the ceiling."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, status=VendorStatus.BLOCKED,
                         tax_id=None, bank_account_number=None)
        _bank_change(db, tenant.id, vendor, user, days_ago=1)
        _bank_change(db, tenant.id, vendor, user, days_ago=100)
        for _ in range(5):
            _flagged_invoice(db, tenant.id, vendor, user)
        for _ in range(5):
            _return(db, tenant.id, vendor, user, REASON_DAMAGED)

        result = VendorRiskService(db).score(vendor)

        assert result["score"] == MAX_SCORE
        assert result["capped"] is True
        assert result["raw_score"] > MAX_SCORE


class TestTiers:
    def test_the_boundaries_are_where_they_say(self):
        assert tier_for(0) == "low"
        assert tier_for(34) == "low"
        assert tier_for(35) == "medium"
        assert tier_for(69) == "medium"
        assert tier_for(70) == "high"


class TestPersisting:
    def test_refresh_writes_both_the_score_and_the_factors(
        self, db, tenant, make_user
    ):
        """risk_flags is where the factors belong — it has been on the model
        as JSON since the beginning, for exactly this."""
        user = make_user(UserRole.ADMIN)
        vendor = _vendor(db, tenant.id, tax_id=None)
        _bank_change(db, tenant.id, vendor, user, days_ago=2)

        VendorRiskService(db).refresh(vendor.id)
        db.refresh(vendor)

        assert vendor.risk_score > 0
        assert {f["code"] for f in vendor.risk_flags} == {
            "bank_changed_recently", "no_tax_id",
        }

    def test_a_vendor_that_is_not_there_returns_none(self, db, tenant):
        """Rather than raising. The caller is usually a bulk refresh."""
        assert VendorRiskService(db).refresh(uuid.uuid4()) is None

    def test_refresh_all_reports_what_moved(self, db, tenant, make_user):
        user = make_user(UserRole.ADMIN)
        clean = _vendor(db, tenant.id)
        risky = _vendor(db, tenant.id, tax_id=None)
        _bank_change(db, tenant.id, risky, user, days_ago=1)

        moved = VendorRiskService(db).refresh_all()
        db.refresh(clean)
        db.refresh(risky)

        assert moved == 1
        assert clean.risk_score == 0
        assert risky.risk_score > 0
