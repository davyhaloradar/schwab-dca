"""5-minute VWAP mean-reversion (做T) rules.

Matches the 2026-10-30 5m backtest (not the 1-minute variant):
long-only, one open T per symbol, flatten at 15:50 America/New_York.
Entry distance is the per-symbol value from that study
(about 0.25 x median daily range, clipped). Exit is VWAP / 5 bps
inside VWAP. Stop is about 2x the entry distance.
Same-bar stop and target: stop wins, except when the bar is the
15:50 flatten bar (the backtest then prefers the VWAP exit if both hit).

v1 of the monitor does not add the backtest's 1.5 bp slippage model.
A just-closed bar may fill at the live mid (else last). Older bars fill
at the trigger price.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FORCE_FLAT_AT = time(15, 50)
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# (entry_bps, exit_bps, stop_bps) from the 5m study summary.
# QQQM entry ~31.3 bps; AAPL ~48.6 bps; stop ~= 2x entry.
BACKTEST_VWAP_MR: dict[str, tuple[float, float, float]] = {
    "QQQM": (31.306446067215525, 5.0, 62.61289213443105),
    "QQQ": (31.453846426479902, 5.0, 62.907692852959805),
    "SPY": (19.414181173017642, 5.0, 38.828362346035284),
    "AAPL": (48.552878033309874, 5.0, 97.10575606661975),
    "NVDA": (60.0, 5.0, 100.0),
}

REASON_LABELS: dict[str, str] = {
    "entry_vwap": "跌破 VWAP 入场",
    "entry_gap": "低开跌破入场",
    "mr_vwap": "回归 VWAP",
    "mr_gap": "跳空回归 VWAP",
    "stop": "止损",
    "stop_gap": "跳空止损",
    "stop_samebar": "同K线止损优先",
    "eod": "15:50 平仓",
    "eod_safety": "收盘强平",
}

PRICE_SOURCE_LABELS: dict[str, str] = {
    "trigger": "触发价",
    "mid": "报价 mid",
    "last": "最新价",
}


@dataclass(frozen=True)
class SymbolParams:
    entry_bps: float
    exit_bps: float
    stop_bps: float
    notional: float
    param_source: str  # "backtest" | "qqqm_default"


@dataclass(frozen=True)
class Intrabar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            object.__setattr__(self, "start", self.start.replace(tzinfo=timezone.utc))


@dataclass
class OpenT:
    entry_ts: datetime
    entry_px: float
    qty: int


@dataclass
class SignalFill:
    bar_start: datetime
    side: str
    qty: int
    price: float
    reason: str
    pnl: float | None
    price_source: str = "trigger"


def params_for(symbol: str, notional: float) -> SymbolParams:
    key = (symbol or "").strip().upper()
    if key in BACKTEST_VWAP_MR:
        entry, exit_bps, stop = BACKTEST_VWAP_MR[key]
        return SymbolParams(entry, exit_bps, stop, float(notional), "backtest")
    entry, exit_bps, stop = BACKTEST_VWAP_MR["QQQM"]
    return SymbolParams(entry, exit_bps, stop, float(notional), "qqqm_default")


def whole_shares(price: float, notional: float) -> int:
    """Floor shares for the notional. Price above notional → 0 (no forced 1-share)."""
    if price <= 0 or notional <= 0:
        return 0
    return int(notional // price)


def is_rth_bar(start: datetime) -> bool:
    et = start.astimezone(ET)
    if et.weekday() >= 5:
        return False
    clock = et.timetz().replace(tzinfo=None)
    return RTH_OPEN <= clock < RTH_CLOSE


def session_date_et(start: datetime) -> str:
    return start.astimezone(ET).date().isoformat()


def chart_time(start: datetime) -> int:
    """Unix seconds of the ET wall clock, so the chart axis reads America/New_York."""
    et = start.astimezone(ET).replace(tzinfo=None)
    return int(et.replace(tzinfo=timezone.utc).timestamp())


def level_prices(vwap: float, params: SymbolParams) -> dict[str, float]:
    """Chart levels that move with session VWAP.

    entry = VWAP * (1 - entry_bps/10000)
    exit  = VWAP * (1 - exit_bps/10000)  (about 5 bps under VWAP)
    stop  = VWAP * (1 - stop_bps/10000)
    """
    def _px(bps: float) -> float:
        return float(vwap) * (1.0 - float(bps) / 10000.0)

    return {
        "entry": _px(params.entry_bps),
        "exit": _px(params.exit_bps),
        "stop": _px(params.stop_bps),
    }


def with_session_vwap(bars: list[Intrabar]) -> list[tuple[Intrabar, float]]:
    """Session VWAP: cumulative (typical price * volume) / cumulative volume.

    Typical price is (high + low + close) / 3. Zero-volume bars do not move VWAP.
    """
    cum_pv = 0.0
    cum_v = 0.0
    out: list[tuple[Intrabar, float]] = []
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3.0
        vol = bar.volume if bar.volume and bar.volume > 0 else 0.0
        if vol > 0:
            cum_pv += typical * vol
            cum_v += vol
        vwap = (cum_pv / cum_v) if cum_v > 0 else bar.close
        out.append((bar, vwap))
    return out


def _exec(theoretical: float, exec_px: float | None, exec_source: str) -> tuple[float, str]:
    if exec_px is not None and exec_px > 0:
        return float(exec_px), exec_source or "last"
    return float(theoretical), "trigger"


def on_closed_bar(
    bar: Intrabar,
    vwap: float,
    pos: OpenT | None,
    params: SymbolParams,
    *,
    day_flattened: bool,
    exec_px: float | None = None,
    exec_source: str = "trigger",
) -> tuple[OpenT | None, SignalFill | None, bool]:
    """One closed 5m bar. Returns (position, fill or None, day_flattened)."""
    clock = bar.start.astimezone(ET).timetz().replace(tzinfo=None)
    if pos is None and (day_flattened or clock >= FORCE_FLAT_AT):
        return None, None, True

    if pos is None:
        entry_lvl = vwap * (1.0 - params.entry_bps / 10000.0)
        if bar.low > entry_lvl:
            return None, None, False
        if bar.open < entry_lvl:
            theoretical = bar.open
            reason = "entry_gap"
        else:
            theoretical = entry_lvl
            reason = "entry_vwap"
        price, src = _exec(theoretical, exec_px, exec_source)
        qty = whole_shares(price, params.notional)
        if qty < 1 or price <= 0:
            return None, None, False
        fill = SignalFill(bar.start, "BUY", qty, price, reason, None, src)
        return OpenT(bar.start, price, qty), fill, False

    exit_lvl = vwap * (1.0 - params.exit_bps / 10000.0)
    stop_lvl = pos.entry_px * (1.0 - params.stop_bps / 10000.0)
    hit_vwap = bar.high >= vwap
    hit_exit = bar.high >= exit_lvl
    hit_stop = bar.low <= stop_lvl
    flatten = clock >= FORCE_FLAT_AT
    if not (hit_stop or hit_vwap or hit_exit or flatten):
        return pos, None, False

    if hit_stop and (hit_vwap or hit_exit) and not flatten:
        theoretical = bar.open if bar.open <= stop_lvl else stop_lvl
        reason = "stop_samebar"
    elif hit_stop and not (hit_vwap or flatten):
        if bar.open <= stop_lvl:
            theoretical = bar.open
            reason = "stop_gap"
        else:
            theoretical = stop_lvl
            reason = "stop"
    elif hit_vwap or hit_exit:
        tgt = vwap if hit_vwap else exit_lvl
        if bar.open >= tgt:
            theoretical = bar.open
            reason = "mr_gap"
        else:
            theoretical = tgt
            reason = "mr_vwap"
    else:
        theoretical = bar.close
        reason = "eod"

    price, src = _exec(theoretical, exec_px, exec_source)
    pnl = (price - pos.entry_px) * pos.qty
    fill = SignalFill(bar.start, "SELL", pos.qty, price, reason, pnl, src)
    return None, fill, bool(flatten or day_flattened)
