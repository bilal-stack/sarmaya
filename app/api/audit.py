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
from app.schemas.audit import AuditTimeline, AuditChainVerification
from app.schemas.ai import AIActionLogResponse
from app.schemas.policy import PolicyEvalResponse
from app.services.audit_service import AuditService
from app.services.ai_action_log import AIActionLogService
from app.services.policy_eval import PolicyEvalService
from app.schemas.correlation import TransactionChain
from app.services.correlation import CorrelationService
from app.schemas.evidence import EvidencePackResponse, EvidencePackRecord
from app.services.evidence_pack import EvidencePackService

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


@router.get("/timeline/{object_type}/{object_id}", response_model=AuditTimeline)
def get_audit_timeline(
    object_type: str,
    object_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Live Audit Mode: open any object as a full timeline where each event has a
    plain-English reason, plus (for invoices) the current policy routing reason.

    Visible to whoever can view the object (e.g. invoice viewers), not only
    auditors — unlike /trail and /logs.
    """
    try:
        return AuditService(db).get_timeline(object_type, object_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/verify/{object_type}/{object_id}", response_model=AuditChainVerification)
def verify_audit_chain(
    object_type: str,
    object_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Verify that an object's audit trail has not been tampered with.

    Recomputes the per-object hash chain and reports whether it is intact, and
    if not, the first event where it breaks. Visible to whoever can view the
    object (same rule as the Live Audit timeline).
    """
    try:
        return AuditService(db).verify_chain(object_type, object_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/ai-actions", response_model=List[AIActionLogResponse])
def list_ai_actions(
    action: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(require_auditor_role),
    db: Session = Depends(get_db_session),
):
    """The AI-action audit trail: every AI invocation with its model/provider,
    prompt version, confidence, latency, and status (completed / failed_schema /
    hitl_requested). Auditor/admin only."""
    rows, _ = AIActionLogService(db).list_actions(
        current_user, action=action, status=status_filter, limit=limit, offset=offset
    )
    return rows


@router.get("/policy-evals", response_model=List[PolicyEvalResponse])
def list_policy_evals(
    object_type: Optional[str] = None,
    object_id: Optional[UUID] = None,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Policy evaluation snapshots: which rule version made each routing
    decision, on what inputs, with what reasons. Reproduces a decision even
    after the policy has been edited or rolled back. Auditors and policy
    administrators only."""
    try:
        rows, _ = PolicyEvalService(db).list_evals(
            current_user, object_type=object_type, object_id=object_id,
            limit=limit, offset=offset,
        )
        return rows
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/chain/{correlation_id}", response_model=TransactionChain)
def get_transaction_chain(
    correlation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Reconstruct an entire transaction story from its correlation_id.

    Merges every record that shares the chain - audit events, policy
    evaluations and AI actions - into one time-ordered feed, across every
    object in the chain. As further modules land (PR, PO, GRN, payment),
    they join the same chain and appear here without changing this endpoint.
    """
    try:
        return CorrelationService(db).get_chain(correlation_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/evidence-pack/{correlation_id}", response_model=EvidencePackResponse)
def generate_evidence_pack(
    correlation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Generate an audit-ready evidence pack for a transaction chain.

    Bundles the objects, the full audit trail with its hash-chain
    verification, the policy evaluations, the AI action log, and every
    attachment with its content hash — sealed with a SHA-256 pack_hash.
    Regenerating later and comparing hashes shows whether anything underlying
    the export has changed. Auditors and admins only.
    """
    try:
        return EvidencePackService(db).generate(correlation_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/evidence-pack/{correlation_id}", response_model=EvidencePackResponse)
def preview_evidence_pack(
    correlation_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Assemble the same bundle without recording a generation."""
    try:
        return EvidencePackService(db).build(correlation_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("/evidence-packs", response_model=List[EvidencePackRecord])
def list_evidence_packs(
    correlation_id: Optional[UUID] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Which packs have been generated, when, by whom, and with what seal."""
    try:
        return EvidencePackService(db).list_packs(current_user, correlation_id)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


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
