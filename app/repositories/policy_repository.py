from sqlalchemy.orm import Session
from typing import Optional, List
from uuid import UUID

from app.models.policy import Policy


class PolicyRepository:
    """Repository for Policy rows.

    Tenant isolation is enforced by Postgres RLS (see get_db_session), so
    queries here carry no tenant_id filter.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, policy_id: UUID) -> Optional[Policy]:
        return self.db.query(Policy).filter(Policy.id == policy_id).first()

    def get_by_name(self, policy_type: str, policy_name: str) -> Optional[Policy]:
        return self.db.query(Policy).filter(
            Policy.policy_type == policy_type,
            Policy.policy_name == policy_name,
        ).first()

    def list_by_type(self, policy_type: str) -> List[Policy]:
        return (
            self.db.query(Policy)
            .filter(Policy.policy_type == policy_type)
            .order_by(Policy.priority.desc(), Policy.policy_name.asc())
            .all()
        )

    def create(self, policy: Policy) -> Policy:
        self.db.add(policy)
        self.db.flush()
        return policy

    def update(self, policy: Policy) -> Policy:
        self.db.add(policy)
        self.db.flush()
        return policy

    def delete(self, policy: Policy) -> None:
        self.db.delete(policy)
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, policy: Policy) -> Policy:
        self.db.refresh(policy)
        return policy
