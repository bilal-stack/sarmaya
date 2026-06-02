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
