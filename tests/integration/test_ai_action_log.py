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


class TestConfidenceScale:
    """Confidence is stored as a 0..1 fraction whatever scale the caller used.

    Agents disagree: the next-action agent emits 0..1, the extraction schema
    clamps to 0..100. Both write this column, so without normalization a
    reader cannot tell 0.95 from 95 — which surfaced in the audit console as a
    confidence of "9500%".
    """

    def test_fraction_is_stored_unchanged(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        row = log_ai_action(
            db, tenant.id, admin["id"], action="invoice_next_action",
            status=STATUS_COMPLETED, confidence=0.72,
        )
        assert row.confidence == pytest.approx(0.72)

    def test_percentage_is_converted_to_a_fraction(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        row = log_ai_action(
            db, tenant.id, admin["id"], action="invoice_extraction",
            status=STATUS_COMPLETED, confidence=95,
        )
        assert row.confidence == pytest.approx(0.95)

    def test_out_of_range_values_are_clamped(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        high = log_ai_action(
            db, tenant.id, admin["id"], action="invoice_extraction",
            status=STATUS_COMPLETED, confidence=250,
        )
        low = log_ai_action(
            db, tenant.id, admin["id"], action="invoice_extraction",
            status=STATUS_COMPLETED, confidence=-3,
        )
        assert high.confidence == 1.0
        assert low.confidence == 0.0

    def test_none_stays_none(self, db, tenant, make_user):
        admin = make_user(UserRole.ADMIN)
        row = log_ai_action(
            db, tenant.id, admin["id"], action="invoice_next_action",
            status=STATUS_COMPLETED, confidence=None,
        )
        assert row.confidence is None
