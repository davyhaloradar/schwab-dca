"""5-minute VWAP 做T monitor page, SSE stream, and engine controls.

Orders are not placed. POSTs that start, stop, reset, or save settings
go through the app CSRF middleware.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.paths import href, redirect
from app.plan_rules import parse_symbols, validate_tickers
from app.services import t_engine
from app.template_helpers import register_template_helpers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["t"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
register_template_helpers(templates.env)

_MAX_SYMBOLS = 6
_NOTIONAL_MIN = 100.0
_NOTIONAL_MAX = 20000.0


def _script_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _clamp_notional(raw: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 2000.0
    if value < _NOTIONAL_MIN:
        return _NOTIONAL_MIN
    if value > _NOTIONAL_MAX:
        return _NOTIONAL_MAX
    return value


@router.get("/t")
async def t_page(request: Request):
    await asyncio.to_thread(t_engine.tick)
    snap = t_engine.snapshot()
    snap["stream_url"] = href("/t/stream")
    snap["state_url"] = href("/t/state")
    snap["replay_url"] = href("/t/replay")
    snap["backfill_url"] = href("/t/replay/backfill")
    response = templates.TemplateResponse(
        request,
        "t_monitor.html",
        {
            "snap": snap,
            "boot_json": _script_json(snap),
            "error": request.query_params.get("error") or "",
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/t/state")
async def t_state():
    snap = await asyncio.to_thread(t_engine.snapshot)
    return snap


_REPLAY_PRESETS = frozenset({"day", "1w", "1m", "3m", "6m", "1y"})


@router.post("/t/replay/backfill")
async def t_replay_backfill(
    symbol: str = Form(""),
    preset: str = Form("1m", alias="range"),
):
    """Fetch 5m bars for a 回看 window into the local cache. Does not create fills."""
    parsed = parse_symbols(symbol)
    errs = validate_tickers(parsed)
    if len(parsed) != 1:
        errs.append("请选择一个标的")
    if preset not in _REPLAY_PRESETS:
        errs.append("未知回看范围")
    if errs:
        return JSONResponse(
            {"error": errs[0], "sessions": [], "read_only": True, "partial": False},
            status_code=400,
        )
    payload = await asyncio.to_thread(t_engine.ensure_replay_range, parsed[0], preset)
    return payload


@router.get("/t/replay")
async def t_replay(symbol: str = Query(""), date: str = Query("")):
    """Historical session chart. Does not create simulated fills."""
    parsed = parse_symbols(symbol)
    errs = validate_tickers(parsed)
    if len(parsed) != 1:
        errs.append("请选择一个标的")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (date or "").strip()):
        errs.append("日期格式应为 YYYY-MM-DD")
    if errs:
        return JSONResponse({"error": errs[0], "bars": [], "trades": [], "read_only": True}, status_code=400)
    payload = await asyncio.to_thread(t_engine.replay_payload, parsed[0], date.strip())
    return payload


@router.get("/t/stream")
async def t_stream(request: Request, once: int = 0):
    """Server-Sent Events. `once=1` emits a single snapshot and returns."""

    async def gen():
        last = ""
        while True:
            if await request.is_disconnected():
                break
            snap = await asyncio.to_thread(t_engine.snapshot)
            payload = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
            if payload != last:
                yield f"event: snapshot\ndata: {payload}\n\n"
                last = payload
            else:
                yield ": keepalive\n\n"
            if once:
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/t/start")
async def t_start():
    try:
        await asyncio.to_thread(t_engine.start_engine)
    except Exception:
        logger.exception("做T start failed")
        return redirect("/t?error=" + quote("启动失败，请看服务日志"))
    return redirect("/t")


@router.post("/t/stop")
async def t_stop():
    t_engine.stop_engine()
    return redirect("/t")


@router.post("/t/reset")
async def t_reset():
    try:
        await asyncio.to_thread(t_engine.reset_session)
    except Exception:
        logger.exception("做T reset failed")
        return redirect("/t?error=" + quote("清空失败"))
    return redirect("/t")


@router.post("/t/reset-symbol")
async def t_reset_symbol(symbol: str = Form("")):
    """Clear every simulated fill for one symbol (all sessions), not only today."""
    parsed = parse_symbols(symbol)
    errs = validate_tickers(parsed)
    if len(parsed) != 1:
        errs.append("请选择一个标的")
    if not errs and parsed[0] not in db.get_t_config()["symbols"]:
        errs.append("标的不在当前监控列表")
    if errs:
        return redirect("/t?error=" + quote(errs[0]))
    try:
        await asyncio.to_thread(t_engine.reset_symbol_all, parsed[0])
    except Exception:
        logger.exception("做T reset symbol failed")
        return redirect("/t?error=" + quote("清空失败"))
    return redirect("/t")


@router.post("/t/settings")
async def t_settings(
    symbols: str = Form(""),
    notional_usd: float = Form(2000),
):
    parsed = parse_symbols(symbols)
    errs = validate_tickers(parsed)
    if len(parsed) > _MAX_SYMBOLS:
        errs.append(f"最多 {_MAX_SYMBOLS} 个标的")
    if errs:
        return redirect("/t?error=" + quote(errs[0]))
    t_engine.update_settings(parsed, _clamp_notional(notional_usd))
    return redirect("/t")
