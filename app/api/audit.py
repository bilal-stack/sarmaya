from fastapi import APIRouter, Depends, HTTPException, Response, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional, List
from datetime import date, datetime
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.models.audit_log import AuditLog
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
from app.services.audit_pack import AuditPackService
from app.services.export_service import canonical_json, to_html

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


@router.get("/controls")
def list_controls(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The controls this system can evidence, and what each one is.

    Read before asking for a control pack — the identifiers are the ones
    `/audit/pack` accepts.
    """
    try:
        return AuditPackService(db).list_controls(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/pack")
def generate_audit_pack(
    period_start: date = Query(..., description="First day covered, inclusive."),
    period_end: date = Query(..., description="Last day covered, inclusive."),
    control: Optional[str] = Query(
        None, description="Narrow the pack to one control. Omit for all of them.",
    ),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """One-click audit pack for a period, optionally for a single control.

    The other pack (`/evidence-pack/{correlation_id}`) answers "what happened
    to this invoice". This answers "did this control operate", across every
    record it touched in the window — which is the question an auditor opens
    with. Sealed with the same SHA-256 hash so either kind can be re-verified
    from its exported document.

    An empty result is sealed here rather than refused, which is the opposite
    of the chain pack's behaviour and deliberately so: "nothing was refused
    this quarter" is a finding, where an empty chain pack is a lookup that
    failed. See the service's module docstring.
    """
    try:
        return AuditPackService(db).generate(
            period_start, period_end, current_user, control,
        )
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        # A backwards period, or a control nobody evidences. Both are the
        # caller asking for something incoherent rather than something absent.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
    except ValueError as e:
        # A correlation id with nothing behind it — another tenant's, or one
        # that never existed. Refusing to seal an empty pack is a 404, not a
        # crash; the service raises rather than returning a certified nothing.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


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


@router.get("/evidence-pack/{correlation_id}/export")
def export_evidence_pack(
    correlation_id: UUID,
    format: str = Query("html", pattern="^(html|json)$"),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The evidence pack as a file an auditor can keep.

    The pack has always existed; until now it only came back as JSON over the
    API, which is not something anybody files.

    **What the hash seals.** The `pack_hash` is computed over the canonical
    JSON bundle, not over this document. Re-hashing a rendered page gives a
    different number, so a page with a hash printed on it that cannot be
    recomputed from what you are holding is decoration rather than evidence.
    The HTML export therefore embeds the exact canonical bundle it was
    rendered from, in a script block, and says how to check it. Extract that
    block, SHA-256 it, and it equals the printed hash.

    This is a *preview* export — it does not seal a new pack. Sealing is a
    POST, because recording that a pack was produced is a write; downloading
    a view of one is not, and a GET that quietly created records would make
    every refresh a new audit event.
    """
    try:
        pack = EvidencePackService(db).build(correlation_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    if not pack["counts"]["objects"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No records found for this correlation id, so there is "
                   "nothing to evidence.",
        )

    bundle = canonical_json(pack["content"])
    filename = f"evidence-pack-{correlation_id}.{'json' if format == 'json' else 'html'}"

    if format == "json":
        return Response(
            content=canonical_json(pack),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # The seal, in the response itself, so a client that stores the
                # file does not have to parse it to know what it should hash to.
                "X-Pack-SHA256": pack["pack_hash"],
            },
        )

    content = pack["content"]
    sections = [
        ("Objects in this chain", ["object_type", "reference", "state", "created_at"],
         content.get("objects", [])),
        ("Integrity", ["object_type", "object_id", "verified", "events", "detail"],
         content.get("integrity", [])),
        ("Audit trail", ["timestamp", "object_type", "action", "user", "comment"],
         content.get("audit_trail", [])),
        ("Policy evaluations", ["evaluated_at", "policy", "version", "outcome", "reason"],
         content.get("policy_evaluations", [])),
        ("AI actions", ["created_at", "action", "status", "ai_model", "prompt_version",
                        "confidence"],
         content.get("ai_actions", [])),
        ("Attachments", ["file_id", "filename", "sha256", "size_bytes"],
         content.get("attachments", [])),
    ]

    document = to_html(
        title="Evidence pack",
        subtitle="Every record behind one transaction chain, with the trail "
                 "that produced it and the result of verifying that trail.",
        sections=sections,
        meta={
            "correlation_id": str(correlation_id),
            "generated_at": pack["generated_at"],
            "generated_by": current_user.get("email") or current_user.get("id"),
            "all_chains_verified": pack["all_chains_verified"],
            "pack_hash": pack["pack_hash"],
        },
        embedded_json=bundle,
        embedded_note=(
            "The pack hash above seals the canonical JSON bundle, not this "
            "page. The bundle is embedded below verbatim: extract the contents "
            "of the script element with id 'canonical-bundle' and take its "
            "SHA-256 to reproduce the hash. Re-generating the pack later and "
            "comparing hashes shows whether anything underneath has changed."
        ),
    )
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Pack-SHA256": pack["pack_hash"],
        },
    )
