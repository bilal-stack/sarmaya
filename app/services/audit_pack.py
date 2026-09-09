"""Audit packs covering a period, or one control across a period.

Build Book, Audit/Compliance: "One-click audit pack export per period and per
control."

`evidence_pack.py` builds the other kind — one transaction chain, end to end,
which answers "what happened to this invoice". This answers the question an
auditor actually opens with, and it is a different question: *did this control
operate*, over a quarter, across every record it touched.

**An empty control pack is a real finding, and is sealed as one.** This is the
one place that deliberately differs from `EvidencePackService.generate`, which
refuses to seal an empty chain pack because an empty chain means the caller
asked about something that does not exist or that they cannot see — a lookup
failure dressed as a document. Here the scope is a date range, the query is
well-formed, and "this control did not fire in Q3" is a computed answer an
auditor specifically wants on the record. The two cases look alike and are
opposites: one certifies an absence it never checked, the other reports an
absence it did.

What a control pack shows, for each control, is three counts and a sample:
how many times the control **applied**, how many times it **blocked**
something, and how many times it was **overridden** with a reason. A control
with applications and no blocks is working and unchallenged; one with no
applications at all is either irrelevant to the period or not wired up, and
those are worth telling apart.
"""
import logging
from datetime import date, datetime, time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.roles import has_permission, PERM_VIEW_AUDIT
from app.models.audit_log import AuditLog
from app.models.evidence_pack import EvidencePack, SCOPE_CONTROL, SCOPE_PERIOD
from app.models.policy_eval import PolicyEval
from app.services.export_service import canonical_json, sha256_of
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

#: The controls this system can actually evidence, and the audit actions that
#: evidence each. Grounded in the actions services really write — a registry
#: naming a control nothing emits would produce a pack that reports zero
#: forever and reads as "the control never fired" rather than "nobody wired
#: this entry up".
#:
#: `applied` is the control doing its ordinary work. `blocked` is it refusing
#: something. `overridden` is somebody setting it aside with a reason, which is
#: legitimate and is exactly what an auditor samples.
CONTROLS: Dict[str, Dict] = {
    "segregation_of_duties": {
        "label": "Segregation of duties",
        "what": (
            "Nobody approves, releases, reconciles or activates their own "
            "work. Enforced with no admin exemption."
        ),
        "applied": ["approved", "released", "reconciled", "status_changed"],
        "blocked": [
            "approval_blocked", "release_blocked", "reconciliation_blocked",
            "vendor_activation_blocked", "bank_change_approval_blocked",
        ],
        "overridden": [],
    },
    "bank_change_dual_approval": {
        "label": "Vendor bank changes",
        "what": (
            "Bank details cannot be edited. They move through a request a "
            "second person approves, then a cooling period, then an explicit "
            "apply — and whoever requested the change cannot release the "
            "first payment that uses it."
        ),
        "applied": [
            "bank_change_requested", "bank_change_approved",
            "bank_change_applied", "bank_change_rejected",
        ],
        "blocked": ["bank_change_approval_blocked"],
        "overridden": [],
    },
    "duplicate_detection": {
        "label": "Duplicate invoice detection",
        "what": (
            "A suspected duplicate blocks payment until somebody states why "
            "it is not one. The override is the interesting record, not the "
            "detection."
        ),
        "applied": ["watchlist_alert_acknowledged"],
        "blocked": [],
        "overridden": ["duplicate_overridden"],
    },
    "approval_routing": {
        "label": "Approval thresholds and routing",
        "what": (
            "Which approver a record required was decided by a versioned "
            "policy and the decision was snapshotted, so the rule that "
            "applied at the time is recoverable afterwards."
        ),
        "applied": ["submitted_for_approval", "approved", "rejected"],
        "blocked": ["approval_blocked"],
        "overridden": [],
    },
    "access_control": {
        "label": "Authentication and access",
        "what": (
            "Second factors, recovery codes, and the org-unit scopes that "
            "decide what a role may act on."
        ),
        "applied": [
            "mfa_enabled", "mfa_disabled", "scope_granted", "scope_revoked",
            "user_linked",
        ],
        "blocked": ["mfa_failed"],
        "overridden": ["mfa_reset", "mfa_recovery_code_used"],
    },
}


def _bounds(period_start: date, period_end: date):
    """The half-open window the queries use.

    `period_end` is inclusive to whoever asked for it — "Q3" means the whole
    of 30 September, not up to midnight at its start. Comparing a timestamp
    against a bare date would silently drop the last day's activity from every
    pack, which is the kind of error that only shows up when somebody
    reconciles a total against the ledger.
    """
    return (
        datetime.combine(period_start, time.min),
        datetime.combine(period_end, time.max),
    )


class AuditPackService:
    def __init__(self, db: Session):
        self.db = db

    # --- building ------------------------------------------------------------

    def build(
        self, period_start: date, period_end: date, current_user: dict,
        control: Optional[str] = None,
    ) -> Dict:
        """Assemble the bundle. Does not record anything — see `generate`."""
        self._require_audit(current_user)
        if period_end < period_start:
            raise ValueError("The period ends before it starts.")
        if control is not None and control not in CONTROLS:
            raise ValueError(
                f"{control!r} is not a control this system evidences. One of: "
                f"{', '.join(sorted(CONTROLS))}"
            )

        start_at, end_at = _bounds(period_start, period_end)
        chosen = {control: CONTROLS[control]} if control else CONTROLS

        entries = (
            self.db.query(
                AuditLog.action, AuditLog.user_email, AuditLog.object_type,
                AuditLog.object_id, AuditLog.comment, AuditLog.after_value,
                AuditLog.timestamp,
            )
            .filter(AuditLog.timestamp >= start_at, AuditLog.timestamp <= end_at)
            .order_by(AuditLog.timestamp.desc())
            .all()
        )

        by_action: Dict[str, List] = {}
        for entry in entries:
            by_action.setdefault(entry.action, []).append(entry)

        controls = [
            self._control_section(key, spec, by_action)
            for key, spec in sorted(chosen.items())
        ]

        policy_evals = (
            self.db.query(PolicyEval)
            .filter(PolicyEval.created_at >= start_at,
                    PolicyEval.created_at <= end_at)
            .count()
        )

        # What the seal covers, and nothing else. `generated_at` is
        # deliberately outside it — the whole value of the hash is that
        # regenerating the same period later and getting a different answer
        # means something underneath changed, and a timestamp inside the
        # payload would make every regeneration differ for a reason that has
        # nothing to do with the evidence. Same split, and same helper, as
        # EvidencePackService uses for a chain pack, so a reader re-hashing an
        # exported document gets the right answer for either kind.
        content = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "control": control,
            "controls": controls,
        }

        return {
            "scope": SCOPE_CONTROL if control else SCOPE_PERIOD,
            "control": control,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "generated_at": utc_now().isoformat(),
            "counts": {
                "audit_entries": len(entries),
                "policy_evaluations": policy_evals,
                "controls_evidenced": len(controls),
                "applied": sum(c["applied"] for c in controls),
                "blocked": sum(c["blocked"] for c in controls),
                "overridden": sum(c["overridden"] for c in controls),
            },
            "pack_hash": sha256_of(canonical_json(content)),
            "content": content,
        }

    def _control_section(self, key: str, spec: Dict, by_action: Dict) -> Dict:
        def collect(actions: List[str]) -> List:
            found = []
            for action in actions:
                found.extend(by_action.get(action, []))
            return found

        applied = collect(spec["applied"])
        blocked = collect(spec["blocked"])
        overridden = collect(spec["overridden"])

        return {
            "control": key,
            "label": spec["label"],
            "what": spec["what"],
            "applied": len(applied),
            "blocked": len(blocked),
            "overridden": len(overridden),
            # Blocks and overrides are sampled; ordinary applications are only
            # counted. An auditor testing a control reads the exceptions and
            # takes the volume of normal operation as a number — shipping
            # every approval in a quarter would bury the five records that
            # matter in ten thousand that do not.
            "blocked_sample": [self._entry(e) for e in blocked[:25]],
            "overridden_sample": [self._entry(e) for e in overridden[:25]],
            "operated": bool(applied or blocked or overridden),
        }

    @staticmethod
    def _entry(entry) -> Dict:
        reason = (entry.after_value or {}).get("reason")
        return {
            "action": entry.action,
            "who": entry.user_email,
            "object_type": entry.object_type,
            "object_id": str(entry.object_id) if entry.object_id else None,
            "reason": reason or entry.comment,
            "at": entry.timestamp.isoformat() if entry.timestamp else None,
        }

    # --- sealing -------------------------------------------------------------

    def generate(
        self, period_start: date, period_end: date, current_user: dict,
        control: Optional[str] = None,
    ) -> Dict:
        """Assemble the bundle and record that it was produced.

        Unlike a chain pack, this seals an empty result. See the module
        docstring: "nothing was refused this quarter" is a finding, where an
        empty chain pack is a lookup that failed.
        """
        pack = self.build(period_start, period_end, current_user, control)

        row = EvidencePack(
            tenant_id=current_user["tenant_id"],
            correlation_id=None,
            scope=pack["scope"],
            control=control,
            period_start=period_start,
            period_end=period_end,
            pack_hash=pack["pack_hash"],
            manifest={
                "counts": pack["counts"],
                "controls": [
                    {
                        "control": c["control"],
                        "applied": c["applied"],
                        "blocked": c["blocked"],
                        "overridden": c["overridden"],
                        "operated": c["operated"],
                    }
                    for c in pack["content"]["controls"]
                ],
            },
            generated_by=current_user.get("id"),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)

        pack["pack_id"] = row.id
        return pack

    # --- reading -------------------------------------------------------------

    def list_controls(self, current_user: dict) -> List[Dict]:
        self._require_audit(current_user)
        return [
            {"control": key, "label": spec["label"], "what": spec["what"]}
            for key, spec in sorted(CONTROLS.items())
        ]

    @staticmethod
    def _require_audit(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_VIEW_AUDIT):
            raise PermissionError(
                "You do not have permission to generate audit packs"
            )
