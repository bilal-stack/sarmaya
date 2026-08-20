"""Org units and user scopes.

Build Book, Access Controls: RBAC *with scopes*. Permissions say what a role
may do; these say what it may do it to.

Managing the org structure needs `users.manage` — assigning a scope narrows or
widens what somebody can see, which is an access-control change and belongs
with the permission that grants access in the first place.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.models.org_unit import UNIT_TYPES
from app.services.org_unit_service import OrgUnitService

router = APIRouter(prefix="/org-units", tags=["Org Units"])


class OrgUnitCreate(BaseModel):
    code: str
    name: str
    unit_type: str
    parent_id: Optional[UUID] = None

    @field_validator("code", "name")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("required")
        return v.strip()

    @field_validator("unit_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in UNIT_TYPES:
            raise ValueError(f"one of: {', '.join(UNIT_TYPES)}")
        return v


class OrgUnitResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    code: str
    name: str
    unit_type: str
    parent_id: Optional[UUID] = None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class ScopeAssignment(BaseModel):
    org_unit_id: UUID


def _raise_for(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("", response_model=List[OrgUnitResponse])
def list_org_units(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """The org chart: business units, locations, departments, cost centres."""
    try:
        return OrgUnitService(db).list_units(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("", response_model=OrgUnitResponse, status_code=status.HTTP_201_CREATED)
def create_org_unit(
    payload: OrgUnitCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    try:
        return OrgUnitService(db).create_unit(
            current_user, code=payload.code, name=payload.name,
            unit_type=payload.unit_type, parent_id=payload.parent_id,
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/users/{user_id}/scopes", response_model=List[OrgUnitResponse])
def user_scopes(
    user_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What this user may act within.

    An empty list means unrestricted — they see the whole tenant. That is the
    default for everybody until a scope is assigned.
    """
    try:
        return OrgUnitService(db).scopes_for(user_id, current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.post("/users/{user_id}/scopes", status_code=status.HTTP_200_OK)
def assign_scope(
    user_id: UUID,
    payload: ScopeAssignment,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Grant a unit, and everything beneath it."""
    try:
        OrgUnitService(db).assign(user_id, payload.org_unit_id, current_user)
        return {"success": True}
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.delete("/users/{user_id}/scopes/{org_unit_id}", status_code=status.HTTP_200_OK)
def revoke_scope(
    user_id: UUID,
    org_unit_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Remove a scope.

    Removing the last one widens access to the whole tenant rather than
    narrowing it to nothing — the audit entry says so, because it surprises
    people.
    """
    try:
        OrgUnitService(db).revoke(user_id, org_unit_id, current_user)
        return {"success": True}
    except (ValueError, PermissionError) as e:
        _raise_for(e)
