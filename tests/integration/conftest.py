"""Database-backed fixtures for integration tests.

Requires a live Postgres (models use Postgres-specific column types, so
SQLite is not an option). The test database is created from the SQLAlchemy
models via create_all — it does NOT touch the dev database.

Connection resolution order:
  1. TEST_DATABASE_URL env var, if set.
  2. settings.ADMIN_DATABASE_URL with the database name suffixed "_test".

The admin URL is used because these fixtures run DDL (create_all) and the
service-layer tests connect as the privileged role on purpose — they exercise
business logic, not RLS isolation (which is covered separately in
test_rls_isolation.py using the least-privilege os_app role).

If the database is unreachable, every integration test is skipped (not failed),
so the suite stays green in environments without Postgres.
"""
import os
import uuid

import pytest

# Ensure all models are registered on the shared metadata before create_all.
import app.models  # noqa: F401
from app.models.base import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.core.enums import UserRole


def _resolve_test_db_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        return url
    from app.core.config import settings
    base = settings.ADMIN_DATABASE_URL
    # Suffix the database name with _test so we never touch the dev DB.
    head, _, db_name = base.rpartition("/")
    return f"{head}/{db_name}_test"


@pytest.fixture(scope="session")
def db_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    url = _resolve_test_db_url()
    engine = create_engine(url, pool_pre_ping=True)
    try:
        conn = engine.connect()
        conn.close()
    except OperationalError as exc:
        pytest.skip(f"Test database not reachable at {url}: {exc}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    """A session wrapped in a transaction that is rolled back after each test,
    keeping tests isolated and the database clean."""
    from sqlalchemy.orm import sessionmaker

    connection = db_engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def tenant(db):
    t = Tenant(id=uuid.uuid4(), name="Test Co", slug=f"test-{uuid.uuid4().hex[:8]}")
    db.add(t)
    db.flush()
    return t


@pytest.fixture
def make_user(db, tenant):
    """Factory: create a user with a given role and return the
    current_user dict shape that the service layer expects."""
    def _make(role: UserRole, email: str | None = None) -> dict:
        u = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=email or f"{role.value}-{uuid.uuid4().hex[:6]}@test.com",
            password="x",
            role=role,
            is_active=True,
        )
        db.add(u)
        db.flush()
        return {
            "id": str(u.id),
            "tenant_id": str(tenant.id),
            "email": u.email,
            "role": role.value,
        }
    return _make


@pytest.fixture
def client(db):
    """FastAPI TestClient bound to the test session.

    The rest of this suite exercises the service layer directly, which cannot
    see how an endpoint is *wired* — whether a value arrives in the body or the
    query string, or which dependency guards it. That blind spot is what let a
    self-service role change (`PUT /auth/me?role=admin`) sit in the API, so
    anything about the HTTP contract itself belongs here.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.core.database import get_db
    from app.api.deps import get_db_session

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_db_session] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def as_user(db):
    """Authenticate the TestClient as a given user.

    Overrides both current-user dependencies: app.api.auth returns the User
    row, app.api.deps returns the dict the service layer takes.
    """
    from app.main import app
    from app.api.auth import get_current_user as auth_current_user
    from app.api.deps import get_current_user as deps_current_user

    def _as(user: dict):
        row = db.query(User).filter(User.id == user["id"]).first()
        app.dependency_overrides[auth_current_user] = lambda: row
        app.dependency_overrides[deps_current_user] = lambda: user
        return user

    return _as


@pytest.fixture
def other_tenant_user(db):
    """A user belonging to a *different* tenant.

    Exists to prove tenant scoping is enforced by the application, not assumed
    from RLS — these tests run against a create_all database, which has no RLS
    policies at all.
    """
    from app.models.tenant import Tenant

    other = Tenant(id=uuid.uuid4(), name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other)
    db.flush()

    u = User(
        id=uuid.uuid4(),
        tenant_id=other.id,
        email=f"outsider-{uuid.uuid4().hex[:6]}@other.com",
        password="x",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return {"id": str(u.id), "tenant_id": str(other.id), "email": u.email, "role": "admin"}
