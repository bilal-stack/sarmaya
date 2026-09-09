"""Proves Postgres RLS isolates conversation_messages across tenants.

conversation_messages was the last tenant-scoped table to get a tenant_id +
RLS policies (migration 006). Before that, isolation relied on application-layer
parent-conversation ownership checks, which get_conversation_history bypassed by
querying messages directly. This module connects as the least-privilege
``os_app`` role (NOSUPERUSER, NOBYPASSRLS) and verifies the DB-layer policies
actually hold.

Setup data is seeded with the admin connection (bypasses RLS, so it can write
rows for multiple tenants); the assertions run on the app connection.
"""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import set_tenant_context
from app.core.enums import UserRole
from app.models.tenant import Tenant
from app.models.user import User
from app.models.conversation import Conversation, ConversationMessage

from tests.integration.conftest import app_role_url_for_test_db

pytestmark = pytest.mark.integration




def _ensure_message_rls(admin_conn) -> None:
    """create_all() builds the schema but not RLS; mirror migration 006 on
    conversation_messages so this test exercises the real policies."""
    admin_conn.execute(text("ALTER TABLE conversation_messages ENABLE ROW LEVEL SECURITY"))
    admin_conn.execute(text("ALTER TABLE conversation_messages FORCE ROW LEVEL SECURITY"))
    admin_conn.execute(text(
        "DROP POLICY IF EXISTS conversation_messages_tenant_isolation ON conversation_messages"
    ))
    admin_conn.execute(text(
        "DROP POLICY IF EXISTS conversation_messages_tenant_insert ON conversation_messages"
    ))
    admin_conn.execute(text(
        "CREATE POLICY conversation_messages_tenant_isolation ON conversation_messages USING "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    admin_conn.execute(text(
        "CREATE POLICY conversation_messages_tenant_insert ON conversation_messages "
        "FOR INSERT WITH CHECK "
        "(tenant_id::text = current_setting('app.current_tenant_id', TRUE))"
    ))
    # create_all (re)creates the table as the admin role each session; make sure
    # the app role can reach it.
    admin_conn.execute(text(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_messages TO os_app"
    ))


@pytest.fixture
def rls(db_engine):
    """Seeds two tenants (A, B), each with a user, a conversation, and one
    message, via the admin connection; yields the ids plus an os_app-bound
    session factory, then cleans up."""
    app_url = app_role_url_for_test_db(settings.DATABASE_URL)
    app_engine = create_engine(app_url, pool_pre_ping=True)

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    conv_a = uuid.uuid4()
    conv_b = uuid.uuid4()
    msg_a = uuid.uuid4()
    msg_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    AdminSession = sessionmaker(bind=db_engine, autoflush=False)
    setup = AdminSession()
    try:
        _ensure_message_rls(setup.connection())
        setup.add_all([
            Tenant(id=tenant_a, name="Tenant A", slug=f"a-{tenant_a.hex[:8]}"),
            Tenant(id=tenant_b, name="Tenant B", slug=f"b-{tenant_b.hex[:8]}"),
        ])
        setup.flush()
        setup.add_all([
            User(id=user_a, tenant_id=tenant_a, email=f"a-{user_a.hex[:8]}@x.io",
                 password="x", role=UserRole.USER),
            User(id=user_b, tenant_id=tenant_b, email=f"b-{user_b.hex[:8]}@x.io",
                 password="x", role=UserRole.USER),
        ])
        setup.flush()
        setup.add_all([
            Conversation(id=conv_a, tenant_id=tenant_a, user_id=user_a, title="A"),
            Conversation(id=conv_b, tenant_id=tenant_b, user_id=user_b, title="B"),
        ])
        setup.flush()
        setup.add_all([
            ConversationMessage(id=msg_a, tenant_id=tenant_a, conversation_id=conv_a,
                                role="user", content="hello from A"),
            ConversationMessage(id=msg_b, tenant_id=tenant_b, conversation_id=conv_b,
                                role="user", content="hello from B"),
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
            "conv_a": conv_a, "conv_b": conv_b,
            "msg_a": msg_a, "msg_b": msg_b,
        }
    finally:
        # Remove the committed seed rows so the shared test DB stays clean.
        setup.execute(text("DELETE FROM conversation_messages WHERE id IN (:a, :b)"),
                      {"a": str(msg_a), "b": str(msg_b)})
        setup.execute(text("DELETE FROM conversations WHERE id IN (:a, :b)"),
                      {"a": str(conv_a), "b": str(conv_b)})
        setup.execute(text("DELETE FROM users WHERE id IN (:a, :b)"),
                      {"a": str(user_a), "b": str(user_b)})
        setup.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"),
                      {"a": str(tenant_a), "b": str(tenant_b)})
        setup.commit()
        setup.close()
        app_engine.dispose()


def test_tenant_sees_only_its_own_message(rls):
    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_a"]))
        ids = {r.id for r in s.query(ConversationMessage).all()}
        assert rls["msg_a"] in ids
        assert rls["msg_b"] not in ids
    finally:
        s.close()

    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_b"]))
        ids = {r.id for r in s.query(ConversationMessage).all()}
        assert rls["msg_b"] in ids
        assert rls["msg_a"] not in ids
    finally:
        s.close()


def test_no_tenant_context_sees_no_messages(rls):
    s = rls["AppSession"]()
    try:
        visible = s.query(ConversationMessage).filter(
            ConversationMessage.id.in_([rls["msg_a"], rls["msg_b"]])
        ).count()
        assert visible == 0
    finally:
        s.close()


def test_history_query_cannot_read_other_tenants_message(rls):
    """get_conversation_history filters by conversation_id only; RLS must still
    block reading a message that belongs to another tenant's conversation."""
    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_a"]))
        leaked = s.query(ConversationMessage).filter(
            ConversationMessage.conversation_id == rls["conv_b"]
        ).count()
        assert leaked == 0
    finally:
        s.close()


def test_insert_message_for_other_tenant_is_blocked(rls):
    s = rls["AppSession"]()
    try:
        set_tenant_context(s, str(rls["tenant_a"]))
        # Context is tenant A but we try to write a message owned by tenant B.
        s.add(ConversationMessage(
            id=uuid.uuid4(), tenant_id=rls["tenant_b"],
            conversation_id=rls["conv_b"], role="user", content="smuggled",
        ))
        with pytest.raises(DBAPIError):
            s.flush()
    finally:
        s.rollback()
        s.close()
