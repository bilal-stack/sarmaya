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
                # Set PostgreSQL session variable for RLS (parameterized for security)
                session.execute(
                    text("SET LOCAL app.current_tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id}
                )
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