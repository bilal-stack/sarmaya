from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session
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