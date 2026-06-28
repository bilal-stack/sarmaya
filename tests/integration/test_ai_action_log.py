"""Integration tests for AI-action logging (app/services/ai_action_log.py).

The Build Book requires every AI action to be logged with provenance and status;
the trail is readable by auditors/admins.
"""
import pytest

from app.core.enums import UserRole
from app.services.ai_action_log import (
    log_ai_action,
    AIActionLogService,
    STATUS_COMPLETED,
    STATUS_FAILED_SCHEMA,
)

pytestmark = pytest.mark.integration


class TestLogAIAction:
    def test_writes_row_with_provenance(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        row = log_ai_action(
            db, tenant.id, admin["id"],
            action="duplicate_detection", status=STATUS_COMPLETED,
            ai_provider="claude", ai_model="claude-opus-4-8", prompt_version="dup-detect-v1",
            confidence=0.92, latency_ms=1234,
            input_summary="Acme | INV-1 | 1000", output_summary="match",
        )
        assert row is not None
        assert row.action == "duplicate_detection"
        assert row.ai_provider == "claude"
        assert row.ai_model == "claude-opus-4-8"
        assert row.prompt_version == "dup-detect-v1"
        assert row.confidence == 0.92
        assert row.latency_ms == 1234

    def test_summaries_truncated(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        row = log_ai_action(
            db, tenant.id, admin["id"], action="nl_query", status=STATUS_COMPLETED,
            input_summary="x" * 1000,
        )
        assert len(row.input_summary) == 500


class TestReadAccess:
    def test_list_newest_first_and_filtered(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        log_ai_action(db, tenant.id, admin["id"], action="nl_query", status=STATUS_COMPLETED)
        log_ai_action(db, tenant.id, admin["id"], action="duplicate_detection", status=STATUS_FAILED_SCHEMA)
        db.flush()

        rows, total = AIActionLogService(db).list_actions(admin)
        assert total == 2
        # filter
        dup, dup_total = AIActionLogService(db).list_actions(admin, action="duplicate_detection")
        assert dup_total == 1
        assert dup[0].status == STATUS_FAILED_SCHEMA

    def test_requires_audit_permission(self, db, tenant, make_user):
        clerk = make_user(UserRole.AP_CLERK)  # no audit.view
        with pytest.raises(PermissionError):
            AIActionLogService(db).list_actions(clerk)
