"""Change watchlist alerts.

Build Book differentiator: *vendor bank changes, master data edits, and policy
overrides trigger real-time alerts to a watchlist role.*

All three were already audited. The audit trail answers "what happened to this
record" for somebody who has already decided to look at that record — and none
of these three give anyone a reason to look. They share the property that makes
that dangerous: each changes where money goes, or who may authorise sending it,
without touching a single invoice. Somebody watching invoices sees nothing.
"""
import uuid

import pytest

from app.core.enums import UserRole, VendorStatus
from app.core.roles import (
    ROLE_PERMISSIONS, PERM_RECEIVE_WATCHLIST, PERM_MANAGE_VENDORS,
)
from app.models.vendor import Vendor
from app.models.watchlist_alert import (
    WatchlistAlert, CATEGORY_BANK_CHANGE, CATEGORY_MASTER_DATA, CATEGORY_POLICY,
)
from app.schemas.policy import ApprovalPolicyCreate, ApprovalRule
from app.schemas.vendor import VendorUpdate
from app.schemas.vendor_bank_change import BankChangeRequest
from app.services.notification_service import NotificationService
from app.services.policy_service import ApprovalPolicyService
from app.services.vendor_bank_service import VendorBankService
from app.services.vendor_service import VendorService
from app.services.watchlist_service import WatchlistService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    monkeypatch.setattr(NotificationService, "_deliver", lambda self, *a, **k: None)


@pytest.fixture
def sent(monkeypatch):
    """What actually left the building, since delivery is best-effort and
    swallows its own errors."""
    captured = []
    monkeypatch.setattr(
        NotificationService, "_send",
        lambda self, tenant_id, recipients, subject, body, category=None: captured.append((recipients, subject)),
    )
    return captured


def _vendor(db, tenant_id, name="Orion Supplies Ltd", **kwargs):
    vendor = Vendor(
        id=uuid.uuid4(), tenant_id=tenant_id, legal_name=name,
        status=VendorStatus.ACTIVE, **kwargs,
    )
    db.add(vendor)
    db.flush()
    return vendor


def _alerts(db, category=None):
    query = db.query(WatchlistAlert)
    if category:
        query = query.filter(WatchlistAlert.category == category)
    return query.all()


def _rule():
    return ApprovalRule(
        amount_threshold=250_000, operator="greater_than", required_role="cfo"
    )


class TestTheWatchlistRole:
    def test_oversight_roles_are_told(self):
        for role in (UserRole.ADMIN, UserRole.CFO, UserRole.AUDITOR):
            assert PERM_RECEIVE_WATCHLIST in ROLE_PERMISSIONS[role.value]

    def test_the_roles_that_make_these_changes_are_not(self):
        """An alert delivered to its own author is a log line. The clerk and
        manager who maintain vendors and policies are the subjects of the
        watchlist, not its audience."""
        for role in (UserRole.AP_CLERK, UserRole.MANAGER):
            assert PERM_MANAGE_VENDORS in ROLE_PERMISSIONS[role.value]
            assert PERM_RECEIVE_WATCHLIST not in ROLE_PERMISSIONS[role.value]


class TestTheThreeTriggers:
    """Exactly the three the Build Book names."""

    def test_a_bank_change_raises_one(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, iban="PK00OLD0000000000000001")

        VendorBankService(db).request_change(
            vendor.id,
            BankChangeRequest(reason="Vendor emailed new details on paper.",
                              iban="PK99NEW0000000000000009"),
            clerk,
        )

        alerts = _alerts(db, CATEGORY_BANK_CHANGE)
        assert len(alerts) == 1
        assert "Orion Supplies Ltd" in alerts[0].summary
        assert alerts[0].severity == "high"

    def test_a_master_data_edit_raises_one(self, db, tenant, make_user):
        """Bank fields cannot come through here — they have their own path —
        so this covers the name, code and tax id, any of which can quietly
        disguise a payee."""
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)

        VendorService(db).update_vendor(
            vendor.id, VendorUpdate(legal_name="0rion Supplies Ltd"), clerk
        )

        alerts = _alerts(db, CATEGORY_MASTER_DATA)
        assert len(alerts) == 1
        assert alerts[0].detail["before"]["legal_name"] == "Orion Supplies Ltd"
        assert alerts[0].detail["after"]["legal_name"] == "0rion Supplies Ltd"

    def test_a_policy_change_raises_one(self, db, tenant, make_user):
        """The rule deciding who may approve what. Editing it is how an amount
        threshold quietly stops applying."""
        admin = make_user(UserRole.ADMIN)
        ApprovalPolicyService(db).create_policy(
            ApprovalPolicyCreate(policy_name="Manager up to 250k", rule=_rule()), admin
        )

        alerts = _alerts(db, CATEGORY_POLICY)
        assert len(alerts) == 1
        assert "Manager up to 250k" in alerts[0].summary
        assert alerts[0].severity == "high"

    def test_a_policy_deletion_raises_one_too(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        service = ApprovalPolicyService(db)
        policy = service.create_policy(
            ApprovalPolicyCreate(policy_name="Doomed", rule=_rule()), admin
        )
        service.delete_policy(policy.id, admin, "Superseded by the tiered matrix.")

        events = {a.detail.get("event") for a in _alerts(db, CATEGORY_POLICY)}
        assert {"created", "deleted"} <= events


class TestTheAlertReachesSomebody:
    def test_the_watchers_are_emailed(self, db, tenant, make_user, sent):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        vendor = _vendor(db, tenant.id)

        VendorService(db).update_vendor(
            vendor.id, VendorUpdate(vendor_code="V-999"), clerk
        )

        assert sent, "a watchlist alert was raised but nobody was told"
        recipients = [r for group, _ in sent for r in group]
        assert cfo["email"] in recipients

    def test_the_person_who_made_the_change_is_not_emailed(
        self, db, tenant, make_user, sent
    ):
        """Telling somebody about their own action is noise, and noise is what
        stops people reading the alerts that matter."""
        admin = make_user(UserRole.ADMIN)   # holds the watchlist permission
        vendor = _vendor(db, tenant.id)

        VendorService(db).update_vendor(
            vendor.id, VendorUpdate(vendor_code="V-SELF"), admin
        )

        recipients = [r for group, _ in sent for r in group]
        assert admin["email"] not in recipients


class TestReviewingAnAlert:
    def _raise_one(self, db, tenant, actor):
        vendor = _vendor(db, tenant.id)
        VendorService(db).update_vendor(
            vendor.id, VendorUpdate(vendor_code="V-REVIEW"), actor
        )
        return _alerts(db, CATEGORY_MASTER_DATA)[0]

    def test_acknowledgement_records_who_and_what_they_concluded(
        self, db, tenant, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        alert = self._raise_one(db, tenant, clerk)

        reviewed = WatchlistService(db).acknowledge(
            alert.id, cfo, "Confirmed with procurement; the code was wrong."
        )

        assert str(reviewed.acknowledged_by) == str(cfo["id"])
        assert reviewed.acknowledged_at is not None
        assert "procurement" in reviewed.acknowledgement_note

    def test_whoever_made_the_change_cannot_sign_it_off(self, db, tenant, make_user):
        """The alert exists to put a second person in front of the change.
        Self-acknowledgement would let the one action the watchlist is for
        clear its own flag."""
        admin = make_user(UserRole.ADMIN)
        alert = self._raise_one(db, tenant, admin)

        with pytest.raises(PermissionError, match="somebody else"):
            WatchlistService(db).acknowledge(alert.id, admin, "Fine, it was me.")

    def test_it_cannot_be_reviewed_twice(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        admin = make_user(UserRole.ADMIN)
        alert = self._raise_one(db, tenant, clerk)

        WatchlistService(db).acknowledge(alert.id, cfo, "Checked.")
        with pytest.raises(ValueError, match="already been reviewed"):
            WatchlistService(db).acknowledge(alert.id, admin, "Checked again.")

    def test_an_operational_role_cannot_read_the_watchlist(
        self, db, tenant, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        with pytest.raises(PermissionError):
            WatchlistService(db).list_alerts(clerk)


class TestTheFeed:
    def test_open_only_hides_what_has_been_reviewed(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        service = VendorService(db)
        for code in ("V-1", "V-2"):
            service.update_vendor(
                _vendor(db, tenant.id, name=f"Vendor {code}").id,
                VendorUpdate(vendor_code=code), clerk,
            )
        alerts = _alerts(db, CATEGORY_MASTER_DATA)
        WatchlistService(db).acknowledge(alerts[0].id, cfo, "Checked.")

        watchlist = WatchlistService(db)
        assert watchlist.open_count(cfo) == 1
        assert len(watchlist.list_alerts(cfo, open_only=True)) == 1
        assert len(watchlist.list_alerts(cfo)) == 2

    def test_the_api_returns_the_feed_with_its_open_count(
        self, db, tenant, client, as_user, make_user
    ):
        clerk = make_user(UserRole.AP_CLERK)
        cfo = make_user(UserRole.CFO)
        vendor = _vendor(db, tenant.id)
        VendorService(db).update_vendor(
            vendor.id, VendorUpdate(vendor_code="V-API"), clerk
        )
        as_user(cfo)

        response = client.get("/api/v1/watchlist")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["open_count"] == 1
        assert body["items"][0]["category"] == CATEGORY_MASTER_DATA


class TestAccountNumbersAreMaskedHereToo:
    def test_a_bank_change_alert_does_not_carry_the_account(
        self, db, tenant, make_user
    ):
        """The auditor is on this list and cannot see full account numbers
        anywhere else — an alert would be an odd place to hand one over."""
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id, iban="PK36SCBL0000001123456702")

        VendorBankService(db).request_change(
            vendor.id,
            BankChangeRequest(reason="Vendor emailed new details on paper.",
                              iban="PK24ALFH0000009988776655"),
            clerk,
        )

        alert = _alerts(db, CATEGORY_BANK_CHANGE)[0]
        assert alert.detail["old_iban"] == "••••6702"
        assert alert.detail["new_iban"] == "••••6655"


class TestAnAlertNeverBreaksTheActionItDescribes:
    def test_a_failing_alert_does_not_roll_back_the_change(
        self, db, tenant, make_user, monkeypatch
    ):
        """A watchlist alert is a parallel observation, not a step in the
        action. A bank change that was correctly requested and approved must
        not fail because the telling did."""
        monkeypatch.setattr(
            WatchlistService, "_notify",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mail server down")),
        )
        clerk = make_user(UserRole.AP_CLERK)
        vendor = _vendor(db, tenant.id)

        updated = VendorService(db).update_vendor(
            vendor.id, VendorUpdate(vendor_code="V-RESILIENT"), clerk
        )

        assert updated.vendor_code == "V-RESILIENT"
