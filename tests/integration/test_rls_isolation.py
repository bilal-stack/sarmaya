"""Proves Postgres Row Level Security actually isolates tenants.

The other integration tests connect as the privileged role and therefore
*bypass* RLS — they validate service/business logic. This module is the only
one that connects as the least-privilege ``os_app`` role (NOSUPERUSER,
NOBYPASSRLS) so the RLS policies from migration 003/005 are enforced. It
verifies that a tenant-scoped session can only see/insert its own rows.

Setup data is created with the admin connection (which bypasses RLS, so it can
seed multiple tenants); the assertions run on the app connection.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.models.tenant import Tenant
from app.models.vendor import Vendor
from app.core.enums import VendorStatus

pytestmark = pytest.mark.integration


def _swap_to_test_db(url: str) -> str:
    """Point the least-privilege role's URL at whichever database the suite is
    running against.

    The database name is taken from TEST_DATABASE_URL when it is set, so this
    module follows the rest of the suite — running the tests against a database
    built by `alembic upgrade head` is how the migrations get proven, and these
    two tests silently read from a different database until this honoured it.
    The credentials stay those of `os_app`: the whole point here is connecting
    as the role that cannot bypass RLS.
    """
    head, _, db = url.rpartition("/")
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        db = override.rpartition("/")[2]
    else:
        db = f"{db}_test"
    return f"{head}/{db}"


def _ensure_vendor_rls(admin_conn) -> None:
    """create_all() builds the schema but not RLS; mirror migration 003/005 on
    the vendors table so this test exercises the real policies."""
    admin_conn.execute(text("ALTER TABLE vendors ENABLE ROW LEVEL SECURITY"))
    admin_conn.execute(text("ALTER TABLE vendors FORCE ROW LEVEL SECURITY"))
    admin_conn.execute(text("DROP POLICY IF EXISTS vendors_tenant_isolation ON vendors"))
    admin_conn.execute(text("DROP POLICY IF EXISTS vendors_tenant_insert ON vendors"))
    admin_conn.execute(text(
        "CREATE POLICY vendors_tenant_isolation ON vendors USING "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    admin_conn.execute(text(
        "CREATE POLICY vendors_tenant_insert ON vendors FOR INSERT WITH CHECK "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    # The vendors table is (re)created per session by create_all as the admin
    # role; make sure the app role can reach it.
    admin_conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON vendors TO os_app"))


@pytest.fixture
def rls(db_engine):
    """Seeds two tenants (A, B) each with one vendor via the admin connection,
    yields the ids plus an os_app-bound session factory, then cleans up."""
    app_url = _swap_to_test_db(settings.DATABASE_URL)
    app_engine = create_engine(app_url, pool_pre_ping=True)

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    vendor_a = uuid.uuid4()
    vendor_b = uuid.uuid4()

    AdminSession = sessionmaker(bind=db_engine, autoflush=False)
    setup = AdminSession()
    try:
        _ensure_vendor_rls(setup.connection())
        setup.add_all([
            Tenant(id=tenant_a, name="Tenant A", slug=f"a-{tenant_a.hex[:8]}"),
            Tenant(id=tenant_b, name="Tenant B", slug=f"b-{tenant_b.hex[:8]}"),
        ])
        setup.flush()
        setup.add_all([
            Vendor(id=vendor_a, tenant_id=tenant_a, legal_name="Vendor A", status=VendorStatus.ACTIVE),
            Vendor(id=vendor_b, tenant_id=tenant_b, legal_name="Vendor B", status=VendorStatus.ACTIVE),
        ])
        setup.commit()
    except Exception:
        setup.rollback()
        setup.close()
        app_engine.dispose()
        raise

    AppSession = sessionmaker(bind=app_engine, autoflush=False)
    try:
        yield {
            "AppSession": AppSession,
            "tenant_a": tenant_a, "tenant_b": tenant_b,
            "vendor_a": vendor_a, "vendor_b": vendor_b,
        }
    finally:
        # Remove the committed seed rows so the shared test DB stays clean.
        setup.execute(text("DELETE FROM vendors WHERE id IN (:a, :b)"),
                      {"a": str(vendor_a), "b": str(vendor_b)})
        setup.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                      {"a": str(tenant_a), "b": str(tenant_b)})
        setup.commit()
        setup.close()
        app_engine.dispose()


def test_app_role_is_not_superuser_or_bypassrls(rls):
    s = rls["AppSession"]()
    try:
        row = s.execute(text(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        )).fetchone()
        assert row.rolsuper is False
        assert row.rolbypassrls is False
    finally:
        s.close()


def test_tenant_sees_only_its_own_vendor(rls):
    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_a"]))
        ids = {r.id for r in s.query(Vendor).all()}
        assert rls["vendor_a"] in ids
        assert rls["vendor_b"] not in ids
    finally:
        s.close()

    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_b"]))
        ids = {r.id for r in s.query(Vendor).all()}
        assert rls["vendor_b"] in ids
        assert rls["vendor_a"] not in ids
    finally:
        s.close()


def test_no_tenant_context_sees_nothing(rls):
    s = rls["AppSession"]()
    try:
        visible = s.query(Vendor).filter(
            Vendor.id.in_([rls["vendor_a"], rls["vendor_b"]])
        ).count()
        assert visible == 0
    finally:
        s.close()


def test_insert_for_other_tenant_is_blocked(rls):
    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_a"]))
        # Context is tenant A but we try to write a row owned by tenant B.
        s.add(Vendor(
            id=uuid.uuid4(), tenant_id=rls["tenant_b"],
            legal_name="Smuggled", status=VendorStatus.ACTIVE,
        ))
        with pytest.raises(DBAPIError):
            s.flush()
    finally:
        s.rollback()
        s.close()
