"""Evidence Pack generation.

Build Book: "one-click audit-ready bundle with hashes, logs, and policy
snapshots", and "evidence pack includes hashes of all attachments referenced".

A pack is assembled from a transaction chain (correlation_id) and contains
everything an auditor needs to reconstruct and trust the story:

  * the business objects in the chain and their current state;
  * the full audit trail, plus the result of verifying its hash chain;
  * the policy evaluations — which rule version decided what, on what inputs;
  * the AI action log — model, prompt version, confidence, schema outcome;
  * every attachment with its stored content hash.

The bundle is sealed with a SHA-256 `pack_hash`. Re-generating later and
comparing hashes shows whether anything underlying the export has changed.
"""
import hashlib
import json
import logging
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.correlation import chain_owners
from app.models.file import File
from app.models.audit_log import AuditLog
from app.models.policy_eval import PolicyEval
from app.models.ai_action_log import AIActionLog
from app.models.evidence_pack import EvidencePack
from app.services.audit_integrity import verify_object_chain
from app.core.roles import has_permission, PERM_VIEW_AUDIT
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)


def _canonical_hash(payload: dict) -> str:
    """Stable SHA-256 over the bundle: the pack's integrity seal."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


class EvidencePackService:
    """Assembles and records audit-ready evidence bundles."""

    def __init__(self, db: Session):
        self.db = db

    def build(self, correlation_id: UUID, current_user: dict) -> Dict:
        """Assemble the bundle for a chain without persisting it."""
        self._require_audit(current_user)

        # Every chain-owning module, so a pack covers the order, the receipts
        # and the invoice rather than only the invoice. Assembled as
        # (object_type, record) pairs because the integrity check and the
        # rendered summary both need to know which module each row came from.
        records = [
            (object_type, row)
            for object_type, model in chain_owners().items()
            for row in (
                self.db.query(model)
                .filter(model.correlation_id == correlation_id)
                .order_by(model.created_at.asc())
                .all()
            )
        ]
        audit = (
            self.db.query(AuditLog)
            .filter(AuditLog.correlation_id == correlation_id)
            .order_by(AuditLog.timestamp.asc())
            .all()
        )
        evals = (
            self.db.query(PolicyEval)
            .filter(PolicyEval.correlation_id == correlation_id)
            .order_by(PolicyEval.created_at.asc())
            .all()
        )
        ai = (
            self.db.query(AIActionLog)
            .filter(AIActionLog.correlation_id == correlation_id)
            .order_by(AIActionLog.created_at.asc())
            .all()
        )

        # Attachments referenced by anything in the chain, with content hashes.
        object_ids = [row.id for _, row in records]
        attachments = []
        if object_ids:
            for f in self.db.query(File).filter(File.object_id.in_(object_ids)).all():
                attachments.append({
                    "file_id": str(f.id),
                    "filename": f.original_filename,
                    "mime_type": f.mime_type,
                    "size_bytes": f.file_size,
                    "sha256": f.file_hash,
                    "object_type": f.object_type,
                    "object_id": str(f.object_id) if f.object_id else None,
                })

        # Integrity of each object's audit chain, included in the pack so the
        # bundle carries its own tamper-evidence rather than a bare assertion.
        integrity = [
            {
                "object_type": object_type,
                "object_id": str(row.id),
                **{
                    k: v for k, v in
                    verify_object_chain(self.db, object_type, row.id).items()
                    if k in ("total_events", "verified", "broken_at_index", "detail")
                },
            }
            for object_type, row in records
        ]

        content = {
            "correlation_id": str(correlation_id),
            "objects": [
                # Fields are read defensively because the chain spans modules
                # with genuinely different shapes: a goods receipt has no
                # vendor, no total and no state — it is a statement that
                # something arrived, not a financial document. Assuming the
                # invoice shape here made the whole pack 500 once receipts
                # joined the chain.
                {
                    "object_type": object_type,
                    "object_id": str(row.id),
                    "reference": getattr(row, "invoice_number", None)
                    or getattr(row, "po_number", None)
                    or getattr(row, "grn_number", None)
                    or str(row.id),
                    "vendor_name": getattr(row, "vendor_name", None),
                    "total_amount": float(getattr(row, "total_amount", None) or 0),
                    "currency": getattr(
                        getattr(row, "currency", None), "value",
                        getattr(row, "currency", None),
                    ),
                    "state": getattr(
                        getattr(row, "current_state", None), "value",
                        getattr(row, "current_state", None),
                    ),
                    "date": str(
                        getattr(row, "invoice_date", None)
                        or getattr(row, "order_date", None)
                        or getattr(row, "received_date", None)
                        or ""
                    ) or None,
                }
                for object_type, row in records
            ],
            "audit_trail": [
                {
                    "at": str(a.timestamp),
                    "action": a.action,
                    "actor": a.user_email,
                    "actor_role": str(a.user_role) if a.user_role is not None else None,
                    "workflow_step": a.workflow_step,
                    "before": a.before_value,
                    "after": a.after_value,
                    "comment": a.comment,
                    "entry_hash": a.entry_hash,
                }
                for a in audit
            ],
            "policy_evaluations": [
                {
                    "at": str(e.created_at),
                    "policy_key": e.policy_key,
                    "policy_name": e.policy_name,
                    "policy_version": e.policy_version,
                    "inputs": e.inputs,
                    "output": e.output,
                    "reasons": e.reasons,
                }
                for e in evals
            ],
            "ai_actions": [
                {
                    "at": str(l.created_at),
                    "action": l.action,
                    "status": l.status,
                    "provider": l.ai_provider,
                    "model": l.ai_model,
                    "prompt_version": l.prompt_version,
                    "confidence": l.confidence,
                    "output_summary": l.output_summary,
                }
                for l in ai
            ],
            "attachments": attachments,
            "integrity": integrity,
        }

        counts = {
            "objects": len(content["objects"]),
            "audit_events": len(audit),
            "policy_evaluations": len(evals),
            "ai_actions": len(ai),
            "attachments": len(attachments),
        }
        return {
            "correlation_id": correlation_id,
            "generated_at": utc_now(),
            "counts": counts,
            "all_chains_verified": all(i.get("verified") for i in integrity) if integrity else True,
            "pack_hash": _canonical_hash(content),
            "content": content,
        }

    def generate(self, correlation_id: UUID, current_user: dict) -> Dict:
        """Assemble the bundle and record that it was produced."""
        pack = self.build(correlation_id, current_user)
        row = EvidencePack(
            tenant_id=current_user["tenant_id"],
            correlation_id=correlation_id,
            pack_hash=pack["pack_hash"],
            manifest={
                "counts": pack["counts"],
                "all_chains_verified": pack["all_chains_verified"],
                "attachment_hashes": [
                    {"file_id": a["file_id"], "sha256": a["sha256"]}
                    for a in pack["content"]["attachments"]
                ],
                "integrity": pack["content"]["integrity"],
            },
            generated_by=current_user.get("id"),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)

        pack["pack_id"] = row.id
        return pack

    def list_packs(self, current_user: dict, correlation_id: Optional[UUID] = None) -> List[EvidencePack]:
        self._require_audit(current_user)
        query = self.db.query(EvidencePack)
        if correlation_id:
            query = query.filter(EvidencePack.correlation_id == correlation_id)
        return query.order_by(EvidencePack.created_at.desc()).all()

    @staticmethod
    def _require_audit(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
            raise PermissionError("You do not have permission to generate evidence packs")
