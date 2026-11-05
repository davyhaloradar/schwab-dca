"""SQLite cache of 5-minute regular-hours bars for 做T replay.

Fresh polls upsert by symbol + bar start. Schwab overwrites Yahoo for the
same bar; Yahoo never overwrites a Schwab bar. Older days stay in the table
after they age out of Schwab's short price-history window.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from app import db
from app.services.t_market import Intrabar, rth_bars
from app.services.vwap_mr import session_date_et

SOURCE_SCHWAB = "schwab"
SOURCE_YAHOO = "yahoo"
_BACKFILL_INTERVAL_SEC = 6 * 3600
_RANGE_FETCH_SEC = 120
_backfill_at: dict[str, float] = {}
_range_fetch_at: dict[tuple[str, str], float] = {}
# Yahoo's 5-minute chart stops returning bars past about 60 calendar days.
YAHOO_5M_MAX_DAYS = 59
PRESET_DAY = "day"


def bar_key(start: datetime) -> str:
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest(symbol: str, bars: list[Intrabar], source: str) -> int:
    """Upsert regular-hours bars. Returns how many bars were written."""
    sym = symbol.strip().upper()
    src = SOURCE_SCHWAB if source == SOURCE_SCHWAB else SOURCE_YAHOO
    rth = rth_bars(bars)
    if not rth:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as conn:
        protected: set[str] = set()
        if src != SOURCE_SCHWAB:
            rows = conn.execute(
                "SELECT bar_ts FROM t_bars WHERE symbol = ? AND source = ?",
                (sym, SOURCE_SCHWAB),
            ).fetchall()
            protected = {str(r[0]) for r in rows}
        written = 0
        for bar in rth:
            key = bar_key(bar.start)
            if key in protected:
                continue
            conn.execute(
                """
                INSERT INTO t_bars (
                    symbol, bar_ts, session_date, open, high, low, close, volume, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, bar_ts) DO UPDATE SET
                    session_date = excluded.session_date,
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    sym,
                    key,
                    session_date_et(bar.start),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    src,
                    now,
                ),
            )
            written += 1
    return written


def load_intrabars(symbol: str) -> list[Intrabar]:
    sym = symbol.strip().upper()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT bar_ts, open, high, low, close, volume
            FROM t_bars
            WHERE symbol = ?
            ORDER BY bar_ts
            """,
            (sym,),
        ).fetchall()
    out: list[Intrabar] = []
    for row in rows:
        start = datetime.strptime(str(row[0]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        out.append(
            Intrabar(
                start,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        )
    return out


def session_dates(symbol: str) -> list[str]:
    """Newest session date first."""
    sym = symbol.strip().upper()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT session_date FROM t_bars
            WHERE symbol = ?
            ORDER BY session_date DESC
            """,
            (sym,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def backfill_due(symbol: str, *, now_mono: float | None = None) -> bool:
    """Yahoo 60d backfill is throttled so the live poll does not hit it every tick."""
    sym = symbol.strip().upper()
    clock = time.monotonic() if now_mono is None else now_mono
    last = _backfill_at.get(sym)
    if last is None:
        return True
    return (clock - last) >= _BACKFILL_INTERVAL_SEC


def mark_backfill(symbol: str, *, now_mono: float | None = None) -> None:
    sym = symbol.strip().upper()
    _backfill_at[sym] = time.monotonic() if now_mono is None else now_mono


def range_fetch_due(symbol: str, preset: str, *, now_mono: float | None = None) -> bool:
    key = (symbol.strip().upper(), preset)
    last = _range_fetch_at.get(key)
    if last is None:
        return True
    clock = time.monotonic() if now_mono is None else now_mono
    return (clock - last) >= _RANGE_FETCH_SEC


def mark_range_fetch(symbol: str, preset: str, *, now_mono: float | None = None) -> None:
    key = (symbol.strip().upper(), preset)
    _range_fetch_at[key] = time.monotonic() if now_mono is None else now_mono


def clamp_yahoo_5m_start(start: date, end: date) -> tuple[date, bool]:
    """Shrink a request so it stays inside Yahoo's 5-minute history cap.

    Returns (request_start, truncated). truncated means the preset asked for
    days the 5-minute feed cannot provide.
    """
    if end < start:
        start, end = end, start
    if (end - start).days > YAHOO_5M_MAX_DAYS:
        return end - timedelta(days=YAHOO_5M_MAX_DAYS), True
    return start, False


# Rolling windows for the 回看 day list. Inclusive of today and the start date.
REPLAY_RANGE_PRESETS: tuple[tuple[str, str], ...] = (
    ("1w", "近1周"),
    ("1m", "近1月"),
    ("3m", "近3月"),
    ("6m", "近6月"),
    ("1y", "近1年"),
)


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def replay_range_start(today: date, preset: str) -> date:
    """First calendar day included in a 回看 preset (ET date)."""
    if preset == "1w":
        return today - timedelta(days=7)
    months = {"1m": -1, "3m": -3, "6m": -6, "1y": -12}.get(preset)
    if months is None:
        raise ValueError(f"unknown replay range: {preset}")
    return _add_months(today, months)


def filter_session_dates(dates: list[str], today: date, preset: str) -> list[str]:
    """Keep cached session days inside the preset window. Order is preserved."""
    start = replay_range_start(today, preset).isoformat()
    end = today.isoformat()
    return [day for day in dates if start <= day <= end]
