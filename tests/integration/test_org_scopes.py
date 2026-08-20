"""RBAC scopes: what a role may act *on*, not just what it may do.

Build Book, Access Controls: *"RBAC with scopes: tenant, business unit,
location, cost center, project."* Until this existed a manager who ran one
warehouse approved invoices for every site.

The two defaults are what these tests are really about, because getting either
backwards is silent and severe:

  * a user with **no scope** sees the whole tenant, so the feature is inert
    until configured rather than hiding everything from everybody;
  * a record with **no unit** stays visible, so existing data does not vanish
    the moment one person is given a scope.

Between them sits the actual control, and the hierarchy: a scope grants a unit
and everything beneath it.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.database import set_org_scope
from app.core.enums import InvoiceState, UserRole
from app.models.audit_log import AuditLog
from app.models.invoice import Invoice
from app.models.org_unit import (
    OrgUnit, UserOrgScope, UNIT_BUSINESS, UNIT_COST_CENTER, UNIT_LOCATION,
)
from app.services.org_unit_service import OrgUnitService

pytestmark = pytest.mark.integration


def _unit(db, tenant_id, code, unit_type=UNIT_COST_CENTER, parent=None):
    unit = OrgUnit(
        id=uuid.uuid4(), tenant_id=tenant_id, code=code, name=f"{code} unit",
        unit_type=unit_type, parent_id=parent.id if parent else None,
    )
    db.add(unit)
    db.flush()
    return unit


def _invoice(db, tenant_id, created_by, unit=None, number=None):
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant_id,
        invoice_number=number or f"INV-{uuid.uuid4().hex[:6]}",
        vendor_name="Acme", invoice_date=date(2026, 8, 1),
        total_amount=Decimal("1000"), current_state=InvoiceState.APPROVED,
        created_by=created_by, org_unit_id=unit.id if unit else None,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _scope(db, tenant_id, user, unit):
    db.add(UserOrgScope(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=user["id"], org_unit_id=unit.id
    ))
    db.flush()


class TestTheDefaults:
    """Both of these are the difference between a useful control and an
    outage, and neither is visible until somebody is scoped."""

    def test_a_user_with_no_scope_sees_everything(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        _invoice(db, tenant.id, admin["id"], unit=khi)
        _invoice(db, tenant.id, admin["id"], unit=None)

        assert OrgUnitService(db).effective_scope(admin["id"]) is None
        assert db.query(Invoice).count() == 2

    def test_no_scope_is_none_not_an_empty_list(self, db, tenant, make_user):
        """None means unrestricted; an empty list would mean nothing at all.
        Returning the wrong one either removes the control or locks everybody
        out, and both look like a working system until somebody looks."""
        admin = make_user(UserRole.ADMIN)

        assert OrgUnitService(db).effective_scope(admin["id"]) is None

    def test_a_record_with_no_unit_stays_visible_to_a_scoped_user(
        self, db, tenant, make_user
    ):
        """Otherwise every invoice raised before scopes existed disappears the
        moment one person is given one."""
        admin = make_user(UserRole.ADMIN)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        _scope(db, tenant.id, admin, khi)
        _invoice(db, tenant.id, admin["id"], unit=None, number="INV-UNSCOPED")

        set_org_scope(db, [khi.id])
        try:
            found = {i.invoice_number for i in db.query(Invoice).all()}
        finally:
            set_org_scope(db, None)

        assert "INV-UNSCOPED" in found


class TestTheControl:
    def test_a_scoped_user_does_not_see_another_units_records(
        self, db, tenant, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        lhe = _unit(db, tenant.id, "LHE", UNIT_LOCATION)
        _invoice(db, tenant.id, admin["id"], unit=khi, number="INV-KHI")
        _invoice(db, tenant.id, admin["id"], unit=lhe, number="INV-LHE")

        set_org_scope(db, [khi.id])
        try:
            found = {i.invoice_number for i in db.query(Invoice).all()}
        finally:
            set_org_scope(db, None)

        assert "INV-KHI" in found
        assert "INV-LHE" not in found

    def test_the_filter_applies_to_a_plain_query_too(self, db, tenant, make_user):
        """Global, like the tenant filter. A query written tomorrow in a module
        that knows nothing about scopes is still scoped."""
        admin = make_user(UserRole.ADMIN)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        lhe = _unit(db, tenant.id, "LHE", UNIT_LOCATION)
        other = _invoice(db, tenant.id, admin["id"], unit=lhe)

        set_org_scope(db, [khi.id])
        try:
            assert db.query(Invoice).filter(Invoice.id == other.id).first() is None
        finally:
            set_org_scope(db, None)


class TestTheHierarchy:
    def test_a_scope_grants_everything_beneath_it(self, db, tenant, make_user):
        """What people mean by "she runs the north region" — nobody expects to
        enumerate every cost centre under it."""
        admin = make_user(UserRole.ADMIN)
        region = _unit(db, tenant.id, "NORTH", UNIT_BUSINESS)
        site = _unit(db, tenant.id, "KHI", UNIT_LOCATION, parent=region)
        centre = _unit(db, tenant.id, "CC-OPS", UNIT_COST_CENTER, parent=site)
        _scope(db, tenant.id, admin, region)

        scope = OrgUnitService(db).effective_scope(admin["id"])

        assert set(scope) == {region.id, site.id, centre.id}

    def test_a_scope_does_not_grant_a_sibling(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        region = _unit(db, tenant.id, "NORTH", UNIT_BUSINESS)
        mine = _unit(db, tenant.id, "KHI", UNIT_LOCATION, parent=region)
        theirs = _unit(db, tenant.id, "LHE", UNIT_LOCATION, parent=region)
        _scope(db, tenant.id, admin, mine)

        scope = OrgUnitService(db).effective_scope(admin["id"])

        assert mine.id in scope
        assert theirs.id not in scope

    def test_a_new_child_is_covered_without_reassigning_anybody(
        self, db, tenant, make_user
    ):
        """The reason the closure is resolved per request rather than stored:
        a stored one would need a data migration every time a site opened, and
        the user it missed would silently stop seeing it."""
        admin = make_user(UserRole.ADMIN)
        region = _unit(db, tenant.id, "NORTH", UNIT_BUSINESS)
        _scope(db, tenant.id, admin, region)
        assert len(OrgUnitService(db).effective_scope(admin["id"])) == 1

        opened_later = _unit(db, tenant.id, "ISB", UNIT_LOCATION, parent=region)

        assert opened_later.id in OrgUnitService(db).effective_scope(admin["id"])


class TestManagingScopes:
    def test_assigning_is_audited_under_the_administrator(self, db, tenant, make_user):
        """Narrowing or widening what somebody can see is an access-control
        change, and those are what an auditor asks about."""
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)

        OrgUnitService(db).assign(target["id"], khi.id, admin)

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == target["id"], AuditLog.action == "scope_granted"
        ).first()
        assert entry is not None
        assert str(entry.user_id) == str(admin["id"])
        assert "KHI" in entry.comment

    def test_revoking_the_last_scope_says_it_widens_access(
        self, db, tenant, make_user
    ):
        """It surprises people: removing the last scope means the whole tenant,
        not nothing. The trail says so rather than leaving it to be found."""
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        OrgUnitService(db).assign(target["id"], khi.id, admin)

        OrgUnitService(db).revoke(target["id"], khi.id, admin)

        entry = db.query(AuditLog).filter(
            AuditLog.object_id == target["id"], AuditLog.action == "scope_revoked"
        ).first()
        assert "whole tenant" in entry.comment
        assert OrgUnitService(db).effective_scope(target["id"]) is None

    def test_assigning_twice_is_harmless(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)

        OrgUnitService(db).assign(target["id"], khi.id, admin)
        OrgUnitService(db).assign(target["id"], khi.id, admin)

        assert len(OrgUnitService(db).effective_scope(target["id"])) == 1

    def test_an_ordinary_role_cannot_assign_scopes(self, db, tenant, make_user):
        """Granting yourself a wider scope would be granting yourself access."""
        clerk = make_user(UserRole.AP_CLERK)
        target = make_user(UserRole.MANAGER)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)

        with pytest.raises(PermissionError):
            OrgUnitService(db).assign(target["id"], khi.id, clerk)

    def test_a_duplicate_code_is_refused(self, db, tenant, make_user):
        """A code is how people refer to a unit out loud."""
        admin = make_user(UserRole.ADMIN)
        OrgUnitService(db).create_unit(
            admin, code="KHI", name="Karachi", unit_type=UNIT_LOCATION
        )

        with pytest.raises(ValueError, match="already exists"):
            OrgUnitService(db).create_unit(
                admin, code="KHI", name="Karachi Warehouse", unit_type=UNIT_LOCATION
            )

    def test_an_unknown_unit_type_is_refused(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)

        with pytest.raises(ValueError, match="org unit type"):
            OrgUnitService(db).create_unit(
                admin, code="X", name="X", unit_type="department_ish"
            )


class TestTheApi:
    def test_an_administrator_can_build_the_org_chart(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        as_user(admin)

        created = client.post("/api/v1/org-units", json={
            "code": "BU-RETAIL", "name": "Retail", "unit_type": "business_unit",
        })
        assert created.status_code == 201, created.text

        listed = client.get("/api/v1/org-units")
        assert listed.status_code == 200
        assert "BU-RETAIL" in {u["code"] for u in listed.json()}

    def test_scopes_are_visible_for_a_user(
        self, db, tenant, client, as_user, make_user
    ):
        admin = make_user(UserRole.ADMIN)
        target = make_user(UserRole.MANAGER)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        as_user(admin)

        assigned = client.post(
            f"/api/v1/org-units/users/{target['id']}/scopes",
            json={"org_unit_id": str(khi.id)},
        )
        assert assigned.status_code == 200, assigned.text

        scopes = client.get(f"/api/v1/org-units/users/{target['id']}/scopes")
        assert [u["code"] for u in scopes.json()] == ["KHI"]

    def test_a_scoped_user_gets_a_scoped_invoice_list(
        self, db, tenant, client, as_user, make_user
    ):
        """Through the real endpoint, not a hand-bound session.

        The service tests above bind the scope themselves, so they say nothing
        about whether a *request* binds it. If the dependency were wrong the
        feature would be dead in production with the whole suite green — which
        is what the client fixture did until it resolved scopes too.
        """
        admin = make_user(UserRole.ADMIN)
        khi = _unit(db, tenant.id, "KHI", UNIT_LOCATION)
        lhe = _unit(db, tenant.id, "LHE", UNIT_LOCATION)
        _invoice(db, tenant.id, admin["id"], unit=khi, number="INV-MINE")
        _invoice(db, tenant.id, admin["id"], unit=lhe, number="INV-THEIRS")
        _scope(db, tenant.id, admin, khi)
        db.commit()
        as_user(admin)

        listed = client.get("/api/v1/invoices")
        assert listed.status_code == 200, listed.text
        numbers = {i["invoice_number"] for i in listed.json()}

        assert "INV-MINE" in numbers
        assert "INV-THEIRS" not in numbers
