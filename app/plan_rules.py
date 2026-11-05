"""Association / boundary rules for DCA plans (symbols, schedule, account)."""
from __future__ import annotations

import re
from typing import Any

# Schwab equity-style tickers: start with letter; letters/digits/./- ; short.
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
ALLOWED_SCHEDULE = frozenset({"daily", "weekly", "monthly"})


def parse_symbols(raw: str | list[str] | None) -> list[str]:
    """Split / normalize symbols to unique uppercase tickers (order preserved)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(s) for s in raw]
    else:
        text = str(raw).replace(";", ",")
        parts = text.split(",")
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        s = p.strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def validate_tickers(symbols: list[str]) -> list[str]:
    """Return human-readable errors for invalid / empty ticker lists."""
    if not symbols:
        return ["至少需要一个标的代码 / at least one symbol required"]
    errs: list[str] = []
    for s in symbols:
        if not TICKER_RE.match(s):
            errs.append(
                f"非法标的代码 {s!r}（需大写字母开头，仅 A-Z / 0-9 / . / -，最长 10）"
            )
    return errs


def normalize_schedule_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Keep schedule_type + time/timezone; zero-out fields irrelevant to the type
    so stored rows stay consistent with what the scheduler actually uses.
    """
    out = dict(data)
    st = (out.get("schedule_type") or "daily").lower()
    if st == "cron":
        st = "daily"
    if st not in ALLOWED_SCHEDULE:
        st = "daily"
    out["schedule_type"] = st
    out["cron_expr"] = None
    # Clamp ranges
    try:
        wd = int(out.get("weekday", 0))
    except (TypeError, ValueError):
        wd = 0
    out["weekday"] = max(0, min(6, wd))
    try:
        dom = int(out.get("day_of_month", 1))
    except (TypeError, ValueError):
        dom = 1
    out["day_of_month"] = max(1, min(28, dom))
    # Ignore irrelevant fields (store safe defaults)
    if st == "daily":
        out["weekday"] = 0
        out["day_of_month"] = 1
    elif st == "weekly":
        out["day_of_month"] = 1
    elif st == "monthly":
        out["weekday"] = 0
    hhmm = (out.get("time_hhmm") or "10:00").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", hhmm):
        hhmm = "10:00"
    out["time_hhmm"] = hhmm
    return out


def symbols_csv(symbols: list[str]) -> str:
    return ",".join(symbols)
