from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from uuid import UUID

from app.api.deps import get_current_user, get_db_session
from app.schemas.policy import (
    ApprovalPolicyCreate,
    ApprovalPolicyUpdate,
    ApprovalPolicyResponse,
    PolicySimulationRequest,
    PolicySimulationResult,
)
from app.schemas.workflow import WorkflowStateResponse, WorkflowTransitionsUpdate, WorkflowSlaUpdate
from app.services.policy_service import ApprovalPolicyService
from app.services.policy_simulator import PolicySimulator
from app.services.workflow_config_service import WorkflowConfigService
from app.services.config_provisioning import ConfigProvisioningService
from app.schemas.autopilot import AutopilotConfig
from app.services.autopilot_service import AutopilotService
from app.schemas.config_version import ConfigVersionResponse
from app.services.config_versioning import ConfigVersionService

router = APIRouter(prefix="/config", tags=["Configuration"])


# ============================================
# TENANT DEFAULTS
# ============================================

@router.post("/initialize-defaults", status_code=status.HTTP_200_OK)
def initialize_default_config(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Seed this tenant's default invoice workflow + approval matrix if absent.

    Idempotent: returns the number of states/policies created (0 when the tenant
    is already configured). Lets a new tenant become configuration-first without
    a deploy, then edit the seeded rows via the endpoints below.
    """
    try:
        return ConfigProvisioningService(db).initialize_defaults(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


# ============================================
# RESTRICTED AUTOPILOT SETTINGS
# ============================================

@router.get("/autopilot", response_model=AutopilotConfig)
def get_autopilot_config(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Read this tenant's Restricted Autopilot settings (disabled by default)."""
    try:
        return AutopilotService(db).get_config(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.put("/autopilot", response_model=AutopilotConfig)
def set_autopilot_config(
    payload: AutopilotConfig,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Enable/adjust Restricted Autopilot bounds (opt-in, amount limit, vendor and
    duplicate guards)."""
    try:
        return AutopilotService(db).set_config(payload, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


# ============================================
# CONFIG VERSION HISTORY (append-only)
# ============================================

@router.get(
    "/versions/{config_type}/{config_key}",
    response_model=List[ConfigVersionResponse],
)
def list_config_versions(
    config_type: str,
    config_key: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Full edit history (newest first) of a config object: an approval policy
    (`approval_policy/{policy_id}`), the autopilot settings (`autopilot/autopilot`),
    or a workflow (`workflow/{workflow_type}`). Each entry is the post-change JSON
    snapshot under a monotonic version number."""
    try:
        return ConfigVersionService(db).list_versions(config_type, config_key, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get(
    "/versions/{config_type}/{config_key}/{version}",
    response_model=ConfigVersionResponse,
)
def get_config_version(
    config_type: str,
    config_key: str,
    version: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Fetch a single historical version snapshot of a config object."""
    try:
        return ConfigVersionService(db).get_version(config_type, config_key, version, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post(
    "/versions/{config_type}/{config_key}/{version}/restore",
    response_model=ConfigVersionResponse,
)
def restore_config_version(
    config_type: str,
    config_key: str,
    version: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Roll a config object back to a previous version. Re-applies that version's
    snapshot as the current config and records the rollback as a new version
    (change_action="restored"). Returns the new version entry."""
    try:
        return ConfigVersionService(db).restore_version(config_type, config_key, version, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


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


@router.post("/approval-policies/simulate", response_model=PolicySimulationResult)
def simulate_approval_policy(
    payload: PolicySimulationRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Policy Simulator: what a proposed approval matrix would have done.

    Replays the proposed rules over the tenant's historical invoices and reports
    how routing shifts between roles, which invoices move and by how much value,
    and — optionally — how many would qualify under a given autopilot limit.

    Strictly read-only: nothing is written and no live policy is touched, so a
    threshold change can be checked before anyone commits to it.
    """
    try:
        return PolicySimulator(db).simulate(
            current_user,
            proposed_rules=[
                {"policy_name": r.policy_name, "priority": r.priority,
                 "rule_config": r.rule.model_dump()}
                for r in payload.proposed_rules
            ],
            window_days=payload.window_days,
            autopilot_limit=payload.autopilot_limit,
        )
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


@router.put(
    "/workflow/{workflow_type}/states/{state_name}/sla",
    response_model=WorkflowStateResponse,
)
def update_workflow_sla(
    workflow_type: str,
    state_name: str,
    payload: WorkflowSlaUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Set or clear the SLA for sitting in `state_name` — {"hours": 48,
    "escalate_to": "cfo"}. The timer starts when an object enters the state;
    breaches surface as overdue in the Decision Inbox and can be escalated."""
    try:
        return WorkflowConfigService(db).update_sla(
            workflow_type, state_name, payload.hours, payload.escalate_to, current_user
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)
