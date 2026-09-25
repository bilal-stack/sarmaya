"""Reading and changing the three-way match tolerance.

`get_match_tolerance` has been in policy.py since the matching engine was
written, its docstring says the values are "editable per tenant", and nothing
routed to editing them — so in practice every tenant ran on the defaults and
the only way to move them was a database write.

**Why loosening this is a governance event, not a preference.** The tolerance
is how much an invoice may disagree with the goods actually received before
the match refuses it. Widen it far enough and three-way matching stops being a
control and becomes a formality that passes everything. That is why this takes
the same route every other policy change takes — permission, a version, and an
audit row naming who changed it from what to what — rather than being a
settings blob somebody can adjust quietly.

The ceilings below are the part worth arguing about, and they are stated here
rather than left implicit.
"""
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.core.roles import PERM_MANAGE_POLICIES, has_permission
from app.models.policy import Policy
from app.services.audit import log_audit
from app.services.config_versioning import TYPE_MATCH_TOLERANCE, record_version
from app.services.policy import (
    DEFAULT_MATCH_TOLERANCE, MATCH_POLICY_NAME, MATCH_POLICY_TYPE,
    get_match_tolerance,
)

#: A tolerance this wide is not a tolerance. At 25% an invoice for a hundred
#: boxes passes against a delivery of seventy-five, which is the discrepancy
#: the control exists to catch rather than the rounding it exists to forgive.
#: Refused rather than warned about: a warning on a settings screen is read
#: once and a control that passes everything is wrong every day afterwards.
MAX_PERCENT = 25.0

#: Zero means every rounding difference fails the match. Allowed — some
#: tenants genuinely want that — but it is the setting most likely to get the
#: whole control switched off a week later, so the API says so when it is set.
STRICT_THRESHOLD = 0.0


class MatchToleranceService:
    """The tenant's three-way match tolerance.

    Two numbers today: amount_percent and quantity_percent, tenant-wide. There
    is deliberately no category, vendor or entity axis — adding one is a data
    model decision, and inventing it here would mean this screen implied a
    control the matching engine does not actually apply.
    """

    def __init__(self, db: Session):
        self.db = db

    def _require(self, current_user: dict) -> None:
        """Same gate as every other policy change.

        Not a lighter one because it is "only two numbers": this is the number
        that decides whether three-way matching refuses an invoice.
        """
        if not has_permission(current_user["role"], PERM_MANAGE_POLICIES):
            raise PermissionError(
                f"Role '{current_user['role']}' cannot change the match tolerance"
            )

    def get(self, current_user: dict) -> Dict:
        """The tolerance in force, and whether it is still the default."""
        self._require(current_user)
        current = get_match_tolerance(self.db, current_user["tenant_id"])
        return {
            **current,
            "defaults": dict(DEFAULT_MATCH_TOLERANCE),
            # A tenant running on defaults has not made a decision; one running
            # on configured values has. Worth telling them apart on screen.
            "is_default": current == DEFAULT_MATCH_TOLERANCE,
            "max_percent": MAX_PERCENT,
            #: Stated so the screen does not have to imply otherwise. The
            #: Build Book asks for a tolerance matrix; the engine applies one
            #: pair of numbers to every line of every invoice.
            "axes": None,
        }

    def set(
        self,
        amount_percent: float,
        quantity_percent: float,
        current_user: dict,
        reason: Optional[str] = None,
    ) -> Dict:
        self._require(current_user)

        for name, value in (
            ("amount_percent", amount_percent),
            ("quantity_percent", quantity_percent),
        ):
            if value is None:
                raise ValueError(f"{name} is required")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            if value > MAX_PERCENT:
                raise ValueError(
                    f"{name} cannot exceed {MAX_PERCENT}%. A tolerance that "
                    f"wide passes the discrepancies three-way matching exists "
                    f"to catch."
                )

        rule = {
            "amount_percent": float(amount_percent),
            "quantity_percent": float(quantity_percent),
        }

        policy = (
            self.db.query(Policy)
            .filter(
                Policy.policy_type == MATCH_POLICY_TYPE,
                Policy.policy_name == MATCH_POLICY_NAME,
            )
            .first()
        )
        before = dict(policy.rule_config or {}) if policy else None

        if policy:
            policy.rule_config = rule
            policy.is_active = True
            self.db.add(policy)
            change_action = "updated"
        else:
            policy = Policy(
                tenant_id=current_user["tenant_id"],
                policy_type=MATCH_POLICY_TYPE,
                policy_name=MATCH_POLICY_NAME,
                description="Three-way match tolerance.",
                rule_config=rule,
                applies_to="invoice",
                is_active=True,
                priority=0,
            )
            self.db.add(policy)
            change_action = "created"

        # A per-tenant singleton, versioned under a fixed key so the full
        # history of how loose this got is preserved — the same treatment
        # autopilot settings get, and for the same reason.
        record_version(
            self.db, current_user["tenant_id"], TYPE_MATCH_TOLERANCE,
            TYPE_MATCH_TOLERANCE, rule, change_action, current_user["id"],
            reason,
        )
        # Flush, not commit: widening what the match will accept must not be
        # recordable separately from who widened it.
        self.db.flush()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="tenant_config",
            object_id=policy.id,
            action="match_tolerance_configured",
            before_value=before or dict(DEFAULT_MATCH_TOLERANCE),
            after_value=rule,
            comment=reason,
        )
        self.db.commit()

        return self.get(current_user)
