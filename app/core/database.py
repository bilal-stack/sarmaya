from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, with_loader_criteria
from typing import Generator
import logging

from app.core.config import settings
from app.models.base import mapper_registry

logger = logging.getLogger(__name__)

# Create engine
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # Verify connections before using
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG  # Log SQL queries in debug mode
)
engine.echo = False
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# RLS tenant context GUC. Read by the policies created in migration 003 via
# current_setting('app.current_tenant_id', TRUE).
TENANT_GUC = "app.current_tenant_id"


def set_tenant_context(session: Session, tenant_id: str) -> None:
    """Bind the RLS tenant for the lifetime of this Session.

    The tenant is stored on session.info and re-applied at the start of every
    transaction by the after_begin listener below. This is required because
    SQLAlchemy returns the connection to the pool at each transaction boundary:
    the service layer commits mid-request and then keeps working (refresh,
    audit insert) in a fresh transaction on a possibly different pooled
    connection. A one-shot SET would be lost there and the follow-up statements
    would run with no tenant — invisible (or rejected) under RLS.

    The GUC is set transaction-locally (set_config is_local => true), so it is
    discarded when the connection returns to the pool and can never leak to
    another tenant's request that later reuses that connection.
    """
    session.info["tenant_id"] = str(tenant_id)
    # Apply to the already-open transaction (after_begin won't fire for it).
    _apply_tenant(session, str(tenant_id))


def _apply_tenant(executor, tenant_id: str) -> None:
    executor.execute(
        text("SELECT set_config(:k, :v, true)"),
        {"k": TENANT_GUC, "v": tenant_id},
    )


@event.listens_for(Session, "after_begin")
def _bind_tenant_on_begin(session, transaction, connection):
    """Re-bind the tenant GUC whenever a new transaction starts on a session
    that has a tenant set, so the context survives mid-request commits."""
    tenant_id = session.info.get("tenant_id")
    if tenant_id:
        _apply_tenant(connection, tenant_id)


# ============================================
# APPLICATION-LEVEL TENANT SCOPING
# ============================================
#
# RLS is the primary tenant boundary, but it is created by migration 003 and so
# does not exist in a database built with create_all — which is every developer
# and test database in this project. That gap is not theoretical: the users
# endpoints were confirmed against a running dev server to list another
# tenant's staff and to apply a role change across tenants.
#
# Rather than add a tenant_id filter to each of the ~39 queries that touch a
# tenant-owned table — where the next query written silently misses it — the
# same restriction is applied once here, to every ORM SELECT issued on a
# session that has a tenant bound. New models and new queries are covered
# without anyone remembering to.
#
# This does not replace RLS. It is the second lock: RLS still protects against
# raw SQL and anything bypassing the ORM, and this protects every environment
# where RLS is absent.


def _tenant_scoped_mappers():
    """Mapped classes carrying a tenant_id column, resolved once on first use.

    Discovered from the registry rather than listed, so a new tenant-owned
    model is scoped the moment it is defined.
    """
    global _TENANT_MAPPERS
    if _TENANT_MAPPERS is None:
        _TENANT_MAPPERS = tuple(
            mapper.class_
            for mapper in mapper_registry.mappers
            if "tenant_id" in mapper.columns
        )
    return _TENANT_MAPPERS


_TENANT_MAPPERS = None


@event.listens_for(Session, "do_orm_execute")
def _scope_query_to_tenant(execute_state):
    """Restrict every ORM SELECT to the session's bound tenant.

    Skipped when no tenant is bound (unauthenticated startup work, migrations,
    provisioning scripts and the test fixtures that deliberately build several
    tenants), and for column/relationship refreshes, which reload rows that
    were already filtered when first fetched.
    """
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
    ):
        return

    tenant_id = execute_state.session.info.get("tenant_id")
    if not tenant_id:
        return

    for model in _tenant_scoped_mappers():
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                # Deliberately the eager expression form, not a lambda.
                # with_loader_criteria caches lambdas by code location, so a
                # closed-over tenant_id risks the first request's tenant being
                # baked into every later one — which would leak across tenants
                # far more severely than the gap this closes.
                model.tenant_id == tenant_id,
                include_aliases=True,
            )
        )


# ============================================
# TENANT CONNECTION FACTORY (Hybrid-Ready)
# ============================================

class TenantConnectionFactory:
    """
    Smart connection factory for multi-tenant isolation
    MVP: Uses RLS only
    Future: Can switch to schema or database per tenant
    """
    
    def __init__(self):
        self.default_engine = engine
        self.schema_engines = {}  # For future schema-per-tenant
        self.db_engines = {}      # For future db-per-tenant
    
    def get_session(
        self, 
        tenant_id: str, 
        isolation_level: str = "rls"
    ) -> Session:
        """
        Get database session with proper tenant isolation
        
        Args:
            tenant_id: UUID of tenant
            isolation_level: 'rls', 'schema', or 'database'
        
        Returns:
            SQLAlchemy Session with proper isolation
        """
        
        if isolation_level == "rls":
            # Use RLS (MVP approach)
            session = SessionLocal()
            try:
                set_tenant_context(session, tenant_id)
                return session
            except Exception as e:
                session.close()
                logger.error(f"Failed to set RLS context: {e}")
                raise
        
        elif isolation_level == "schema":
            # Future: Schema-per-tenant
            raise NotImplementedError("Schema isolation not yet implemented")
        
        elif isolation_level == "database":
            # Future: Database-per-tenant
            raise NotImplementedError("Database isolation not yet implemented")
        
        else:
            raise ValueError(f"Invalid isolation_level: {isolation_level}")


# Global factory instance
connection_factory = TenantConnectionFactory()


# ============================================
# SESSION MANAGEMENT
# ============================================

def get_db() -> Generator[Session, None, None]:
    """
    Standard database session (no RLS context)
    Use for: Authentication, tenant lookup, system operations
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_with_rls(tenant_id: str) -> Generator[Session, None, None]:
    """
    Database session with RLS context set
    Use for: All tenant-scoped operations
    
    Args:
        tenant_id: UUID string of tenant
    """
    session = connection_factory.get_session(tenant_id, isolation_level="rls")
    try:
        yield session
    finally:
        # RLS context is automatically reset when session closes
        session.close()


# ============================================
# CONNECTION POOL MONITORING (Optional)
# ============================================

@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """Log new connections"""
    if settings.DEBUG:
        logger.debug("New database connection established")


@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_conn, connection_record, connection_proxy):
    """Verify connection is valid before use"""
    if settings.DEBUG:
        logger.debug("Connection checked out from pool")