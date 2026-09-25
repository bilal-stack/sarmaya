"""Changing how much an invoice may disagree with what arrived.

`get_match_tolerance` has said the values are "editable per tenant" since the
matching engine was written, and nothing routed to editing them — so every
tenant ran on the defaults and the only way to move them was a database write.

The tests that matter here are not the ones proving a number round-trips.
Widening this far enough turns three-way matching into a formality that passes
everything, so the load-bearing assertions are TestItRefusesToStopBeingAControl
— that there is a ceiling and it is a refusal rather than a warning — and
TestChangingItIsOnTheRecord, because a control being loosened is exactly the
change somebody asks about six months later.
"""
import uuid

import pytest

from app.core.enums import UserRole
from app.models.audit_log import AuditLog
from app.services.config_versioning import TYPE_MATCH_TOLERANCE
from app.services.match_tolerance_service import (
    MAX_PERCENT, MatchToleranceService,
)
from app.services.policy import DEFAULT_MATCH_TOLERANCE, get_match_tolerance

pytestmark = pytest.mark.integration


def _admin(make_user):
    return make_user(UserRole.ADMIN)


class TestReading:
    def test_a_tenant_that_has_not_chosen_gets_the_defaults(
        self, db, tenant, make_user
    ):
        result = MatchToleranceService(db).get(_admin(make_user))

        assert result["amount_percent"] == DEFAULT_MATCH_TOLERANCE["amount_percent"]
        assert result["quantity_percent"] == DEFAULT_MATCH_TOLERANCE["quantity_percent"]

    def test_it_says_whether_anybody_actually_chose(self, db, tenant, make_user):
        """A tenant running on defaults has not made a decision; one running on
        configured values has. On a control screen those are different states
        and the reader should not have to guess which they are looking at."""
        service = MatchToleranceService(db)
        user = _admin(make_user)

        assert service.get(user)["is_default"] is True
        service.set(1.0, 1.0, user)
        assert service.get(user)["is_default"] is False

    def test_the_missing_axes_are_stated_not_omitted(self, db, tenant, make_user):
        """The Build Book asks for a tolerance matrix. The engine applies one
        pair of numbers to every line of every invoice, and a screen that drew
        axes would imply a control the matching engine does not apply."""
        assert MatchToleranceService(db).get(_admin(make_user))["axes"] is None


class TestItRefusesToStopBeingAControl:
    """The ceiling, and why it is a refusal."""

    def test_it_refuses_a_tolerance_wide_enough_to_pass_anything(
        self, db, tenant, make_user
    ):
        """At 25% an invoice for a hundred boxes passes against a delivery of
        seventy-five — the discrepancy the control exists to catch, not the
        rounding it exists to forgive."""
        with pytest.raises(ValueError, match="cannot exceed"):
            MatchToleranceService(db).set(50.0, 5.0, _admin(make_user))

    def test_the_ceiling_itself_is_allowed(self, db, tenant, make_user):
        """A boundary that rejects its own stated limit is a different limit."""
        result = MatchToleranceService(db).set(
            MAX_PERCENT, MAX_PERCENT, _admin(make_user)
        )

        assert result["amount_percent"] == MAX_PERCENT

    def test_it_refuses_a_negative(self, db, tenant, make_user):
        with pytest.raises(ValueError, match="negative"):
            MatchToleranceService(db).set(-1.0, 5.0, _admin(make_user))

    def test_zero_is_allowed(self, db, tenant, make_user):
        """Strictest possible: every rounding difference fails the match. Some
        tenants genuinely want that, and refusing it would be this module
        deciding how strict a customer is allowed to be."""
        result = MatchToleranceService(db).set(0.0, 0.0, _admin(make_user))

        assert result["amount_percent"] == 0.0

    def test_a_refused_change_leaves_the_old_value_in_force(
        self, db, tenant, make_user
    ):
        """The important half of refusing. A rejected widening that had already
        half-applied would be worse than accepting it."""
        service = MatchToleranceService(db)
        user = _admin(make_user)
        service.set(3.0, 4.0, user)

        with pytest.raises(ValueError):
            service.set(99.0, 4.0, user)

        assert get_match_tolerance(db, tenant.id)["amount_percent"] == 3.0


class TestChangingItIsOnTheRecord:
    def test_it_writes_an_audit_row_with_both_values(
        self, db, tenant, make_user
    ):
        """Loosening a control is the change somebody asks about later, and
        "it was always like that" needs to be answerable."""
        user = _admin(make_user)
        MatchToleranceService(db).set(2.0, 5.0, user)
        MatchToleranceService(db).set(8.0, 9.0, user, reason="Supplier rounding")

        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "match_tolerance_configured")
            .all()
        )

        assert len(rows) == 2
        widened = [r for r in rows if (r.after_value or {}).get("amount_percent") == 8.0]
        assert widened, "the widening is not on the trail"
        assert widened[0].before_value["amount_percent"] == 2.0
        assert widened[0].comment == "Supplier rounding"

    def test_it_is_versioned_under_a_fixed_key(self, db, tenant, make_user):
        """A per-tenant singleton, so the whole history of how loose this got
        is one document — the same treatment autopilot settings get."""
        from app.models.config_version import ConfigVersion

        user = _admin(make_user)
        MatchToleranceService(db).set(1.0, 1.0, user)
        MatchToleranceService(db).set(2.0, 2.0, user)

        versions = (
            db.query(ConfigVersion)
            .filter(ConfigVersion.config_type == TYPE_MATCH_TOLERANCE)
            .all()
        )

        assert len(versions) == 2
        assert {v.config_key for v in versions} == {TYPE_MATCH_TOLERANCE}


class TestItReachesTheMatchingEngine:
    def test_what_was_saved_is_what_the_matcher_reads(
        self, db, tenant, make_user
    ):
        """The point of the whole change. A settings screen that writes a row
        the engine never consults is worse than no screen, because it looks
        like the control moved."""
        MatchToleranceService(db).set(7.5, 12.5, _admin(make_user))

        live = get_match_tolerance(db, tenant.id)

        assert live["amount_percent"] == 7.5
        assert live["quantity_percent"] == 12.5


class TestPermissions:
    def test_an_ordinary_role_cannot_read_or_change_it(
        self, db, tenant, make_user
    ):
        """Not a lighter gate because it is "only two numbers": this is the
        number that decides whether matching refuses an invoice."""
        clerk = {"id": str(uuid.uuid4()), "role": "ap_clerk", "tenant_id": tenant.id}

        with pytest.raises(PermissionError):
            MatchToleranceService(db).get(clerk)
        with pytest.raises(PermissionError):
            MatchToleranceService(db).set(1.0, 1.0, clerk)

    def test_the_api_refuses_a_clerk_and_serves_an_admin(
        self, client, as_user, make_user
    ):
        as_user(make_user(UserRole.AP_CLERK))
        assert client.get("/api/v1/config/match-tolerance").status_code == 403

        as_user(make_user(UserRole.ADMIN))
        assert client.get("/api/v1/config/match-tolerance").status_code == 200

    def test_the_api_refuses_a_tolerance_over_the_ceiling(
        self, client, as_user, make_user
    ):
        as_user(make_user(UserRole.ADMIN))

        response = client.put(
            "/api/v1/config/match-tolerance",
            json={"amount_percent": 80, "quantity_percent": 5},
        )

        assert response.status_code == 400
        assert "cannot exceed" in response.json()["detail"]
