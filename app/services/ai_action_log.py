"""Logging + read access for AI-action audit records.

`log_ai_action` is called by every AI agent after it runs, recording the
model/provider, prompt version, confidence, latency, and status. It is
best-effort: a logging failure must never break the AI response it describes.
"""
import logging
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.ai_action_log import AIActionLog
from app.services.correlation import resolve_correlation_id
from app.core.roles import has_permission, PERM_VIEW_AUDIT

logger = logging.getLogger(__name__)

# Statuses (Build Book Appendix A ai.* event family).
STATUS_COMPLETED = "completed"
STATUS_FAILED_SCHEMA = "failed_schema"
STATUS_HITL = "hitl_requested"
STATUS_ERROR = "error"


def _normalize_confidence(value: Optional[float]) -> Optional[float]:
    """Store confidence as a 0..1 fraction, always.

    Callers disagree about scale — the next-action agent produces 0..1 while
    the extraction schema clamps to 0..100 — and both were being written to
    this one column. A reader then has no way to tell 0.95 from 95 apart from
    guessing, which surfaced as a "9500%" confidence in the audit console.

    Values above 1 are treated as percentages. That is unambiguous here
    because a fraction can never exceed 1, and it keeps the fix in one place
    instead of asking every caller to remember the convention.
    """
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value > 1:
        value = value / 100
    return max(0.0, min(1.0, value))


def log_ai_action(
    db: Session,
    tenant_id,
    user_id,
    action: str,
    status: str,
    *,
    ai_provider: Optional[str] = None,
    ai_model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    confidence: Optional[float] = None,
    latency_ms: Optional[int] = None,
    input_summary: Optional[str] = None,
    output_summary: Optional[str] = None,
    object_type: Optional[str] = None,
    object_id=None,
) -> Optional[AIActionLog]:
    """Append one AI-action record. Summaries are truncated; raw payloads are
    never stored. `confidence` is normalized to a 0..1 fraction regardless of
    the scale the caller uses. Returns the row, or None if logging failed
    (swallowed)."""
    try:
        row = AIActionLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            status=status,
            ai_provider=ai_provider,
            ai_model=ai_model,
            prompt_version=prompt_version,
            confidence=_normalize_confidence(confidence),
            latency_ms=latency_ms,
            input_summary=(input_summary or None) and input_summary[:500],
            output_summary=(output_summary or None) and output_summary[:500],
            correlation_id=resolve_correlation_id(db, object_type, object_id),
            object_type=object_type,
            object_id=object_id,
        )
        db.add(row)
        db.flush()
        return row
    except Exception:
        logger.exception("Failed to write AI action log (action=%s)", action)
        return None


class AIActionLogService:
    """Read access to the AI-action trail, for the audit viewer (auditor/admin)."""

    def __init__(self, db: Session):
        self.db = db

    def list_actions(
        self,
        current_user: dict,
        action: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AIActionLog], int]:
        if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
            raise PermissionError("You do not have permission to view AI action logs")
        query = self.db.query(AIActionLog)
        if action:
            query = query.filter(AIActionLog.action == action)
        if status:
            query = query.filter(AIActionLog.status == status)
        total = query.count()
        rows = (
            query.order_by(AIActionLog.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total
