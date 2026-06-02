from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.policy import (
    ApprovalPolicyCreate,
    ApprovalPolicyUpdate,
    ApprovalPolicyResponse,
)
from app.schemas.workflow import WorkflowStateResponse, WorkflowTransitionsUpdate
from app.services.policy_service import ApprovalPolicyService
from app.services.workflow_config_service import WorkflowConfigService

router = APIRouter(prefix="/config", tags=["Configuration"])


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ============================================
# APPROVAL POLICIES (approval routing matrix)
# ============================================

@router.get("/approval-policies", response_model=List[ApprovalPolicyResponse])
def list_approval_policies(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """List the tenant's approval-routing rules (read by evaluate_approval_role)."""
    try:
        return ApprovalPolicyService(db).list_policies(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/approval-policies", response_model=ApprovalPolicyResponse, status_code=status.HTTP_201_CREATED)
def create_approval_policy(
    payload: ApprovalPolicyCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ApprovalPolicyService(db).create_policy(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.put("/approval-policies/{policy_id}", response_model=ApprovalPolicyResponse)
def update_approval_policy(
    policy_id: UUID,
    payload: ApprovalPolicyUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return ApprovalPolicyService(db).update_policy(policy_id, payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.delete("/approval-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_approval_policy(
    policy_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        ApprovalPolicyService(db).delete_policy(policy_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# ============================================
# WORKFLOW TRANSITIONS (state machine config)
# ============================================

@router.get("/workflow/{workflow_type}/states", response_model=List[WorkflowStateResponse])
def list_workflow_states(
    workflow_type: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """List a workflow's states and their allowed transitions (read by
    transition_state)."""
    try:
        return WorkflowConfigService(db).list_states(workflow_type, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.put(
    "/workflow/{workflow_type}/states/{state_name}/transitions",
    response_model=WorkflowStateResponse,
)
def update_workflow_transitions(
    workflow_type: str,
    state_name: str,
    payload: WorkflowTransitionsUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Replace the set of states reachable from `state_name`."""
    try:
        return WorkflowConfigService(db).update_transitions(
            workflow_type, state_name, payload.allowed_transitions, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)
