"""Daily OHLCV price history for plan chart overlays.

Primary: Schwab Market Data GET /pricehistory (OAuth).
Fallback: Nasdaq public historical quote API (no API key).
Tertiary: Yahoo Finance chart API v8 (no API key; often blocked from datacenters).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.services import schwab_client

logger = logging.getLogger(__name__)

DEFAULT_PERIOD_TYPE = "year"
DEFAULT_PERIOD = 1
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _ms_to_date(ms: int | float) -> str:
    """Epoch ms → YYYY-MM-DD (UTC calendar day; daily bars align to session date)."""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).date().isoformat()


def _normalize_candles(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            if "date" in c and c.get("date"):
                d = str(c["date"])[:10]
            elif "datetime" in c:
                d = _ms_to_date(c["datetime"])
            else:
                continue
            o = float(c.get("open") or 0)
            h = float(c.get("high") or 0)
            low = float(c.get("low") or 0)
            cl = float(c.get("close") or 0)
            vol = float(c.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if cl <= 0:
            continue
        out.append(
            {
                "date": d,
                "open": o,
                "high": h,
                "low": low,
                "close": cl,
                "volume": vol,
            }
        )
    out.sort(key=lambda x: x["date"])
    return out


def _parse_money(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace("$", "").replace(",", "")
    if not s or s in ("N/A", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else None


def _parse_mdy(raw: str) -> str | None:
    """MM/DD/YYYY → YYYY-MM-DD"""
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def fetch_schwab_daily(symbol: str, *, period: int = DEFAULT_PERIOD) -> list[dict[str, Any]]:
    """Schwab daily candles: periodType=year, frequencyType=daily."""
    settings = get_settings()
    url = f"{settings.schwab_marketdata_base}/pricehistory"
    params = {
        "symbol": symbol.upper(),
        "periodType": DEFAULT_PERIOD_TYPE,
        "period": int(period),
        "frequencyType": "daily",
        "frequency": 1,
        "needExtendedHoursData": "false",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=schwab_client._auth_headers(), params=params)
        resp.raise_for_status()
        payload = resp.json()
    candles = payload.get("candles") if isinstance(payload, dict) else None
    if not isinstance(candles, list):
        raise RuntimeError(f"Schwab pricehistory: unexpected payload for {symbol}")
    return _normalize_candles(candles)


def fetch_nasdaq_daily(symbol: str, *, days: int = 400) -> list[dict[str, Any]]:
    """Nasdaq.com public historical endpoint — no API key."""
    sym = symbol.upper()
    end = date.today()
    start = end - timedelta(days=max(days, 30))
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    last_err: Exception | None = None
    for assetclass in ("etf", "stocks", "index"):
        url = f"https://api.nasdaq.com/api/quote/{sym}/historical"
        params = {
            "assetclass": assetclass,
            "fromdate": start.isoformat(),
            "todate": end.isoformat(),
            "limit": "400",
        }
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            last_err = exc
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        rows = ((data.get("tradesTable") or {}).get("rows")) or []
        if not rows:
            continue
        raw: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = _parse_mdy(str(row.get("date") or ""))
            if not d:
                continue
            cl = _parse_money(row.get("close"))
            if cl is None or cl <= 0:
                continue
            o = _parse_money(row.get("open")) or cl
            h = _parse_money(row.get("high")) or cl
            low = _parse_money(row.get("low")) or cl
            vol = _parse_money(row.get("volume")) or 0
            raw.append(
                {"date": d, "open": o, "high": h, "low": low, "close": cl, "volume": vol}
            )
        candles = _normalize_candles(raw)
        if candles:
            return candles
    if last_err:
        raise RuntimeError(f"Nasdaq historical failed for {sym}: {last_err}") from last_err
    raise RuntimeError(f"Nasdaq historical empty for {sym}")


def fetch_yahoo_daily(symbol: str, *, range_: str = "1y") -> list[dict[str, Any]]:
    """Yahoo Finance chart API (public, no key) — daily close history."""
    sym = symbol.upper()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    params = {"interval": "1d", "range": range_}
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        err = (payload.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo chart empty for {sym}: {err}")
    block = result[0]
    timestamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    raw: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        try:
            cl = closes[i]
            if cl is None:
                continue
            raw.append(
                {
                    "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
                    "open": opens[i] if i < len(opens) and opens[i] is not None else cl,
                    "high": highs[i] if i < len(highs) and highs[i] is not None else cl,
                    "low": lows[i] if i < len(lows) and lows[i] is not None else cl,
                    "close": float(cl),
                    "volume": volumes[i] if i < len(volumes) and volumes[i] is not None else 0,
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return _normalize_candles(raw)


def get_daily_history(symbol: str) -> tuple[list[dict[str, Any]], str]:
    """
    Return (candles, source_label).
    Tries Schwab OAuth marketdata → Nasdaq public API → Yahoo Finance.
    """
    sym = symbol.strip().upper()
    if not sym:
        return [], "none"
    try:
        candles = fetch_schwab_daily(sym)
        if candles:
            return candles, "Schwab Market Data /pricehistory"
    except Exception as exc:
        logger.info("Schwab pricehistory failed for %s: %s — trying Nasdaq", sym, exc)
    try:
        candles = fetch_nasdaq_daily(sym)
        if candles:
            return candles, "Nasdaq historical API (fallback)"
    except Exception as exc:
        logger.info("Nasdaq historical failed for %s: %s — trying Yahoo", sym, exc)
    try:
        candles = fetch_yahoo_daily(sym)
        if candles:
            return candles, "Yahoo Finance chart API (fallback)"
    except Exception as exc:
        logger.warning("All price history sources failed for %s: %s", sym, exc)
        raise
    return [], "none"
