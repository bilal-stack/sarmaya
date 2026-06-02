from sqlalchemy.orm import Session
from typing import Optional, List

from app.models.workflow_state import WorkflowState


class WorkflowRepository:
    """Repository for WorkflowState configuration rows.

    Tenant isolation is enforced by Postgres RLS, so queries carry no tenant_id
    filter.
    """

    def __init__(self, db: Session):
        self.db = db

    def list_states(self, workflow_type: str) -> List[WorkflowState]:
        return (
            self.db.query(WorkflowState)
            .filter(WorkflowState.workflow_type == workflow_type)
            .order_by(WorkflowState.state_order.asc())
            .all()
        )

    def get_state(self, workflow_type: str, state_name: str) -> Optional[WorkflowState]:
        return self.db.query(WorkflowState).filter(
            WorkflowState.workflow_type == workflow_type,
            WorkflowState.state_name == state_name.lower(),
        ).first()

    def update(self, state: WorkflowState) -> WorkflowState:
        self.db.add(state)
        self.db.flush()
        return state

    def commit(self) -> None:
        self.db.commit()

    def refresh(self, state: WorkflowState) -> WorkflowState:
        self.db.refresh(state)
        return state
