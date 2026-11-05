"""Live 5-minute VWAP 做T monitor.

Dry-run only. This module never calls the Schwab Trader order API.
The scheduler polls bars; the page streams the cached snapshot over SSE.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta, timezone
from typing import Any

from app import db
from app.config import get_settings
from app.services import t_market
from app.services.t_market import MarketSnap, QuoteSnap
from app.services.vwap_mr import (
    ET,
    FORCE_FLAT_AT,
    PRICE_SOURCE_LABELS,
    REASON_LABELS,
    Intrabar,
    OpenT,
    SignalFill,
    chart_time,
    level_prices,
    on_closed_bar,
    params_for,
    with_session_vwap,
)
from app.template_helpers import china_time

logger = logging.getLogger(__name__)

_fetch_lock = threading.Lock()
_view_lock = threading.Lock()
_view: dict[str, Any] = {"series": {}, "errors": [], "fetched_at": None}
_live_warned = False

# Just-closed bar: use the live quote as the simulated fill.
_LIVE_FILL_WINDOW = timedelta(seconds=180)


def _warn_live_flag() -> None:
    global _live_warned
    if _live_warned:
        return
    if get_settings().t_live_orders:
        logger.warning("T_LIVE_ORDERS is set but 做T v1 does not place Schwab orders")
    _live_warned = True


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _split_closed(bars: list[Intrabar], now: datetime) -> tuple[list[Intrabar], Intrabar | None]:
    closed: list[Intrabar] = []
    forming: Intrabar | None = None
    for bar in bars:
        end = bar.start + timedelta(minutes=5)
        if end <= now:
            closed.append(bar)
        else:
            forming = bar
            break
    return closed, forming


def _pos_from_state(state: dict[str, Any] | None) -> OpenT | None:
    if not state or not state.get("open_qty"):
        return None
    entry_ts = _parse_iso(state.get("open_entry_ts"))
    entry_px = state.get("open_entry_px")
    if entry_ts is None or entry_px is None:
        return None
    return OpenT(entry_ts, float(entry_px), int(state["open_qty"]))


def _persist_step(
    *,
    symbol: str,
    session_date: str,
    pos: OpenT | None,
    last_bar: datetime,
    flattened: bool,
    fill: SignalFill | None,
) -> None:
    if fill is not None:
        inserted = db.insert_t_fill(
            session_date=session_date,
            symbol=symbol,
            side=fill.side,
            qty=fill.qty,
            price=fill.price,
            pnl=fill.pnl,
            reason=fill.reason,
            price_source=fill.price_source,
            bar_ts=_iso(fill.bar_start),
        )
        if inserted:
            logger.info(
                "做T simulate %s %s %s qty=%s px=%.4f reason=%s src=%s",
                session_date,
                symbol,
                fill.side,
                fill.qty,
                fill.price,
                fill.reason,
                fill.price_source,
            )
    db.upsert_t_state(
        symbol=symbol,
        session_date=session_date,
        open_entry_ts=_iso(pos.entry_ts) if pos else None,
        open_entry_px=pos.entry_px if pos else None,
        open_qty=pos.qty if pos else None,
        last_bar_ts=_iso(last_bar),
        flattened=flattened,
    )


def process_closed_bars(
    symbol: str,
    session_date: str,
    bars: list[Intrabar],
    *,
    now: datetime,
    quote: QuoteSnap | None = None,
    notional: float | None = None,
) -> None:
    """Walk newly closed bars for one session and persist simulated fills.

    `bars` is the raw regular-hours session (quote is not baked into OHLC).
    Safe to call again: already-processed bars are skipped via last_bar_ts.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cfg = db.get_t_config()
    notional_usd = float(cfg["notional_usd"] if notional is None else notional)
    params = params_for(symbol, notional_usd)
    closed, _forming = _split_closed(bars, now)
    if not closed:
        return

    state = db.get_t_state(symbol, session_date)
    last = _parse_iso(state.get("last_bar_ts")) if state else None
    pos = _pos_from_state(state)
    flattened = bool(state and state.get("flattened"))
    priced = with_session_vwap(closed)
    live_px, live_src = (None, "")
    if quote is not None:
        live_px, live_src = quote.fill_px()

    for index, (bar, vwap) in enumerate(priced):
        if last is not None and bar.start <= last:
            continue
        bar_end = bar.start + timedelta(minutes=5)
        is_latest = index == len(priced) - 1
        use_live = bool(
            is_latest
            and live_px
            and bar_end <= now
            and (now - bar_end) <= _LIVE_FILL_WINDOW
        )
        pos, fill, flattened = on_closed_bar(
            bar,
            vwap,
            pos,
            params,
            day_flattened=flattened,
            exec_px=live_px if use_live else None,
            exec_source=live_src if use_live else "trigger",
        )
        _persist_step(
            symbol=symbol,
            session_date=session_date,
            pos=pos,
            last_bar=bar.start,
            flattened=flattened,
            fill=fill,
        )
        last = bar.start

    if pos is None or last is None:
        return
    last_bar = closed[-1]
    last_end = last_bar.start + timedelta(minutes=5)
    now_et = now.astimezone(ET)
    session_done = now_et.time() >= time(16, 0) or (
        last_bar.start.astimezone(ET).time() >= time(15, 55) and last_end <= now
    )
    if not session_done or last < last_bar.start:
        return
    # Data gap: flatten time passed and the position is still open.
    safety = SignalFill(
        last_bar.start,
        "SELL",
        pos.qty,
        last_bar.close,
        "eod_safety",
        (last_bar.close - pos.entry_px) * pos.qty,
        "trigger",
    )
    _persist_step(
        symbol=symbol,
        session_date=session_date,
        pos=None,
        last_bar=last_bar.start,
        flattened=True,
        fill=safety,
    )


def _display_bars(snap: MarketSnap, now: datetime, notional: float) -> list[dict[str, Any]]:
    closed, forming = _split_closed(snap.bars, now)
    display = list(closed)
    if forming is not None:
        display.append(t_market.merge_quote_bar(forming, snap.quote, now))
    params = params_for(snap.symbol, notional)
    rows: list[dict[str, Any]] = []
    for bar, vwap in with_session_vwap(display):
        levels = level_prices(vwap, params)
        rows.append(
            {
                "time": chart_time(bar.start),
                "open": round(bar.open, 4),
                "high": round(bar.high, 4),
                "low": round(bar.low, 4),
                "close": round(bar.close, 4),
                "volume": bar.volume,
                "vwap": round(vwap, 4),
                "entry": round(levels["entry"], 4),
                "exit": round(levels["exit"], 4),
                "stop": round(levels["stop"], 4),
            }
        )
    return rows


def _display_closed(symbol: str, bars: list[Intrabar], notional: float) -> list[dict[str, Any]]:
    """Static bars for a finished session. No live quote, no new fills."""
    if not bars:
        return []
    day = bars[0].start.astimezone(ET).date().isoformat()
    closed_at = bars[-1].start + timedelta(minutes=10)
    snap = MarketSnap(
        symbol=symbol,
        session_date=day,
        is_today=False,
        bars=list(bars),
        source="",
        quote=None,
    )
    return _display_bars(snap, closed_at, notional)


def _history_rows(snap: MarketSnap, notional: float, now: datetime) -> dict[str, list[dict[str, Any]]]:
    sessions = snap.sessions or {}
    if not sessions and snap.session_date and snap.bars:
        sessions = {snap.session_date: list(snap.bars)}
    out: dict[str, list[dict[str, Any]]] = {}
    for day, day_bars in sessions.items():
        if snap.is_today and day == snap.session_date:
            out[day] = _display_bars(snap, now, notional)
        else:
            out[day] = _display_closed(snap.symbol, day_bars, notional)
    return out


def _quote_dict(quote: QuoteSnap | None) -> dict[str, float | None] | None:
    if quote is None:
        return None
    if not (quote.last or quote.mid or quote.bid or quote.ask):
        return None
    return {"last": quote.last, "bid": quote.bid, "ask": quote.ask, "mid": quote.mid}


def _load_one(symbol: str, now: datetime) -> MarketSnap:
    return t_market.load_symbol(symbol, now)


def tick(now: datetime | None = None) -> None:
    """Fetch bars (and, when the engine is running, simulate new fills)."""
    if not _fetch_lock.acquire(blocking=False):
        return
    try:
        _tick_body(now or datetime.now(timezone.utc))
    finally:
        _fetch_lock.release()


def _tick_body(now: datetime) -> None:
    _warn_live_flag()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cfg = db.get_t_config()
    symbols: list[str] = list(cfg["symbols"])
    running = bool(cfg["running"])
    loaded: dict[str, MarketSnap] = {}
    errors: list[str] = []
    if symbols:
        workers = min(4, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_load_one, sym, now): sym for sym in symbols}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    loaded[sym] = fut.result()
                except Exception as exc:
                    errors.append(f"{sym}: {exc}")
                    logger.info("做T market load %s failed: %s", sym, exc)

    if running:
        for sym, snap in loaded.items():
            if not snap.is_today or not snap.session_date:
                continue
            try:
                process_closed_bars(
                    sym,
                    snap.session_date,
                    snap.bars,
                    now=now,
                    quote=snap.quote,
                    notional=float(cfg["notional_usd"]),
                )
            except Exception:
                logger.exception("做T signal step failed for %s", sym)
                errors.append(f"{sym}: 信号计算失败")

    series: dict[str, Any] = {}
    history: dict[str, dict[str, list[dict[str, Any]]]] = {}
    sources: dict[str, str | None] = {}
    with _view_lock:
        previous = dict(_view.get("series") or {})
    for sym in symbols:
        snap = loaded.get(sym)
        if snap is None:
            if sym in previous:
                series[sym] = previous[sym]
            else:
                series[sym] = {
                    "symbol": sym,
                    "session_date": None,
                    "is_today": False,
                    "source": None,
                    "bars": [],
                    "quote": None,
                    "error": next((e for e in errors if e.startswith(sym)), "无行情"),
                }
            continue
        notional = float(cfg["notional_usd"])
        series[sym] = {
            "symbol": sym,
            "session_date": snap.session_date,
            "is_today": snap.is_today,
            "source": snap.source,
            "bars": _display_bars(snap, now, notional),
            "quote": _quote_dict(snap.quote),
            "error": None,
        }
        history[sym] = _history_rows(snap, notional, now)
        sources[sym] = snap.source

    with _view_lock:
        kept_history = dict(_view.get("history") or {})
        kept_sources = dict(_view.get("sources") or {})
        for sym in symbols:
            if sym not in history and sym in kept_history:
                history[sym] = kept_history[sym]
                sources[sym] = kept_sources.get(sym)
        _view["series"] = series
        _view["history"] = history
        _view["sources"] = sources
        _view["errors"] = errors
        _view["fetched_at"] = _iso(now)
        _view["market_phase"] = t_market.market_phase(now)
        _view["now_et"] = now.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _fill_public(row: dict[str, Any]) -> dict[str, Any]:
    bar = _parse_iso(row.get("bar_ts"))
    et_text = "—"
    if bar is not None:
        et_text = bar.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    return {
        "id": row["id"],
        "time": chart_time(bar) if bar is not None else None,
        "time_et": et_text,
        "time_cn": china_time(row.get("bar_ts")),
        "session_date": row["session_date"],
        "symbol": row["symbol"],
        "side": row["side"],
        "qty": row["qty"],
        "price": row["price"],
        "pnl": row["pnl"],
        "reason": row["reason"],
        "reason_label": REASON_LABELS.get(row["reason"], row["reason"]),
        "price_source": row["price_source"],
        "price_source_label": PRICE_SOURCE_LABELS.get(row["price_source"], row["price_source"]),
        "dry_run": True,
    }


def _mark_price(series_row: dict[str, Any] | None) -> float | None:
    if not series_row:
        return None
    quote = series_row.get("quote") or {}
    for key in ("mid", "last"):
        val = quote.get(key)
        if val:
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if num > 0:
                return num
    bars = series_row.get("bars") or []
    if bars:
        try:
            return float(bars[-1]["close"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def snapshot() -> dict[str, Any]:
    """JSON snapshot for the page, SSE, and the trades table. No secrets."""
    cfg = db.get_t_config()
    settings = get_settings()
    with _view_lock:
        series = {k: dict(v) for k, v in (_view.get("series") or {}).items()}
        # bars lists are replaced wholesale; shallow copy of the row is enough
        # as long as we don't mutate the lists. Copy bar lists to be safe.
        series = {
            k: {**v, "bars": list(v.get("bars") or [])}
            for k, v in series.items()
        }
        errors = list(_view.get("errors") or [])
        fetched_at = _view.get("fetched_at")
        market_phase = _view.get("market_phase") or "closed"
        now_et = _view.get("now_et")
        history = {
            sym: sorted((days or {}).keys(), reverse=True)
            for sym, days in (_view.get("history") or {}).items()
        }
        sources = dict(_view.get("sources") or {})
    if not series:
        for sym in cfg["symbols"]:
            series[sym] = {
                "symbol": sym,
                "session_date": None,
                "is_today": False,
                "source": None,
                "bars": [],
                "quote": None,
                "error": None,
            }

    dates: set[str] = set()
    today = datetime.now(ET).date().isoformat()
    dates.add(today)
    for row in series.values():
        if row.get("session_date"):
            dates.add(row["session_date"])
    fills: list[dict[str, Any]] = []
    seen: set[int] = set()
    for day in sorted(dates):
        for row in db.list_t_fills(day):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            fills.append(_fill_public(row))
    fills.sort(key=lambda r: (r.get("time") or 0, r["id"]))

    positions: list[dict[str, Any]] = []
    for sym in cfg["symbols"]:
        day = (series.get(sym) or {}).get("session_date") or today
        state = db.get_t_state(sym, day)
        pos = _pos_from_state(state)
        if pos is None:
            continue
        mark = _mark_price(series.get(sym))
        unreal = None if mark is None else (mark - pos.entry_px) * pos.qty
        positions.append(
            {
                "symbol": sym,
                "qty": pos.qty,
                "entry_px": pos.entry_px,
                "entry_et": pos.entry_ts.astimezone(ET).strftime("%H:%M ET"),
                "unrealized": unreal,
            }
        )

    params = {}
    for sym in cfg["symbols"]:
        p = params_for(sym, float(cfg["notional_usd"]))
        params[sym] = {
            "entry_bps": p.entry_bps,
            "exit_bps": p.exit_bps,
            "stop_bps": p.stop_bps,
            "param_source": p.param_source,
            "entry_bps_label": f"{p.entry_bps:.1f}",
            "stop_bps_label": f"{p.stop_bps:.1f}",
        }

    return {
        "engine_running": bool(cfg["running"]),
        "global_dry_run": bool(settings.dry_run),
        "t_live_orders": False,
        "t_mode": "dry_run",
        "symbols": list(cfg["symbols"]),
        "symbols_raw": cfg["symbols_raw"],
        "notional_usd": float(cfg["notional_usd"]),
        "params": params,
        "series": series,
        "trades": fills,
        "positions": positions,
        "errors": errors,
        "fetched_at": fetched_at,
        "market_phase": market_phase,
        "now_et": now_et,
        "flat_at": FORCE_FLAT_AT.strftime("%H:%M"),
        "sessions": history,
        "sources": sources,
        "help": (
            "5 分钟 VWAP 均值回归做T：价格低于当日 VWAP 达到该标的入场阈值时模拟买入，"
            "回到 VWAP 附近卖出，止损约为入场距离的 2 倍。只做多，每标的最多 1 笔未平仓，"
            "美东 15:50 起平仓并不再开新仓。名义约 $2000 整股。"
            "v1 只模拟成交，不向 Schwab 下单。"
        ),
    }


def _bars_for_cached_day(symbol: str, day: str) -> list[dict[str, Any]]:
    """Build chart rows for one session from SQLite, without creating fills."""
    from app.services import t_bar_cache
    from app.services.vwap_mr import session_date_et

    day_bars = [
        bar
        for bar in t_bar_cache.load_intrabars(symbol)
        if session_date_et(bar.start) == day
    ]
    if not day_bars:
        return []
    notional = float(db.get_t_config()["notional_usd"])
    return _display_closed(symbol, day_bars, notional)


def _remember_cached_history(symbol: str) -> None:
    """Refresh in-memory replay bars for one symbol from t_bars."""
    from app.services import t_bar_cache
    from app.services.t_market import group_sessions, rth_bars

    sessions = group_sessions(rth_bars(t_bar_cache.load_intrabars(symbol)))
    notional = float(db.get_t_config()["notional_usd"])
    snap = MarketSnap(
        symbol=symbol,
        session_date=None,
        is_today=False,
        bars=[],
        source="本地缓存",
        quote=None,
        sessions=sessions,
    )
    rows = _history_rows(snap, notional, datetime.now(timezone.utc))
    with _view_lock:
        history = dict(_view.get("history") or {})
        history[symbol] = rows
        _view["history"] = history


def ensure_replay_range(
    symbol: str,
    preset: str,
    *,
    today: date | None = None,
    force: bool = False,
    yahoo_fetch: Any = None,
    schwab_fetch: Any = None,
) -> dict[str, Any]:
    """Fetch 5m bars for a 回看 preset into t_bars, then list sessions in that window.

    Does not simulate fills. Presets longer than Yahoo's 5-minute cap are marked
    partial and only the available tail is requested.
    """
    from app.services import t_bar_cache

    sym = (symbol or "").strip().upper()
    today = today or datetime.now(ET).date()
    note = ""
    error: str | None = None
    partial = False
    if preset == t_bar_cache.PRESET_DAY:
        if not t_bar_cache.session_dates(sym) and (force or t_bar_cache.range_fetch_due(sym, "1m")):
            error = _pull_replay_window(
                sym,
                t_bar_cache.replay_range_start(today, "1m"),
                today,
                yahoo_fetch=yahoo_fetch,
                schwab_fetch=schwab_fetch,
            )
            t_bar_cache.mark_range_fetch(sym, "1m")
        sessions = t_bar_cache.session_dates(sym)[:400]
        if not sessions:
            note = error or "缓存里还没有交易日，行情回补没有拿到 K 线。"
        _remember_cached_history(sym)
        return _range_result(
            sym, preset, sessions, partial=False, note=note, error=error, continuous=False
        )

    start = t_bar_cache.replay_range_start(today, preset)
    request_start, partial = t_bar_cache.clamp_yahoo_5m_start(start, today)
    if partial:
        note = "5 分钟行情最多约 60 个自然日，更早的日期无法回补，没有编造 K 线。"
    if force or t_bar_cache.range_fetch_due(sym, preset):
        error = _pull_replay_window(
            sym,
            request_start,
            today,
            yahoo_fetch=yahoo_fetch,
            schwab_fetch=schwab_fetch,
        )
        t_bar_cache.mark_range_fetch(sym, preset)
    sessions = t_bar_cache.filter_session_dates(t_bar_cache.session_dates(sym), today, preset)
    if not sessions:
        note = note or error or "该范围内没有拿到 5 分钟 K 线。"
    elif error and not note:
        note = "部分行情回补失败，图上是缓存里已有的 K 线。"
    _remember_cached_history(sym)
    span_bars, span_trades = _span_chart(sym, sessions)
    return _range_result(
        sym,
        preset,
        sessions,
        partial=partial,
        note=note,
        error=error,
        continuous=True,
        bars=span_bars,
        trades=span_trades,
    )


def _pull_replay_window(
    symbol: str,
    start: date,
    end: date,
    *,
    yahoo_fetch: Any = None,
    schwab_fetch: Any = None,
) -> str | None:
    """Ingest Schwab (if it responds) then Yahoo for [start, end]. No fills."""
    from app.services import t_bar_cache

    yahoo = yahoo_fetch or t_market.fetch_yahoo_5m_span
    schwab = schwab_fetch if schwab_fetch is not None else t_market.fetch_schwab_5m
    if schwab is not False:
        try:
            raw, _quote = schwab(symbol)
            t_bar_cache.ingest(symbol, raw, t_bar_cache.SOURCE_SCHWAB)
        except Exception as exc:
            logger.info("回看 Schwab %s skipped: %s", symbol, exc)
    try:
        raw, _quote = yahoo(symbol, start, end)
        t_bar_cache.ingest(symbol, raw, t_bar_cache.SOURCE_YAHOO)
    except Exception as exc:
        logger.info("回看 Yahoo %s %s..%s failed: %s", symbol, start, end, exc)
        return str(exc)
    return None


def _span_chart(symbol: str, sessions: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Concatenate one session after another. VWAP still resets each day. No new fills."""
    bars: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    seen_times: set[int] = set()
    for day in sorted(sessions):
        for row in _bars_for_cached_day(symbol, day):
            stamp = row.get("time")
            if isinstance(stamp, int) and stamp in seen_times:
                continue
            if isinstance(stamp, int):
                seen_times.add(stamp)
            bars.append(row)
        trades.extend(
            _fill_public(row) for row in db.list_t_fills(day) if row["symbol"] == symbol
        )
    trades.sort(key=lambda row: (row.get("time") or 0, row.get("id") or 0))
    return bars, trades


def _range_result(
    symbol: str,
    preset: str,
    sessions: list[str],
    *,
    partial: bool,
    note: str,
    error: str | None,
    continuous: bool,
    bars: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "range": preset,
        "sessions": sessions,
        "partial": partial,
        "note": note,
        "error": error,
        "read_only": True,
        "continuous": continuous,
        "bars": bars or [],
        "trades": trades or [],
    }


def replay_payload(symbol: str, session_date: str) -> dict[str, Any]:
    """Read-only chart for one past session. Does not simulate new fills."""
    sym = (symbol or "").strip().upper()
    day = (session_date or "").strip()
    with _view_lock:
        stored = ((_view.get("history") or {}).get(sym) or {}).get(day)
        bars = list(stored or [])
        source = (_view.get("sources") or {}).get(sym)
    if not bars:
        bars = _bars_for_cached_day(sym, day)
        if bars:
            source = source or "本地缓存"
    trades: list[dict[str, Any]] = []
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        trades = [_fill_public(row) for row in db.list_t_fills(day) if row["symbol"] == sym]
    if not bars:
        return {
            "symbol": sym,
            "session_date": day,
            "is_today": False,
            "source": source,
            "bars": [],
            "quote": None,
            "trades": trades,
            "view": "replay",
            "read_only": True,
            "error": "该日没有已加载的 5 分钟行情",
        }
    return {
        "symbol": sym,
        "session_date": day,
        "is_today": False,
        "source": source,
        "bars": bars,
        "quote": None,
        "trades": trades,
        "view": "replay",
        "read_only": True,
        "error": None,
    }


def start_engine() -> None:
    """Arm the monitor and catch up today's closed bars immediately."""
    db.set_t_running(True)
    if not _fetch_lock.acquire(blocking=True, timeout=45):
        logger.warning("做T start: market refresh still busy")
        return
    try:
        _tick_body(datetime.now(timezone.utc))
    finally:
        _fetch_lock.release()


def stop_engine() -> None:
    db.set_t_running(False)


def update_settings(symbols: list[str], notional: float) -> None:
    db.set_t_settings(",".join(symbols), float(notional))


def reset_session(now: datetime | None = None) -> None:
    """Drop today's simulated fills and skip bars that already started.

    A later start continues from new 5m closes instead of replaying the session.
    """
    if not _fetch_lock.acquire(blocking=True, timeout=45):
        logger.warning("做T reset: market refresh still busy")
        return
    try:
        _reset_session(now)
    finally:
        _fetch_lock.release()


def _reset_session(now: datetime | None) -> None:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(ET).date().isoformat()
    cfg = db.get_t_config()
    db.delete_t_fills(today)
    for sym in cfg["symbols"]:
        db.upsert_t_state(
            symbol=sym,
            session_date=today,
            open_entry_ts=None,
            open_entry_px=None,
            open_qty=None,
            last_bar_ts=_iso(now),
            flattened=False,
        )


def reset_symbol_all(symbol: str, now: datetime | None = None) -> None:
    """Drop every simulated fill for one configured symbol, including older sessions.

    Today's cursor moves to `now`, so a running monitor does not immediately
    rewrite the bars that already started. Other symbols are left alone.
    """
    if not _fetch_lock.acquire(blocking=True, timeout=45):
        logger.warning("做T reset symbol: market refresh still busy")
        return
    try:
        _reset_symbol_all(symbol, now)
    finally:
        _fetch_lock.release()


def _reset_symbol_all(symbol: str, now: datetime | None) -> None:
    sym = (symbol or "").strip().upper()
    cfg = db.get_t_config()
    if sym not in cfg["symbols"]:
        raise ValueError(f"symbol not monitored: {sym}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.astimezone(ET).date().isoformat()
    db.delete_t_fills_for_symbol(sym)
    db.delete_t_state_for_symbol(sym)
    db.upsert_t_state(
        symbol=sym,
        session_date=today,
        open_entry_ts=None,
        open_entry_px=None,
        open_qty=None,
        last_bar_ts=_iso(now),
        flattened=False,
    )
