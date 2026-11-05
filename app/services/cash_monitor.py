"""Margin-safe cash monitoring: remaining DCA runs + low-cash warnings.

Uses only cash fields from schwab_client (never buyingPower / margin BP).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app import db
from app.config import get_settings
from app.services import notify, schwab_client

logger = logging.getLogger(__name__)

# Debounce low-cash notify: plan_id -> monotonic timestamp of last warn
_last_low_cash_notify: dict[int, float] = {}
_last_global_notify: float = 0.0


def plan_notional_estimate(
    plan: dict[str, Any],
    prices: dict[str, float | None] | None = None,
) -> float:
    """Estimate one-run notional for remaining-runs math.

    Share mode: shares_per_symbol * sum(known prices); missing price → fall back
    to amount_usd if set, else 0.
    Dollar mode: amount_usd (allocated total).
    """
    symbols = list(plan.get("symbols") or [])
    shares_each = int(plan.get("shares_per_symbol") or 0)
    amount = float(plan.get("amount_usd") or 0)
    if shares_each >= 1:
        if not symbols:
            return 0.0
        if not prices:
            # Without prices, approximate via amount_usd if configured as hint
            return amount if amount > 0 else 0.0
        total = 0.0
        missing = False
        for sym in symbols:
            p = prices.get(sym)
            if p is None or float(p) <= 0:
                missing = True
                continue
            total += shares_each * float(p)
        if total > 0:
            return round(total, 2)
        return amount if amount > 0 and missing else 0.0
    return round(amount, 2)


def remaining_runs(cash: float | None, notional: float) -> int | None:
    """Floor(cash / notional). None if cash unknown or notional <= 0."""
    if cash is None or notional <= 0:
        return None
    if cash < 0:
        return 0
    return int(cash // notional)


def cash_forecast_for_plans(
    plans: list[dict[str, Any]],
    *,
    prices: dict[str, float | None] | None = None,
    cash_by_hash: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build dashboard/API cash forecast.

    Returns:
      accounts: [{account_hash, cash, field_used, ...}]
      plans: [{plan_id, name, enabled, notional, cash, remaining_runs, low_cash, ...}]
      warn_threshold: int
      any_low_cash: bool
    """
    settings = get_settings()
    threshold = max(0, int(settings.dca_cash_warn_runs or 3))
    cash_by_hash = dict(cash_by_hash or {})

    # Collect unique account hashes from enabled (and all) plans + first linked
    hashes: list[str] = []
    seen: set[str] = set()
    for p in plans:
        h = (p.get("account_hash") or "").strip()
        if h and h not in seen:
            seen.add(h)
            hashes.append(h)
    if not hashes:
        first = schwab_client.first_account_hash()
        if first:
            hashes.append(first)

    accounts_out: list[dict[str, Any]] = []
    for h in hashes:
        if h not in cash_by_hash:
            try:
                cash_by_hash[h] = schwab_client.get_cash_details(h)
            except Exception as exc:
                logger.warning("cash_forecast: get_cash_details failed: %s", exc)
                cash_by_hash[h] = {"cash": None, "error": str(exc)}
        details = cash_by_hash[h]
        accounts_out.append(
            {
                "account_hash": h,
                "account_hash_short": h[:8] + "…",
                "cash": details.get("cash"),
                "field_used": details.get("field_used"),
                "balances_block": details.get("balances_block"),
                "account_type": details.get("account_type"),
                "cash_fields_seen": details.get("cash_fields_seen") or {},
                "excluded_margin_fields_seen": details.get("excluded_margin_fields_seen") or {},
                "error": details.get("error"),
            }
        )

    default_hash = hashes[0] if hashes else None
    plan_rows: list[dict[str, Any]] = []
    any_low = False
    for p in plans:
        h = (p.get("account_hash") or "").strip() or default_hash
        details = cash_by_hash.get(h or "", {}) if h else {}
        cash = details.get("cash") if details else None
        notional = plan_notional_estimate(p, prices=prices)
        runs = remaining_runs(cash, notional)
        enabled = bool(p.get("enabled"))
        low = bool(
            enabled
            and runs is not None
            and threshold >= 0
            and runs <= threshold
        )
        if low:
            any_low = True
        plan_rows.append(
            {
                "plan_id": p["id"],
                "name": p.get("name"),
                "enabled": enabled,
                "account_hash": h,
                "notional": notional,
                "cash": cash,
                "field_used": details.get("field_used") if details else None,
                "remaining_runs": runs,
                "low_cash": low,
                "shares_per_symbol": int(p.get("shares_per_symbol") or 0),
                "amount_usd": float(p.get("amount_usd") or 0),
                "symbols": list(p.get("symbols") or []),
            }
        )

    return {
        "accounts": accounts_out,
        "plans": plan_rows,
        "warn_threshold": threshold,
        "any_low_cash": any_low,
        "cash_field_policy": (
            "cashAvailableForTrading → cashBalance → availableFundsNonMarginableTrade; "
            "never buyingPower / marginBuyingPower / dayTradingBuyingPower / availableFunds"
        ),
    }


def maybe_notify_low_cash(forecast: dict[str, Any] | None = None) -> None:
    """Notify when any enabled plan has remaining_runs ≤ threshold (debounced)."""
    global _last_global_notify
    settings = get_settings()
    cooldown = max(60, int(settings.dca_cash_warn_cooldown_sec or 21600))
    threshold = max(0, int(settings.dca_cash_warn_runs or 3))

    if forecast is None:
        plans = [p for p in db.list_plans() if p.get("enabled")]
        if not plans:
            return
        # Best-effort prices for share-mode plans
        symbols: list[str] = []
        seen: set[str] = set()
        for p in plans:
            for s in p.get("symbols") or []:
                su = str(s).strip().upper()
                if su and su not in seen:
                    seen.add(su)
                    symbols.append(su)
        prices: dict[str, float | None] = {s: None for s in symbols}
        if symbols:
            try:
                quotes = schwab_client.get_quotes(symbols)
                for s in symbols:
                    prices[s] = schwab_client.extract_last_price(quotes, s)
            except Exception as exc:
                logger.warning("low_cash notify: quotes failed: %s", exc)
        forecast = cash_forecast_for_plans(plans, prices=prices)

    low_plans = [r for r in forecast.get("plans") or [] if r.get("low_cash")]
    if not low_plans:
        return

    now = time.monotonic()
    # Global debounce so a tick every 120s does not spam
    if _last_global_notify and (now - _last_global_notify) < cooldown:
        return

    # Per-plan debounce
    to_warn: list[dict[str, Any]] = []
    for row in low_plans:
        pid = int(row["plan_id"])
        last = _last_low_cash_notify.get(pid)
        if last is not None and (now - last) < cooldown:
            continue
        to_warn.append(row)

    if not to_warn:
        return

    parts = []
    for row in to_warn:
        runs = row.get("remaining_runs")
        cash = row.get("cash")
        notional = row.get("notional")
        parts.append(
            f"{row.get('name')}(#{row['plan_id']}): "
            f"~{runs} runs left (cash ${cash:.2f} / notional ${notional:.2f})"
            if cash is not None and notional is not None
            else f"{row.get('name')}(#{row['plan_id']}): low cash"
        )
        _last_low_cash_notify[int(row["plan_id"])] = now

    _last_global_notify = now
    msg = (
        f"Low cash warning (≤{threshold} runs remaining; margin BP ignored): "
        + "; ".join(parts)
    )
    notify.notify_event(
        "low_cash_warning",
        plan_id=to_warn[0]["plan_id"],
        plan_name=to_warn[0].get("name"),
        message=msg,
        extra={
            "threshold": threshold,
            "plans": [
                {
                    "plan_id": r["plan_id"],
                    "remaining_runs": r.get("remaining_runs"),
                    "cash": r.get("cash"),
                    "notional": r.get("notional"),
                    "field_used": r.get("field_used"),
                }
                for r in to_warn
            ],
        },
    )
