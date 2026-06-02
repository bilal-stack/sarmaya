from sqlalchemy.orm import Session
from typing import List
from uuid import UUID

from app.repositories.policy_repository import PolicyRepository
from app.models.policy import Policy
from app.schemas.policy import ApprovalPolicyCreate, ApprovalPolicyUpdate
from app.services.audit import log_audit
from app.core.roles import has_permission, PERM_MANAGE_POLICIES

POLICY_TYPE = "approval_limit"
APPLIES_TO = "invoice"


class ApprovalPolicyService:
    """Admin CRUD for the approval-routing matrix. These rows are read by
    evaluate_approval_role, so editing them changes approval routing without a
    code deploy (configuration-first)."""

    def __init__(self, db: Session):
        self.db = db
        self.repository = PolicyRepository(db)

    def list_policies(self, current_user: dict) -> List[Policy]:
        self._require_manage(current_user)
        return self.repository.list_by_type(POLICY_TYPE)

    def get_policy(self, policy_id: UUID, current_user: dict) -> Policy:
        self._require_manage(current_user)
        policy = self._load(policy_id)
        return policy

    def create_policy(self, data: ApprovalPolicyCreate, current_user: dict) -> Policy:
        self._require_manage(current_user)

        if self.repository.get_by_name(POLICY_TYPE, data.policy_name):
            raise ValueError("An approval policy with this name already exists")

        policy = Policy(
            tenant_id=current_user["tenant_id"],
            policy_type=POLICY_TYPE,
            policy_name=data.policy_name,
            description=data.description,
            rule_config=data.rule.model_dump(),
            applies_to=APPLIES_TO,
            is_active=data.is_active,
            priority=data.priority,
        )
        policy = self.repository.create(policy)
        self.repository.commit()
        policy = self.repository.refresh(policy)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="policy",
            object_id=policy.id,
            action="created",
            after_value={"policy_name": policy.policy_name, "rule_config": policy.rule_config},
        )
        return policy

    def update_policy(
        self, policy_id: UUID, data: ApprovalPolicyUpdate, current_user: dict
    ) -> Policy:
        self._require_manage(current_user)
        policy = self._load(policy_id)

        changes = data.model_dump(exclude_unset=True)

        new_name = changes.get("policy_name")
        if new_name and new_name != policy.policy_name:
            if self.repository.get_by_name(POLICY_TYPE, new_name):
                raise ValueError("An approval policy with this name already exists")

        before = {"rule_config": policy.rule_config, "priority": policy.priority, "is_active": policy.is_active}

        if "policy_name" in changes:
            policy.policy_name = changes["policy_name"]
        if "description" in changes:
            policy.description = changes["description"]
        if "priority" in changes:
            policy.priority = changes["priority"]
        if "is_active" in changes:
            policy.is_active = changes["is_active"]
        if data.rule is not None:
            policy.rule_config = data.rule.model_dump()

        policy = self.repository.update(policy)
        self.repository.commit()
        policy = self.repository.refresh(policy)

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="policy",
            object_id=policy.id,
            action="updated",
            before_value=before,
            after_value={"rule_config": policy.rule_config, "priority": policy.priority, "is_active": policy.is_active},
        )
        return policy

    def delete_policy(self, policy_id: UUID, current_user: dict) -> None:
        self._require_manage(current_user)
        policy = self._load(policy_id)

        self.repository.delete(policy)
        self.repository.commit()

        log_audit(
            db=self.db,
            tenant_id=current_user["tenant_id"],
            user_id=current_user["id"],
            object_type="policy",
            object_id=policy_id,
            action="deleted",
            before_value={"policy_name": policy.policy_name},
        )

    def _load(self, policy_id: UUID) -> Policy:
        policy = self.repository.get_by_id(policy_id)
        if not policy or policy.policy_type != POLICY_TYPE:
            raise ValueError("Approval policy not found")
        return policy

    @staticmethod
    def _require_manage(current_user: dict) -> None:
        if not has_permission(current_user["role"], PERM_MANAGE_POLICIES):
            raise PermissionError("You do not have permission to manage policies")
