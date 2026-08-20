"""Dashboards, as files.

One route for all seven rather than seven near-identical ones. The reports
differ in their arguments, not in what exporting means, and seven copies is
seven places for one to quietly start behaving differently — which for an
export means a file that disagrees with the screen it came from.

CSV carries one table, because a CSV holding several tables separated by blank
lines opens tidily in Excel and is unparseable by everything else. HTML carries
the whole report and prints to PDF from any browser, which is how you get a PDF
here without putting a rendering library in the path that produces audit
evidence.
"""
import logging
from typing import Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db_session
from app.services.dashboards import DashboardService
from app.services.export_service import (
    summary_of, tables_in, to_csv, to_html,
)
from app.utils.datetime_helpers import utc_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

#: Route name -> (service method, takes a `days` window, human title). Keyed by
#: the same slugs the read endpoints use, so the export of a report is its URL
#: plus `/export` rather than a second vocabulary to learn.
REPORTS: Dict[str, tuple] = {
    "control-room": ("control_room", False, "Executive control room"),
    "bottlenecks": ("approval_bottlenecks", True, "Approval bottlenecks"),
    "exceptions": ("exceptions_heatmap", True, "Exceptions heatmap"),
    "policy-overrides": ("policy_overrides", True, "Policy overrides"),
    "evidence": ("evidence_completeness", False, "Audit evidence completeness"),
    "reconciliation-health": ("reconciliation_health", False, "Reconciliation health"),
    "autopilot-health": ("autopilot_health", True, "Autopilot health"),
    # Variant D.
    "stock-accuracy": ("stock_accuracy", True, "Stock accuracy and adjustments"),
    "supplier-performance": (
        "supplier_delivery_performance", True, "Supplier delivery performance",
    ),
    "receipt-to-invoice": (
        "receipt_to_invoice_latency", True, "Receipt to invoice latency",
    ),
    # Variant C.
    "hiring-pipeline": ("hiring_pipeline", True, "Hiring pipeline"),
    "payroll-variance": ("payroll_variance", True, "Payroll variance"),
    "expense-exceptions": ("expense_exceptions", True, "Expense exceptions"),
}


def _filename(report: str, extension: str) -> str:
    """Dated, so a folder of these stays sortable and nothing overwrites."""
    return f"sarmaya-{report}-{utc_now().date().isoformat()}.{extension}"


def _disposition(name: str) -> str:
    return f'attachment; filename="{name}"'


@router.get("/{report}/export")
def export_report(
    report: str,
    format: str = Query("csv", pattern="^(csv|html|json)$"),
    table: Optional[str] = Query(
        None,
        description="Which table to export as CSV. Defaults to the report's "
                    "largest. Ignored for html and json, which carry all of them.",
    ),
    days: int = Query(90, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db_session),
):
    """A dashboard as a file: csv (one table), html (all of it), or json.

    The figures are recomputed for the export rather than accepted from the
    caller. An endpoint that formatted a posted payload would let anyone
    produce an official-looking company report containing whatever they sent
    it — which, on a document that carries the tenant's name and is meant to
    be filed, is the whole risk.
    """
    if report not in REPORTS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown report. One of: {', '.join(sorted(REPORTS))}",
        )

    method_name, takes_days, title = REPORTS[report]
    method: Callable = getattr(DashboardService(db), method_name)

    try:
        payload = method(current_user, days=days) if takes_days else method(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    tables = tables_in(payload)
    summary = summary_of(payload)

    if format == "json":
        return payload

    if format == "csv":
        if table and table not in tables:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"This report has no table named '{table}'. "
                    f"Available: {', '.join(sorted(tables)) or 'none'}"
                ),
            )
        if table:
            columns, rows = tables[table]
            chosen = table
        elif tables:
            # The largest, which is the one somebody means by "the data" —
            # a report's headline table is its longest in every case here.
            chosen = max(tables, key=lambda name: len(tables[name][1]))
            columns, rows = tables[chosen]
        else:
            # A report that is all scalars still has to export something, and
            # an empty file would read as a broken download.
            chosen = "summary"
            columns, rows = ["measure", "value"], summary

        name = _filename(f"{report}-{chosen}", "csv")
        return Response(
            content=to_csv(columns, rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": _disposition(name)},
        )

    sections = [("Summary", ["measure", "value"], summary)]
    sections.extend(
        (name.replace("_", " ").capitalize(), columns, rows)
        for name, (columns, rows) in tables.items()
    )

    document = to_html(
        title=title,
        subtitle="Sarmaya OS — generated from stored history, not from cached counters.",
        sections=sections,
        meta={
            "generated_at": utc_now().isoformat(timespec="seconds"),
            "generated_by": current_user.get("email") or current_user.get("id"),
            "window": f"{days} days" if takes_days else "current state",
        },
    )
    return Response(
        content=document,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": _disposition(_filename(report, "html"))},
    )
