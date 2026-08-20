"""Org units: the scopes a role is exercised within.

Build Book, Access Controls: *"RBAC with scopes: tenant, business unit,
location, cost center, project."* And the entity list: `OrgUnit(org_unit_id,
tenant_id, type, parent_id)` — "business unit, location, department hierarchy".

Until now a role was tenant-wide: a manager who only runs the Karachi warehouse
approved invoices for every site, and an auditor scoped to one business unit
read the whole company. Permissions said what you may *do*; nothing said what
you may do it *to*.

Two decisions shape everything here:

  * **One table with a `unit_type`, not four tables.** The spec asks for a
    hierarchy, and business unit → location → cost centre is that hierarchy
    rather than four unrelated lists. A project hangs off whichever unit owns
    it. Separate tables would need four sets of scope rows and four joins to
    answer one question.
  * **No scope means no restriction.** A user with no rows in
    `user_org_scopes` sees the whole tenant, exactly as before. That keeps the
    feature inert until somebody configures it — the alternative, where adding
    a table silently hides every record from everybody, is the kind of change
    that gets discovered by a CFO rather than by a test.
"""
from sqlalchemy import Column, String, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.models.base import BaseModel

#: What a unit is. The Build Book's list, plus department, which is what most
#: clients actually call the layer between a business unit and a cost centre.
UNIT_BUSINESS = "business_unit"
UNIT_LOCATION = "location"
UNIT_DEPARTMENT = "department"
UNIT_COST_CENTER = "cost_center"
UNIT_PROJECT = "project"

UNIT_TYPES = (
    UNIT_BUSINESS, UNIT_LOCATION, UNIT_DEPARTMENT, UNIT_COST_CENTER, UNIT_PROJECT,
)


class OrgUnit(BaseModel):
    __tablename__ = "org_units"

    OBJECT_TYPE = "org_unit"
    REFERENCE_FIELD = "code"

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: What a person quotes: "CC-OPS", "KHI", "BU-RETAIL".
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    unit_type = Column(String(30), nullable=False, index=True)

    #: The hierarchy. Scoping somebody to a parent scopes them to everything
    #: beneath it, which is what people mean by "she runs the north region".
    parent_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id", ondelete="RESTRICT"),
        nullable=True, index=True,
    )

    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    parent = relationship("OrgUnit", remote_side="OrgUnit.id", backref="children")
    tenant = relationship("Tenant", backref="org_units")

    __table_args__ = (
        # A code is how people refer to a unit out loud, so two units sharing
        # one inside a tenant makes every conversation ambiguous.
        UniqueConstraint("tenant_id", "code", name="uq_org_units_tenant_code"),
    )


class UserOrgScope(BaseModel):
    """Which units a user may act within.

    A row grants the unit *and everything under it*. No rows at all grants the
    whole tenant — see the module docstring for why that is the default rather
    than the other way round.
    """
    __tablename__ = "user_org_scopes"

    #: Never filtered by org scope itself — the query that resolves a user's
    #: scope reads this table, so scoping it would be circular.
    ORG_SCOPE_EXEMPT = True

    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    org_unit_id = Column(
        UUID(as_uuid=True), ForeignKey("org_units.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    user = relationship("User", backref="org_scopes")
    org_unit = relationship("OrgUnit", backref="user_scopes")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "org_unit_id", name="uq_user_org_scopes_user_unit"
        ),
    )
