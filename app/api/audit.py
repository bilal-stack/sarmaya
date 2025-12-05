from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional, List
from datetime import date, datetime
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.models.audit_log import AuditLog
from app.models.user import User
from app.core.enums import UserRole

router = APIRouter(prefix="/audit", tags=["Audit"])


def require_auditor_role(current_user: dict = Depends(get_current_user)):
    """Ensure user has auditor or admin role"""
    if current_user["role"] not in [UserRole.AUDITOR.value, UserRole.ADMIN.value]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Auditor or admin role required"
        )
    return current_user


# ============================================
# AUDIT LOG QUERIES (READ-ONLY)
# ============================================

@router.get("/logs")
def get_audit_logs(
    object_type: Optional[str] = None,
    object_id: Optional[UUID] = None,
    user_id: Optional[UUID] = None,
    action: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    workflow_type: Optional[str] = None,
    ai_assisted: Optional[bool] = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(require_auditor_role),
    db: Session = Depends(get_db_session),
):
    """
    Query audit logs with comprehensive filters (read-only)
    
    Accessible only to auditors and admins
    """
    query = db.query(AuditLog)
    
    if object_type:
        query = query.filter(AuditLog.object_type == object_type)
    
    if object_id:
        query = query.filter(AuditLog.object_id == object_id)
    
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    
    if action:
        query = query.filter(AuditLog.action == action)
    
    if start_date:
        query = query.filter(AuditLog.timestamp >= datetime.combine(start_date, datetime.min.time()))
    
    if end_date:
        query = query.filter(AuditLog.timestamp <= datetime.combine(end_date, datetime.max.time()))
    
    if workflow_type:
        query = query.filter(AuditLog.workflow_type == workflow_type)
    
    if ai_assisted is not None:
        query = query.filter(AuditLog.ai_assisted == ai_assisted)
    
    total = query.count()
    logs = query.order_by(desc(AuditLog.timestamp)).offset(offset).limit(limit).all()
    
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "logs": [
            {
                "id": str(log.id),
                "timestamp": log.timestamp.isoformat(),
                "user_email": log.user_email,
                "user_role": log.user_role,
                "action": log.action,
                "object_type": log.object_type,
                "object_id": str(log.object_id),
                "workflow_step": log.workflow_step,
                "workflow_type": log.workflow_type,
                "before_value": log.before_value,
                "after_value": log.after_value,
                "changes": log.changes,
                "file_path": log.file_path,
                "document_hash": log.document_hash,
                "ai_assisted": log.ai_assisted,
                "ai_provider": log.ai_provider,
                "ai_confidence": log.ai_confidence,
                "ip_address": str(log.ip_address) if log.ip_address else None,
                "comment": log.comment,
            }
            for log in logs
        ]
    }


@router.get("/trail/{object_type}/{object_id}")
def get_audit_trail(
    object_type: str,
    object_id: UUID,
    current_user: dict = Depends(require_auditor_role),
    db: Session = Depends(get_db_session),
):
    """
    Get complete audit trail for a specific object
    
    Returns chronological history of all changes
    """
    logs = db.query(AuditLog).filter(
        AuditLog.object_type == object_type,
        AuditLog.object_id == object_id
    ).order_by(AuditLog.timestamp.asc()).all()
    
    return {
        "object_type": object_type,
        "object_id": str(object_id),
        "total_events": len(logs),
        "trail": [
            {
                "timestamp": log.timestamp.isoformat(),
                "action": log.action,
                "user": log.user_email,
                "role": log.user_role,
                "workflow_step": log.workflow_step,
                "before": log.before_value,
                "after": log.after_value,
                "ai_assisted": log.ai_assisted,
                "file_hash": log.document_hash,
            }
            for log in logs
        ]
    }


@router.get("/stats/summary")
def get_audit_stats(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: dict = Depends(require_auditor_role),
    db: Session = Depends(get_db_session),
):
    """
    Audit statistics and metrics
    """
    query = db.query(AuditLog)
    
    if start_date:
        query = query.filter(AuditLog.timestamp >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(AuditLog.timestamp <= datetime.combine(end_date, datetime.max.time()))
    
    # Count by action
    actions = db.query(
        AuditLog.action,
        func.count(AuditLog.id).label("count")
    ).group_by(AuditLog.action).all()
    
    # Count AI-assisted actions
    ai_assisted_count = query.filter(AuditLog.ai_assisted == True).count()
    
    # Count by user role
    roles = db.query(
        AuditLog.user_role,
        func.count(AuditLog.id).label("count")
    ).group_by(AuditLog.user_role).all()
    
    return {
        "total_events": query.count(),
        "by_action": {a[0]: a[1] for a in actions},
        "by_role": {r[0]: r[1] for r in roles},
        "ai_assisted_actions": ai_assisted_count,
    }
