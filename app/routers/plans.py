"""DCA plan CRUD + manual run."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.template_helpers import register_template_helpers

from app import db
from app.config import get_settings
from app.paths import href, redirect
from app.plan_rules import (
    normalize_schedule_fields,
    parse_symbols,
    symbols_csv,
    validate_tickers,
)
from app.services import dca as dca_service
from app.services import schwab_client
from app.services.dca import (
    CapExceededError,
    IdempotencyError,
    PlanBusyError,
    PolicyRejectError,
    check_plan_caps,
    try_acquire_plan,
)
from app.services.order_status import live_fill_markers_for_plan
from app.services.price_history import get_daily_history
from app.services.scheduler import remove_plan_job, sync_plan_job

router = APIRouter(prefix="/plans", tags=["plans"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
register_template_helpers(templates.env)

WEEKDAYS = [
    (0, "周一 Mon"),
    (1, "周二 Tue"),
    (2, "周三 Wed"),
    (3, "周四 Thu"),
    (4, "周五 Fri"),
    (5, "周六 Sat"),
    (6, "周日 Sun"),
]



def _auto_account_hash(current: str | None = None) -> str | None:
    """If current empty and OAuth linked, bind first (or sole) account hash."""
    cur = (current or "").strip() or None
    if cur:
        return cur
    return schwab_client.first_account_hash()


def _form_to_data(
    name: str,
    symbols: str,
    amount_usd: float,
    shares_per_symbol: int,
    schedule_type: str,
    weekday: int,
    day_of_month: int,
    time_hhmm: str,
    timezone: str,
    cron_expr: str,
    account_hash: str,
    enabled: str | None,
) -> dict:
    data = {
        "name": name.strip() or "DCA Plan",
        "symbols": symbols_csv(parse_symbols(symbols)),
        "amount_usd": amount_usd,
        "shares_per_symbol": int(shares_per_symbol or 0),
        "schedule_type": ("daily" if schedule_type == "cron" else schedule_type) or "daily",
        "weekday": weekday,
        "day_of_month": day_of_month,
        "time_hhmm": time_hhmm.strip() or "10:00",
        "timezone": timezone.strip() or "America/New_York",
        "cron_expr": None,
        "account_hash": account_hash.strip() or None,
        "enabled": enabled == "on" or enabled == "true" or enabled == "1",
    }
    return normalize_schedule_fields(data)


def _validation_html(msgs: list[str]) -> HTMLResponse:
    body = (
        "<h1>Plan validation failed</h1><pre>"
        + "\n".join(msgs)
        + f'</pre><p><a href="{href("/")}">Back</a></p>'
    )
    return HTMLResponse(body, status_code=400)


def _busy_html(exc: Exception) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>Plan busy</h1><pre>{exc}</pre>"
        f'<p><a href="{href("/")}">Dashboard</a></p>',
        status_code=409,
    )


def _validate_caps_or_html(data: dict) -> HTMLResponse | None:
    """Soft-block create/update when clearly over share/amount caps (no prices yet)."""
    symbols_raw = data.get("symbols") or ""
    if isinstance(symbols_raw, str):
        n = len([s for s in symbols_raw.replace(";", ",").split(",") if s.strip()])
    else:
        n = len(symbols_raw or [])
    msgs = check_plan_caps(
        shares_per_symbol=int(data.get("shares_per_symbol") or 0),
        amount_usd=float(data.get("amount_usd") or 0),
        n_symbols=max(n, 1),
        prices=None,
    )
    if not msgs:
        return None
    # Still allow save but warn strongly — LIVE execute will hard-fail.
    # Refuse save only when shares or amount alone already exceed (clear misconfig).
    settings = get_settings()
    hard = []
    if int(data.get("shares_per_symbol") or 0) > int(settings.dca_max_shares_per_symbol):
        hard.append(msgs[0] if msgs else "shares over cap")
    if float(data.get("amount_usd") or 0) > float(settings.dca_max_notional_usd):
        hard.extend([m for m in msgs if "amount_usd" in m or "NOTIONAL" in m])
    if not hard:
        return None
    body = (
        "<h1>Plan exceeds hard caps</h1><pre>"
        + "\n".join(hard)
        + f"</pre><p>Tune via DCA_MAX_SHARES_PER_SYMBOL / DCA_MAX_NOTIONAL_USD "
        f"(defaults {settings.dca_max_shares_per_symbol} / {settings.dca_max_notional_usd}).</p>"
        f'<p><a href="{href("/")}">Back</a></p>'
    )
    return HTMLResponse(body, status_code=400)


@router.get("/new")
async def plan_new_form(request: Request):
    suggested = _auto_account_hash(None)
    return templates.TemplateResponse(
        request,
        "plan_form.html",
        {
            "plan": None,
            "weekdays": WEEKDAYS,
            "dry_run": get_settings().dry_run,
            "title": "新建定投计划 / New DCA Plan",
            "suggested_account_hash": suggested,
            "account_auto_bound": bool(suggested),
        },
    )


@router.post("/new")
async def plan_create(
    name: str = Form(...),
    symbols: str = Form(...),
    amount_usd: float = Form(0),
    shares_per_symbol: int = Form(0),
    schedule_type: str = Form("daily"),
    weekday: int = Form(0),
    day_of_month: int = Form(1),
    time_hhmm: str = Form("10:00"),
    timezone: str = Form("America/New_York"),
    cron_expr: str = Form(""),
    account_hash: str = Form(""),
    enabled: str | None = Form(None),
):
    data = _form_to_data(
        name, symbols, amount_usd, shares_per_symbol, schedule_type, weekday, day_of_month,
        time_hhmm, timezone, cron_expr, account_hash, enabled,
    )
    # Auto-bind when empty + OAuth linked (first / sole account hash)
    if not (data.get("account_hash") or "").strip():
        auto = _auto_account_hash(None)
        if auto:
            data["account_hash"] = auto
    sym_errs = validate_tickers(parse_symbols(data.get("symbols")))
    if sym_errs:
        return _validation_html(sym_errs)
    blocked = _validate_caps_or_html(data)
    if blocked:
        return blocked
    plan_id = db.create_plan(data)
    plan = db.get_plan(plan_id)
    if plan:
        sync_plan_job(plan)
    return redirect("/", status_code=303)



@router.get("/{plan_id}/chart")
async def plan_chart(request: Request, plan_id: int, symbol: str | None = None):
    """Daily price chart with live DCA fill markers (read-only GET)."""
    plan = db.get_plan(plan_id)
    if not plan:
        return redirect("/", status_code=303)
    symbols = list(plan.get("symbols") or [])
    if not symbols:
        return templates.TemplateResponse(
            request,
            "plan_chart.html",
            {
                "plan": plan,
                "symbols": [],
                "active_symbol": None,
                "candles": [],
                "markers": [],
                "all_markers": {},
                "source": "none",
                "history_error": "计划没有标的代码",
                "has_live_fills": False,
            },
        )
    active = (symbol or "").strip().upper()
    if active not in symbols:
        active = symbols[0]
    allowed = set(symbols)
    all_markers = {
        k: v
        for k, v in live_fill_markers_for_plan(plan_id).items()
        if k in allowed
    }
    markers = all_markers.get(active) or []
    has_live = any(bool(v) for v in all_markers.values())
    candles: list = []
    source = "none"
    history_error = None
    try:
        candles, source = get_daily_history(active)
    except Exception as exc:
        history_error = str(exc)
        candles, source = [], "none"
    # Align marker Y to fill price; if missing close that day still plot fill price
    return templates.TemplateResponse(
        request,
        "plan_chart.html",
        {
            "plan": plan,
            "symbols": symbols,
            "active_symbol": active,
            "candles": candles,
            "markers": markers,
            "all_markers": all_markers,
            "source": source,
            "history_error": history_error,
            "has_live_fills": has_live,
        },
    )


@router.get("/{plan_id}/edit")
async def plan_edit_form(request: Request, plan_id: int):
    plan = db.get_plan(plan_id)
    if not plan:
        return redirect("/", status_code=303)
    auto_bound = False
    if not (plan.get("account_hash") or "").strip():
        auto = _auto_account_hash(None)
        if auto and db.set_account_hash_if_empty(plan_id, auto):
            plan = db.get_plan(plan_id) or plan
            auto_bound = True
        elif auto:
            # Race / already filled; refresh
            plan = db.get_plan(plan_id) or plan
            auto_bound = bool((plan.get("account_hash") or "").strip())
    return templates.TemplateResponse(
        request,
        "plan_form.html",
        {
            "plan": plan,
            "weekdays": WEEKDAYS,
            "dry_run": get_settings().dry_run,
            "title": f"编辑计划 / Edit Plan #{plan_id}",
            "suggested_account_hash": None,
            "account_auto_bound": auto_bound,
        },
    )


@router.post("/{plan_id}/edit")
async def plan_update(
    plan_id: int,
    name: str = Form(...),
    amount_usd: float = Form(0),
    shares_per_symbol: int = Form(0),
    schedule_type: str = Form("daily"),
    weekday: int = Form(0),
    day_of_month: int = Form(1),
    time_hhmm: str = Form("10:00"),
    timezone: str = Form("America/New_York"),
    cron_expr: str = Form(""),
    enabled: str | None = Form(None),
    # symbols / account_hash accepted if present but ignored — locked after create
    symbols: str = Form(""),
    account_hash: str = Form(""),
):
    existing = db.get_plan(plan_id)
    if not existing:
        return redirect("/", status_code=303)
    # Keep DB symbols so run_logs / chart markers stay coherent.
    # account_hash: locked once set; if empty, allow one-time fill from form (LIVE needs it).
    locked_symbols = existing.get("symbols_raw") or ",".join(existing.get("symbols") or [])
    existing_hash = (existing.get("account_hash") or "").strip()
    if existing_hash:
        locked_account = existing_hash
    else:
        # Prefer form value; else auto-bind from linked Schwab accounts
        locked_account = (account_hash or "").strip() or (_auto_account_hash(None) or "")
    data = _form_to_data(
        name, locked_symbols, amount_usd, shares_per_symbol, schedule_type, weekday, day_of_month,
        time_hhmm, timezone, cron_expr, locked_account, enabled,
    )
    blocked = _validate_caps_or_html(data)
    if blocked:
        return blocked
    db.update_plan(plan_id, data)
    plan = db.get_plan(plan_id)
    if plan:
        sync_plan_job(plan)
    return redirect("/", status_code=303)


@router.post("/{plan_id}/delete")
async def plan_delete(plan_id: int):
    """Remove plan + scheduler job. run_logs soft-kept (plan_id → NULL)."""
    lock = try_acquire_plan(plan_id)
    if lock is None:
        return _busy_html(
            PlanBusyError(f"Plan {plan_id} is running; refuse delete until run finishes")
        )
    try:
        remove_plan_job(plan_id)
        db.delete_plan(plan_id)
    finally:
        lock.release()
    return redirect("/", status_code=303)


@router.post("/{plan_id}/toggle")
async def plan_toggle(plan_id: int):
    """Pause / resume: flip enabled and resync scheduler job."""
    lock = try_acquire_plan(plan_id)
    if lock is None:
        return _busy_html(
            PlanBusyError(f"Plan {plan_id} is running; refuse toggle until run finishes")
        )
    try:
        plan = db.get_plan(plan_id)
        if not plan:
            return redirect("/", status_code=303)
        new_enabled = not bool(plan.get("enabled"))
        db.set_plan_enabled(plan_id, new_enabled)
        plan = db.get_plan(plan_id)
        if plan:
            sync_plan_job(plan)
    finally:
        lock.release()
    return redirect("/", status_code=303)


def _run_error_html(title: str, exc: Exception, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"<h1>{title}</h1><pre>{exc}</pre>"
        f'<p><a href="{href("/runs")}">Run Log</a> · <a href="{href("/")}">Dashboard</a></p>',
        status_code=status,
    )


@router.get("/{plan_id}/run/preview")
async def plan_run_preview(request: Request, plan_id: int):
    """LIVE two-step step 1: show sized orders + cash/policy before confirm."""
    plan = db.get_plan(plan_id)
    if not plan:
        return redirect("/", status_code=303)
    settings = get_settings()
    if settings.dry_run:
        # Dry-run stays one-click; preview page not needed
        return redirect(f"/plans/{plan_id}/edit", status_code=303)
    try:
        preview = dca_service.preview_plan(plan_id)
    except Exception as exc:
        return _run_error_html("Preview failed", exc, 400)
    return templates.TemplateResponse(
        request,
        "plan_run_preview.html",
        {
            "plan": plan,
            "preview": preview,
            "dry_run": False,
            "title": f"确认实盘下单 / Confirm LIVE · #{plan_id}",
        },
    )


@router.post("/{plan_id}/run")
async def plan_run_now(plan_id: int, confirm: str | None = Form(None)):
    """Manual trigger.

    DRY_RUN=true: one-click execute.
    LIVE: require confirm=1 (from preview form + CSRF). Bare POST without confirm
    redirects to preview (Feature 3 two-step).
    """
    settings = get_settings()
    if not settings.dry_run and confirm != "1":
        return redirect(f"/plans/{plan_id}/run/preview", status_code=303)
    try:
        dca_service.execute_plan(plan_id, trigger="manual")
    except PlanBusyError as exc:
        return _run_error_html("Run blocked (busy)", exc, 409)
    except IdempotencyError as exc:
        return _run_error_html("Run blocked (idempotency)", exc, 409)
    except CapExceededError as exc:
        return _run_error_html("LIVE refused (hard cap)", exc, 400)
    except PolicyRejectError as exc:
        return _run_error_html("LIVE refused (policy)", exc, 400)
    except Exception as exc:
        return _run_error_html("Run failed", exc, 400)
    return redirect("/runs", status_code=303)
