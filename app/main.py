"""Schwab DCA web app — FastAPI entrypoint (supports APP_ROOT_PATH e.g. /plan)."""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import (
    get_settings,
    set_dry_run,
    set_notify_email_enabled,
    set_notify_on_manual,
    set_notify_smtp_settings,
)
from app.csrf import CSRFMiddleware, csrf_input_html
from app.paths import bp, href, redirect
from app.routers import auth, plans, runs, tmonitor
from app.services import schwab_client
from app.services.cash_monitor import cash_forecast_for_plans
from app.services.order_status import (
    dca_holdings_by_plan,
    enrich_holdings_market_value,
    enrich_log,
)
from app.services.scheduler import shutdown as scheduler_shutdown
from app.services.scheduler import start as scheduler_start
from app.template_helpers import register_template_helpers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["bp"] = bp
templates.env.globals["href"] = href
templates.env.globals["dry_run_mode"] = lambda: get_settings().dry_run
templates.env.globals["notify_email_on"] = lambda: get_settings().email_notify_enabled()
templates.env.globals["notify_on_manual"] = lambda: get_settings().dca_notify_on_manual
templates.env.globals["csrf_field"] = csrf_input_html
register_template_helpers(templates.env)


def _backfill_empty_account_hashes() -> int:
    """One-shot: fill plans with empty account_hash from first linked account."""
    status = schwab_client.auth_status()
    if not status.get("linked") or not status.get("refresh_valid"):
        logger.info("account_hash backfill skipped (OAuth not linked)")
        return 0
    auto = schwab_client.first_account_hash()
    if not auto:
        logger.info("account_hash backfill skipped (no account numbers)")
        return 0
    filled = 0
    for plan in db.list_plans():
        if (plan.get("account_hash") or "").strip():
            continue
        if db.set_account_hash_if_empty(plan["id"], auto):
            filled += 1
            logger.info(
                "Auto-bound account_hash for plan #%s (%s)",
                plan["id"],
                plan.get("name"),
            )
    return filled


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    settings = get_settings()
    callback_host = urlparse(settings.schwab_callback_url or "").hostname
    logger.info(
        "DRY_RUN=%s oauth_configured=%s root_path=%s caps shares=%s notional=%s "
        "callback_host=%s callback_host_allowed=%s dashboard=%s",
        settings.dry_run,
        settings.oauth_configured,
        settings.app_root_path or "/",
        settings.dca_max_shares_per_symbol,
        settings.dca_max_notional_usd,
        callback_host or "",
        settings.host_allowed(callback_host),
        settings.public_dashboard_url(),
    )
    try:
        n = _backfill_empty_account_hashes()
        if n:
            logger.info("account_hash backfill filled %s plan(s)", n)
    except Exception as exc:
        logger.warning("account_hash backfill failed: %s", exc)
    scheduler_start()
    yield
    scheduler_shutdown()


# Disable OpenAPI docs in all environments (prod hardening; no public schema).
app = FastAPI(
    title="Schwab DCA",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(CSRFMiddleware)

_prefix = bp()  # evaluated at import; Settings from env
app.mount(f"{_prefix}/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(auth.router, prefix=_prefix)
app.include_router(plans.router, prefix=_prefix)
app.include_router(runs.router, prefix=_prefix)
app.include_router(tmonitor.router, prefix=_prefix)

# Share Jinja helpers with router-local template engines
for _tpl in (plans.templates, runs.templates, tmonitor.templates):
    _tpl.env.globals["bp"] = bp
    _tpl.env.globals["href"] = href
    _tpl.env.globals["dry_run_mode"] = lambda: get_settings().dry_run
    _tpl.env.globals["notify_email_on"] = lambda: get_settings().email_notify_enabled()
    _tpl.env.globals["notify_on_manual"] = lambda: get_settings().dca_notify_on_manual
    _tpl.env.globals["csrf_field"] = csrf_input_html
    register_template_helpers(_tpl.env)


def _account_summary() -> dict:
    status = schwab_client.auth_status()
    summary: dict = {
        "auth": status,
        "accounts": [],
        "accounts_error": None,
    }
    if not status.get("linked") or not status.get("refresh_valid"):
        return summary
    try:
        nums = schwab_client.get_account_numbers()
        summary["accounts"] = nums
    except Exception as exc:
        summary["accounts_error"] = str(exc)
    return summary


def safe_internal_redirect(referer: str | None) -> str:
    """Only allow redirects to same-origin paths under bp() (open-redirect fix)."""
    fallback = href("/")
    if not referer:
        return fallback
    try:
        parsed = urlparse(referer)
    except Exception:
        return fallback
    # Relative path
    path = parsed.path if parsed.scheme or parsed.netloc else referer.split("?", 1)[0]
    if parsed.scheme or parsed.netloc:
        host = (parsed.hostname or "").lower()
        if not get_settings().host_allowed(host):
            return fallback
        path = parsed.path or "/"
    prefix = bp() or ""
    if prefix:
        if path != prefix and not path.startswith(prefix + "/"):
            return fallback
    # Disallow protocol-relative and weird paths
    if "//" in path.replace(prefix, "", 1) or path.startswith("//"):
        return fallback
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path or fallback



def _market_prices_for_plans(plans_list: list) -> dict[str, float | None]:
    """Best-effort last prices for all plan symbols. Missing/failed → None."""
    symbols: list[str] = []
    seen: set[str] = set()
    for p in plans_list:
        for s in p.get("symbols") or []:
            su = str(s).strip().upper()
            if su and su not in seen:
                seen.add(su)
                symbols.append(su)
    if not symbols:
        return {}
    out: dict[str, float | None] = {s: None for s in symbols}
    try:
        quotes = schwab_client.get_quotes(symbols)
    except Exception as exc:
        logger.warning("Dashboard quote fetch failed: %s", exc)
        return out
    for s in symbols:
        out[s] = schwab_client.extract_last_price(quotes, s)
    return out


@app.get(_prefix + "/" if _prefix else "/")
async def dashboard(request: Request):
    settings = get_settings()
    summary = _account_summary()
    plans_list = db.list_plans()
    plan_ids = [p["id"] for p in plans_list] if plans_list else []
    plan_holdings = dca_holdings_by_plan(plan_ids) if plan_ids else {}
    plan_avg_costs = {
        pid: {sym: row["avg_cost"] for sym, row in (h.get("by_symbol") or {}).items()}
        for pid, h in plan_holdings.items()
    }
    market_prices = _market_prices_for_plans(plans_list) if plans_list else {}
    if plan_holdings:
        enrich_holdings_market_value(plan_holdings, market_prices)
    cash_forecast = None
    if summary["auth"].get("linked") and summary["auth"].get("refresh_valid"):
        try:
            cash_forecast = cash_forecast_for_plans(plans_list, prices=market_prices)
        except Exception as exc:
            logger.warning("Dashboard cash forecast failed: %s", exc)
            cash_forecast = {"error": str(exc), "accounts": [], "plans": [], "any_low_cash": False,
                             "warn_threshold": settings.dca_cash_warn_runs}
    recent_raw = db.list_run_logs(limit=5)
    # Reconcile live order status so Recent runs never shows false 成功
    recent = []
    live_refreshes = 0
    for log in recent_raw:
        do_refresh = (not log.get("dry_run")) and live_refreshes < 5
        enriched = enrich_log(log, refresh=do_refresh)
        if do_refresh and enriched.get("order_statuses"):
            live_refreshes += 1
        recent.append(enriched)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "settings": settings,
            "dry_run": settings.dry_run,
            "auth": summary["auth"],
            "accounts": summary["accounts"],
            "accounts_error": summary["accounts_error"],
            "plans": plans_list,
            "plan_avg_costs": plan_avg_costs,
            "plan_holdings": plan_holdings,
            "market_prices": market_prices,
            "cash_forecast": cash_forecast,
            "recent_logs": recent,
            "oauth_configured": settings.oauth_configured,
        },
    )


if _prefix:
    @app.get(_prefix)
    async def dashboard_noslash():
        return redirect("/", status_code=302)


@app.get((_prefix or "") + "/health")
async def health():
    return {"ok": True, "dry_run": get_settings().dry_run, "root": bp() or "/"}


@app.post((_prefix or "") + "/settings/dry-run")
async def settings_dry_run(request: Request):
    """Toggle DRY_RUN; persists to .env and clears settings cache."""
    form = await request.form()
    raw = (form.get("dry_run") or "").strip().lower()
    enabled = raw in ("1", "true", "on", "yes")
    set_dry_run(enabled)
    dest = safe_internal_redirect(request.headers.get("referer"))
    return RedirectResponse(dest, status_code=303)


@app.post((_prefix or "") + "/settings/notify-email")
async def settings_notify_email(request: Request):
    """Toggle DCA_NOTIFY_EMAIL_ENABLED; persists like DRY_RUN."""
    form = await request.form()
    raw = (form.get("enabled") or "").strip().lower()
    enabled = raw in ("1", "true", "on", "yes")
    set_notify_email_enabled(enabled)
    dest = safe_internal_redirect(request.headers.get("referer"))
    return RedirectResponse(dest, status_code=303)


@app.post((_prefix or "") + "/settings/notify-on-manual")
async def settings_notify_on_manual(request: Request):
    """Toggle DCA_NOTIFY_ON_MANUAL; persists like DRY_RUN."""
    form = await request.form()
    raw = (form.get("enabled") or "").strip().lower()
    enabled = raw in ("1", "true", "on", "yes")
    set_notify_on_manual(enabled)
    dest = safe_internal_redirect(request.headers.get("referer"))
    return RedirectResponse(dest, status_code=303)



_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True  # empty allowed (clears / optional)
    return bool(_EMAIL_RE.match(v)) and len(v) <= 254


@app.get((_prefix or "") + "/settings/notify")
async def settings_notify_page(request: Request):
    """通知设置：收件人 / SMTP 凭证（仍可用 .env 覆盖）。"""
    settings = get_settings()
    flash = request.query_params.get("msg") or ""
    flash_err = request.query_params.get("err") or ""
    return templates.TemplateResponse(
        request,
        "notify_settings.html",
        {
            "settings": settings,
            "smtp_password_set": bool((settings.dca_notify_smtp_password or "").strip()),
            "email_configured": bool(
                (settings.dca_notify_email_to or "").strip()
                and (settings.dca_notify_smtp_user or "").strip()
                and (settings.dca_notify_smtp_password or "").strip()
                and settings.email_notify_enabled()
            ),
            "flash": flash,
            "flash_err": flash_err,
        },
    )


@app.post((_prefix or "") + "/settings/notify")
async def settings_notify_save(request: Request):
    """Persist email TO/FROM + SMTP; blank password leaves existing unchanged."""
    form = await request.form()
    email_to = (form.get("email_to") or "").strip()
    email_from = (form.get("email_from") or "").strip()
    smtp_host = (form.get("smtp_host") or "").strip() or "smtp.163.com"
    smtp_user = (form.get("smtp_user") or "").strip()
    raw_port = (form.get("smtp_port") or "465").strip()
    raw_password = form.get("smtp_password")
    # Treat missing or blank password field as "leave unchanged"
    if raw_password is None or str(raw_password) == "":
        smtp_password: str | None = None
    else:
        smtp_password = str(raw_password)

    err: str | None = None
    smtp_port = 465
    if email_to and not _looks_like_email(email_to):
        err = "收件人邮箱格式不正确"
    elif email_from and not _looks_like_email(email_from):
        err = "发件人邮箱格式不正确"
    elif smtp_user and not _looks_like_email(smtp_user):
        err = "SMTP 用户须为完整邮箱（如 you@example.com）"
    else:
        try:
            smtp_port = int(raw_port)
            if smtp_port < 1 or smtp_port > 65535:
                raise ValueError("port out of range")
        except ValueError:
            err = "SMTP 端口无效"

    if err:
        return RedirectResponse(
            href(f"/settings/notify?err={quote(err)}"),
            status_code=303,
        )

    set_notify_smtp_settings(
        email_to=email_to,
        email_from=email_from,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
    )
    return RedirectResponse(
        href(f"/settings/notify?msg={quote('通知邮箱设置已保存')}"),
        status_code=303,
    )


@app.post((_prefix or "") + "/settings/notify/test")
async def settings_notify_test(request: Request):
    """Send one test email with current SMTP config (policy gates bypassed)."""
    # Consume form for CSRF body replay consistency (middleware already checked).
    await request.form()
    from app.services.notify import send_test_email

    ok, message = send_test_email()
    if ok:
        return RedirectResponse(
            href(f"/settings/notify?msg={quote(message)}"),
            status_code=303,
        )
    return RedirectResponse(
        href(f"/settings/notify?err={quote(message)}"),
        status_code=303,
    )
