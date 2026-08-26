from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.services.dashboards import DashboardService
from app.api.deps import get_current_user, get_db_session
from app.schemas.invoice import (
    InvoiceListResponse,
    InvoiceStats
)
from app.services.invoice_service import InvoiceService
from typing import List

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats")
def stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Headline dashboard figures (pending approvals, this month, top vendors).
    Authenticated via the Authorization header; tenant isolation by RLS."""
    return InvoiceService(db).get_dashboard_summary()


# ============================================
# STATS & ANALYTICS
# ============================================

@router.get("/stats/summary", response_model=InvoiceStats)
def get_invoice_stats(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get invoice summary statistics
    Used by dashboard
    """
    service = InvoiceService(db)
    return service.get_stats()


@router.get("/pending", response_model=List[InvoiceListResponse])
def get_pending_approvals(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """
    Get all pending approval invoices
    Shortcut for dashboard
    """
    service = InvoiceService(db)
    return service.get_pending_approvals()


def _raise_for(exc: Exception) -> None:
    """Same mapping the rest of the API uses: refusal is 403, bad input is 400.

    A helper rather than the same two lines under every handler — there are
    eight of them here, and eight copies is eight places for one of them to
    quietly start answering differently.
    """
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

# --- Global dashboards (Build Book lines 265-272) ----------------------------
#
# Seven questions somebody actually asks, each computed from history the system
# already keeps. Uncached on purpose: a stale figure for "what is stuck and
# what is it costing" is worse than a slow page. See app/services/dashboards.py.

@router.get("/overview")
def dashboard_overview(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """All seven in one call, because the page shows them together and seven
    round trips would render it in pieces."""
    try:
        return DashboardService(db).overview(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/control-room")
def control_room(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What is stuck, why, and the cash behind it."""
    try:
        return DashboardService(db).control_room(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/bottlenecks")
def approval_bottlenecks(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Cycle time by step and by role, plus how long the undecided have waited."""
    try:
        return DashboardService(db).approval_bottlenecks(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/exceptions")
def exceptions_heatmap(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What is being blocked, and which vendors account for it."""
    try:
        return DashboardService(db).exceptions_heatmap(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/policy-overrides")
def policy_overrides(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Who set a control aside, how often, and for how much."""
    try:
        return DashboardService(db).policy_overrides(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/sod-violations")
def sod_violations(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Attempts the controls refused, and who made them.

    Gated on audit.view rather than the dashboard permission — it names a
    person and an action they were refused. See the service docstring.
    """
    try:
        return DashboardService(db).sod_violations(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/evidence")
def evidence_completeness(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What would fail an audit right now: missing documents, late approvals."""
    try:
        return DashboardService(db).evidence_completeness(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/reconciliation-health")
def reconciliation_health(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Money that left with nothing accounting for it, by age."""
    try:
        return DashboardService(db).reconciliation_health(current_user)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/stock-accuracy")
def stock_accuracy(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """How often the count disagrees with the system, and what it cost."""
    try:
        return DashboardService(db).stock_accuracy(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/supplier-performance")
def supplier_delivery_performance(
    days: int = Query(180, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Who delivers on time, in full, and undamaged."""
    try:
        return DashboardService(db).supplier_delivery_performance(
            current_user, days=days
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/receipt-to-invoice")
def receipt_to_invoice_latency(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Goods received with no invoice yet — a liability the ledger does not
    show, and the accrual an auditor asks about."""
    try:
        return DashboardService(db).receipt_to_invoice_latency(
            current_user, days=days
        )
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/hiring-pipeline")
def hiring_pipeline(
    days: int = Query(365, ge=1, le=1095),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Time to hire, measured from approval, plus the roles nobody filled."""
    try:
        return DashboardService(db).hiring_pipeline(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/payroll-variance")
def payroll_variance(
    days: int = Query(365, ge=1, le=1095),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What pay changed and why. Movement, never a payroll total — this is
    readable with hr.view while salaries are not."""
    try:
        return DashboardService(db).payroll_variance(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/expense-exceptions")
def expense_exceptions(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """Waived rules, and employees still out of pocket."""
    try:
        return DashboardService(db).expense_exceptions(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)


@router.get("/autopilot-health")
def autopilot_health(
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """What the machine decided, and what came back."""
    try:
        return DashboardService(db).autopilot_health(current_user, days=days)
    except (ValueError, PermissionError) as e:
        _raise_for(e)
