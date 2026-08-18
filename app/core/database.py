from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, Session, with_loader_criteria
from typing import Generator
import logging

from app.core.config import settings
from app.models.base import mapper_registry

logger = logging.getLogger(__name__)

# Every timestamp column in this schema is TIMESTAMP WITHOUT TIME ZONE and holds
# UTC. Application code writes them with utc_now(), which returns an *aware*
# datetime — and Postgres converts an aware value into the session's zone before
# dropping the offset to fit a naive column. On a server set to Asia/Karachi
# that stored local time in columns everything else reads as UTC: released_at
# and created_at on the same payment row landed five hours apart, and the audit
# log's timestamp disagreed with its own created_at.
#
# DR-012 fixed the mirror image of this on the server-side defaults; this is the
# client-side half. Pinning the session's zone corrects every write at once,
# rather than depending on ~40 call sites remembering to strip the offset — the
# same argument as DR-013. Server defaults are already zone-independent
# (timezone('utc', now())), so they are unaffected.
#
# Exported so the test fixtures build their engine the same way: a database
# session in this project speaks UTC, and that must not be a property only the
# production engine happens to have.
ENGINE_CONNECT_ARGS = {"options": "-c timezone=UTC"}

# Create engine
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # Verify connections before using
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,  # Log SQL queries in debug mode
    connect_args=ENGINE_CONNECT_ARGS,
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


class HardDeleteRefused(RuntimeError):
    """Something tried to destroy a record the audit trail still refers to."""


_SOFT_DELETE_MAPPERS = None


def _soft_delete_mappers():
    """Every mapped class carrying SoftDeleteMixin, discovered once."""
    global _SOFT_DELETE_MAPPERS
    if _SOFT_DELETE_MAPPERS is None:
        from app.models.base import SoftDeleteMixin

        _SOFT_DELETE_MAPPERS = [
            mapper.class_
            for mapper in mapper_registry.mappers
            if issubclass(mapper.class_, SoftDeleteMixin)
        ]
    return _SOFT_DELETE_MAPPERS


#: Set on session.info to see withdrawn rows — the audit trail and evidence
#: packs must still resolve what a deletion event refers to, which is the whole
#: reason the row was kept.
INCLUDE_DELETED = "include_deleted"


def include_deleted(session: Session, value: bool = True) -> None:
    session.info[INCLUDE_DELETED] = value


@event.listens_for(Session, "do_orm_execute")
def _exclude_soft_deleted(execute_state):
    """Hide withdrawn rows from every ORM SELECT.

    Same mechanism and the same argument as the tenant scoping above: one rule
    applied centrally beats ~40 call sites remembering a filter, and the one
    that forgets is the one that shows a deleted vendor in a payment run.

    Deliberately *not* conditional on a bound tenant, unlike tenant scoping.
    That filter is a safety net over RLS, which already enforces it in the
    database; there is no second mechanism behind this one.
    """
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
    ):
        return
    if execute_state.session.info.get(INCLUDE_DELETED):
        return

    for model in _soft_delete_mappers():
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                model,
                model.deleted_at.is_(None),
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def _refuse_hard_deletes(session, flush_context, instances):
    """The guardrail the Build Book asks for, at the last point it can be applied.

    Withdrawing a record is a service-layer decision that writes an audit entry
    and a reason. Nothing should reach the database as a DELETE for these
    models — not a stray `session.delete()`, not a cascade, not a cleanup
    script that seemed harmless. Raising here makes that structural rather than
    a convention people have to remember, and the error names the alternative
    so whoever hits it is not left guessing.
    """
    if not session.deleted:
        return
    from app.models.base import SoftDeleteMixin

    for obj in session.deleted:
        if isinstance(obj, SoftDeleteMixin):
            raise HardDeleteRefused(
                f"{type(obj).__name__} cannot be hard-deleted: the audit trail "
                "would keep an entry pointing at a row that no longer exists. "
                "Withdraw it instead (soft delete with a reason), which keeps "
                "the record resolvable and out of every query."
            )


# ============================================
# TENANT ISOLATION STRATEGY
# ============================================
#
# One strategy is implemented: shared tables, isolated by RLS (migration 003)
# and by the application-level scoping above. `tenants.isolation_level` records
# the intended strategy per tenant and every tenant is 'rls'.
#
# A TenantConnectionFactory previously stood here offering 'schema' and
# 'database' alongside it, both raising NotImplementedError. Nothing ever
# called it, and its 'rls' branch merely did what get_db_session already does.
# It was removed rather than completed: returning a session pointed at a schema
# or database that no one provisions or migrates would be worse than not
# offering the option. See DR-014 for what implementing either would require.


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