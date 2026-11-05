"""Run log page + order status / cancel."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.config import get_settings
from app.paths import href, redirect
from app.services import order_status as order_status_service
from app.template_helpers import register_template_helpers

router = APIRouter(tags=["runs"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
register_template_helpers(templates.env)

# Page size for /runs (within 20–50 range requested)
PAGE_SIZE = 30


@router.get("/runs")
async def runs_page(request: Request, page: int = Query(1, ge=1)):
    total = db.count_run_logs()
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE
    raw_logs = db.list_run_logs(limit=PAGE_SIZE, offset=offset)
    # Refresh live order status for non-dry recent logs (cap API calls)
    logs = []
    live_refreshes = 0
    for log in raw_logs:
        do_refresh = (not log.get("dry_run")) and live_refreshes < 15
        enriched = order_status_service.enrich_log(log, refresh=do_refresh)
        if do_refresh and enriched.get("order_statuses"):
            live_refreshes += 1
        logs.append(enriched)

    # Compact page window for number links (current ±2)
    window: list[int] = []
    if total_pages:
        start = max(1, page - 2)
        end = min(total_pages, page + 2)
        window = list(range(start, end + 1))

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "logs": logs,
            "dry_run": get_settings().dry_run,
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "page_window": window,
        },
    )


@router.post("/runs/{log_id}/orders/{order_id}/cancel")
async def cancel_order(log_id: int, order_id: str):
    try:
        order_status_service.cancel_log_order(log_id, order_id)
    except Exception as exc:
        return HTMLResponse(
            f"<h1>撤单失败</h1><pre>{exc}</pre>"
            f'<p><a href="{href("/runs")}">返回 Run Log</a></p>',
            status_code=400,
        )
    return redirect("/runs", status_code=303)


@router.post("/runs/{log_id}/refresh")
async def refresh_log_orders(log_id: int):
    log = db.get_run_log(log_id)
    if not log:
        return redirect("/runs", status_code=303)
    order_status_service.enrich_log(log, refresh=True)
    return redirect("/runs", status_code=303)
