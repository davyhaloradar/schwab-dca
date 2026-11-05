"""Today's (or latest session) 5-minute bars for the 做T monitor.

Schwab Market Data /pricehistory when OAuth is linked, otherwise Yahoo chart.
Both paths keep regular-hours bars only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.services import schwab_client
from app.services.vwap_mr import ET, Intrabar, is_rth_bar, session_date_et

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class QuoteSnap:
    last: float | None = None
    bid: float | None = None
    ask: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return None

    def fill_px(self) -> tuple[float | None, str]:
        mid = self.mid
        if mid and mid > 0:
            return mid, "mid"
        if self.last and self.last > 0:
            return self.last, "last"
        return None, ""


@dataclass
class MarketSnap:
    symbol: str
    session_date: str | None
    is_today: bool
    bars: list[Intrabar]
    source: str
    quote: QuoteSnap | None
    error: str | None = None
    sessions: dict[str, list[Intrabar]] = field(default_factory=dict)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return num


def _epoch_to_dt(raw: Any) -> datetime | None:
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return None
    if num > 10_000_000_000:  # milliseconds
        num = num / 1000.0
    if num <= 0:
        return None
    return datetime.fromtimestamp(num, tz=timezone.utc)


def _candle(start: datetime | None, o: Any, h: Any, low: Any, c: Any, vol: Any) -> Intrabar | None:
    if start is None:
        return None
    close = _as_float(c)
    if close is None:
        return None
    open_ = _as_float(o) or close
    high = _as_float(h) or max(open_, close)
    low_ = _as_float(low) or min(open_, close)
    high = max(high, open_, close, low_)
    low_ = min(low_, open_, close, high)
    try:
        volume = float(vol or 0)
    except (TypeError, ValueError):
        volume = 0.0
    if volume < 0:
        volume = 0.0
    return Intrabar(start, open_, high, low_, close, volume)


def rth_bars(bars: list[Intrabar]) -> list[Intrabar]:
    """Sort, drop duplicates, keep regular-hours bars."""
    cleaned: dict[int, Intrabar] = {}
    for bar in bars:
        if not is_rth_bar(bar.start):
            continue
        cleaned[int(bar.start.timestamp())] = bar
    return [cleaned[k] for k in sorted(cleaned)]


def group_sessions(bars: list[Intrabar]) -> dict[str, list[Intrabar]]:
    """Map each regular-hours session date (ET) to its 5-minute bars."""
    out: dict[str, list[Intrabar]] = {}
    for bar in bars:
        out.setdefault(session_date_et(bar.start), []).append(bar)
    for day in out:
        out[day].sort(key=lambda b: b.start)
    return out


def select_session(
    bars: list[Intrabar], now: datetime
) -> tuple[str | None, list[Intrabar], bool]:
    """Prefer today's ET session. Otherwise the latest session in the payload."""
    by_day = group_sessions(bars)
    if not by_day:
        return None, [], False
    today = now.astimezone(ET).date().isoformat()
    if by_day.get(today):
        return today, by_day[today], True
    latest = sorted(by_day)[-1]
    return latest, by_day[latest], False


def _quote_from_schwab(payload: dict[str, Any], symbol: str) -> QuoteSnap | None:
    sym = symbol.upper()
    entry = None
    if isinstance(payload, dict):
        entry = payload.get(sym) or payload.get(symbol)
        if entry is None:
            for key, val in payload.items():
                if str(key).upper() == sym and isinstance(val, dict):
                    entry = val
                    break
    if not isinstance(entry, dict):
        return None
    quote = entry.get("quote") if isinstance(entry.get("quote"), dict) else entry
    if not isinstance(quote, dict):
        return None
    snap = QuoteSnap(
        last=_as_float(quote.get("lastPrice")) or _as_float(quote.get("mark")),
        bid=_as_float(quote.get("bidPrice")),
        ask=_as_float(quote.get("askPrice")),
    )
    if snap.last or snap.mid:
        return snap
    return None


def fetch_schwab_5m(symbol: str) -> tuple[list[Intrabar], QuoteSnap | None]:
    status = schwab_client.auth_status()
    if not status.get("linked") or not status.get("refresh_valid"):
        raise RuntimeError("Schwab 未连接")
    settings = get_settings()
    url = f"{settings.schwab_marketdata_base}/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": "day",
        "period": 10,
        "frequencyType": "minute",
        "frequency": 5,
        "needExtendedHoursData": "false",
    }
    with httpx.Client(timeout=12.0) as client:
        resp = client.get(url, headers=schwab_client._auth_headers(), params=params)
        resp.raise_for_status()
        payload = resp.json()
    candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        raise RuntimeError("Schwab pricehistory 返回异常")
    bars: list[Intrabar] = []
    for raw in candles:
        if not isinstance(raw, dict):
            continue
        bar = _candle(
            _epoch_to_dt(raw.get("datetime")),
            raw.get("open"),
            raw.get("high"),
            raw.get("low"),
            raw.get("close"),
            raw.get("volume"),
        )
        if bar:
            bars.append(bar)
    quote = None
    try:
        qpayload = schwab_client.get_quotes([symbol.upper()])
        quote = _quote_from_schwab(qpayload, symbol)
    except Exception as exc:
        logger.info("Schwab quote failed for %s: %s", symbol, exc)
    return bars, quote


def _yahoo_chart(symbol: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    query = {"interval": "5m", "includePrePost": "false"}
    query.update(params)
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
        resp = client.get(url, params=query)
        resp.raise_for_status()
        payload = resp.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        err = (payload.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo 5m 无数据 ({params}): {err}")
    block = result[0]
    if not isinstance(block, dict):
        raise RuntimeError(f"Yahoo 5m 返回异常 ({params})")
    return block


def _bars_from_yahoo_block(block: dict[str, Any]) -> tuple[list[Intrabar], QuoteSnap | None]:
    timestamps = block.get("timestamp") or []
    quote_block = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_block.get("open") or []
    highs = quote_block.get("high") or []
    lows = quote_block.get("low") or []
    closes = quote_block.get("close") or []
    volumes = quote_block.get("volume") or []
    bars: list[Intrabar] = []
    for i, ts in enumerate(timestamps):
        bar = _candle(
            _epoch_to_dt(ts),
            opens[i] if i < len(opens) else None,
            highs[i] if i < len(highs) else None,
            lows[i] if i < len(lows) else None,
            closes[i] if i < len(closes) else None,
            volumes[i] if i < len(volumes) else 0,
        )
        if bar:
            bars.append(bar)
    meta = block.get("meta") if isinstance(block.get("meta"), dict) else {}
    quote = QuoteSnap(
        last=_as_float(meta.get("regularMarketPrice")),
        bid=_as_float(meta.get("bid")),
        ask=_as_float(meta.get("ask")),
    )
    if not (quote.last or quote.mid):
        quote = None
    return bars, quote


def fetch_yahoo_5m(
    symbol: str, *, ranges: tuple[str, ...] = ("1mo", "5d")
) -> tuple[list[Intrabar], QuoteSnap | None]:
    sym = symbol.upper()
    block: dict[str, Any] | None = None
    last_err: Exception | None = None
    for range_ in ranges:
        try:
            block = _yahoo_chart(sym, {"range": range_})
            break
        except Exception as exc:
            last_err = exc
            logger.info("Yahoo 5m %s range=%s failed: %s", sym, range_, exc)
    if block is None:
        raise RuntimeError(str(last_err) if last_err else "Yahoo 5m 无数据")
    return _bars_from_yahoo_block(block)


def fetch_yahoo_5m_span(
    symbol: str, start: datetime, end: datetime
) -> tuple[list[Intrabar], QuoteSnap | None]:
    """5-minute bars for an inclusive ET date span. `start`/`end` are datetimes or dates."""
    from datetime import date as date_cls
    from datetime import time as time_cls

    sym = symbol.upper()
    if isinstance(start, datetime):
        start_day = start.astimezone(ET).date()
    else:
        start_day = start if isinstance(start, date_cls) else start
    if isinstance(end, datetime):
        end_day = end.astimezone(ET).date()
    else:
        end_day = end if isinstance(end, date_cls) else end
    start_dt = datetime.combine(start_day, time_cls(0, 0), tzinfo=ET)
    end_dt = datetime.combine(end_day + timedelta(days=1), time_cls(0, 0), tzinfo=ET)
    params = {
        "period1": str(int(start_dt.timestamp())),
        "period2": str(int(end_dt.timestamp())),
    }
    block = _yahoo_chart(sym, params)
    return _bars_from_yahoo_block(block)


def load_symbol(symbol: str, now: datetime | None = None) -> MarketSnap:
    """Schwab 5m when linked, Yahoo otherwise, merged with the local bar cache.

    Each successful fetch is upserted. A throttled Yahoo 60-day pull backfills
    days that Schwab's ~10-session window does not cover. Replay dates are the
    union of cache and this fetch. Schwab wins when both sources have a bar.
    """
    from app.services import t_bar_cache

    sym = symbol.strip().upper()
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    quote: QuoteSnap | None = None
    label: str | None = None

    try:
        raw, quote = fetch_schwab_5m(sym)
        if t_bar_cache.ingest(sym, raw, t_bar_cache.SOURCE_SCHWAB):
            label = "Schwab Market Data /pricehistory 5m + 本地缓存"
    except Exception as exc:
        errors.append(f"Schwab Market Data /pricehistory 5m: {exc}")
        logger.info("5m fetch %s Schwab failed: %s", sym, exc)

    if label is None:
        try:
            raw, yquote = fetch_yahoo_5m(sym, ranges=("5d", "1d"))
            quote = quote or yquote
            if t_bar_cache.ingest(sym, raw, t_bar_cache.SOURCE_YAHOO):
                label = "Yahoo Finance 5m（备用） + 本地缓存"
        except Exception as exc:
            errors.append(f"Yahoo Finance 5m（备用）: {exc}")
            logger.info("5m fetch %s Yahoo live failed: %s", sym, exc)

    if t_bar_cache.backfill_due(sym):
        try:
            raw, yquote = fetch_yahoo_5m(sym, ranges=("60d", "1mo"))
            t_bar_cache.ingest(sym, raw, t_bar_cache.SOURCE_YAHOO)
            t_bar_cache.mark_backfill(sym)
            quote = quote or yquote
            if label is None:
                label = "Yahoo Finance 5m + 本地缓存"
        except Exception as exc:
            errors.append(f"Yahoo 回补: {exc}")
            logger.info("5m Yahoo backfill %s failed: %s", sym, exc)

    rth = rth_bars(t_bar_cache.load_intrabars(sym))
    sessions = group_sessions(rth)
    session, chosen, is_today = select_session(rth, now)
    if not chosen:
        detail = "; ".join(errors) if errors else "无常规交易时段 K 线"
        raise RuntimeError(detail)
    if label is None:
        label = "本地缓存"
    return MarketSnap(
        symbol=sym,
        session_date=session,
        is_today=is_today,
        bars=chosen,
        source=label,
        quote=quote,
        sessions=sessions,
    )


def merge_quote_bar(bar: Intrabar, quote: QuoteSnap | None, now: datetime) -> Intrabar:
    """Patch the forming bar's close with the live quote so the chart moves between polls of history."""
    if quote is None or not quote.last:
        return bar
    end = bar.start + timedelta(minutes=5)
    if not (bar.start <= now < end):
        return bar
    last = quote.last
    return Intrabar(
        bar.start,
        bar.open,
        max(bar.high, last),
        min(bar.low, last),
        last,
        bar.volume,
    )


def market_phase(now: datetime) -> str:
    """Clock phase in America/New_York: pre / rth / post / closed (weekends)."""
    et = now.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return "closed"
    clock = et.time()
    if clock < datetime.strptime("09:30", "%H:%M").time():
        return "pre"
    if clock >= datetime.strptime("16:00", "%H:%M").time():
        return "post"
    return "rth"
