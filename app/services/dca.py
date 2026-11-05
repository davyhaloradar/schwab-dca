"""DCA plan execution: quote → whole shares → dry-run log or live order.

Features wired here:
  - Feature 1: advance plans.last_run_at after successful execute (or explicit advance)
  - Feature 2: LIVE cash precheck (margin-safe cash only); skip all if notional > cash;
    notify on skip/fail; dry-run annotates shortfall
  - Feature 3: symbol allowlist + daily cumulative LIVE notional cap; preview builder
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.config import get_settings
from app.quant import NoopStrategy, StrategyContext
from app.services import notify, schwab_client
from app.services.due import next_due_after

logger = logging.getLogger(__name__)

PLACEHOLDER_PRICE = 100.0
PLACEHOLDER_NOTE = "PLACEHOLDER_PRICE — not a live quote; dry-run only sizing"

_locks_guard = threading.Lock()
_plan_locks: dict[int, threading.Lock] = {}
_last_live_success: dict[int, float] = {}


class PlanBusyError(RuntimeError):
    """Another run for this plan is already in progress."""


class IdempotencyError(RuntimeError):
    """Second successful LIVE submit within the idempotency window."""


class CapExceededError(RuntimeError):
    """LIVE order refused because shares/notional exceed hard caps."""


class PolicyRejectError(RuntimeError):
    """LIVE refused by symbol allowlist or daily notional policy."""


class InsufficientCashError(RuntimeError):
    """LIVE skipped: available cash < planned notional."""


def _plan_lock(plan_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _plan_locks.get(plan_id)
        if lock is None:
            lock = threading.Lock()
            _plan_locks[plan_id] = lock
        return lock


def try_acquire_plan(plan_id: int) -> threading.Lock | None:
    """Non-blocking exclusive lock for run / toggle / delete. None if busy."""
    lock = _plan_lock(plan_id)
    if lock.acquire(blocking=False):
        return lock
    return None


def _split_amount(amount_usd: float, n: int) -> float:
    if n <= 0:
        return 0.0
    return round(amount_usd / n, 2)


def _first_linked_account_hash() -> str | None:
    return schwab_client.first_account_hash()


def _resolve_account_hash(plan: dict[str, Any], *, dry_run: bool) -> str | None:
    plan_hash = (plan.get("account_hash") or "").strip() or None
    if not dry_run:
        return plan_hash
    return plan_hash or _first_linked_account_hash()


def _prices_for_symbols(symbols: list[str], dry_run: bool) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    quotes: dict[str, Any] = {}
    try:
        quotes = schwab_client.get_quotes(symbols)
    except Exception as exc:
        logger.warning("Quote fetch failed: %s", exc)
        if not dry_run:
            raise RuntimeError(f"Live quotes required when DRY_RUN=false: {exc}") from exc

    for sym in symbols:
        price = schwab_client.extract_last_price(quotes, sym) if quotes else None
        if price is None:
            if dry_run:
                out[sym] = {
                    "price": PLACEHOLDER_PRICE,
                    "placeholder": True,
                    "note": PLACEHOLDER_NOTE,
                }
            else:
                raise RuntimeError(f"No live quote for {sym}; refusing live order")
        else:
            out[sym] = {"price": price, "placeholder": False, "note": None}
    return out


def check_plan_caps(
    *,
    shares_per_symbol: int,
    amount_usd: float,
    n_symbols: int,
    prices: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return human-readable cap violation messages (empty if OK)."""
    settings = get_settings()
    max_shares = int(settings.dca_max_shares_per_symbol)
    max_usd = float(settings.dca_max_notional_usd)
    msgs: list[str] = []
    shares_each = int(shares_per_symbol or 0)
    if shares_each > max_shares:
        msgs.append(
            f"shares_per_symbol={shares_each} exceeds DCA_MAX_SHARES_PER_SYMBOL={max_shares}"
        )
    amt = float(amount_usd or 0)
    if amt > max_usd:
        msgs.append(f"amount_usd={amt} exceeds DCA_MAX_NOTIONAL_USD={max_usd}")

    if prices and n_symbols > 0:
        if shares_each >= 1:
            est = sum(shares_each * float(prices[s]["price"]) for s in prices)
        else:
            est = amt
        if est > max_usd:
            msgs.append(
                f"estimated notional ${est:.2f} exceeds DCA_MAX_NOTIONAL_USD={max_usd}"
            )
    return msgs


def check_symbol_allowlist(symbols: list[str]) -> list[str]:
    """Empty allowlist = allow any. Else every symbol must be in the set."""
    allowed = get_settings().symbol_allowlist_set()
    if not allowed:
        return []
    bad = [s for s in symbols if s.upper() not in allowed]
    if not bad:
        return []
    return [
        f"symbols not in DCA_SYMBOL_ALLOWLIST ({sorted(allowed)}): {', '.join(bad)}"
    ]


def estimate_notional(
    *,
    symbols: list[str],
    shares_each: int,
    amount_usd: float,
    prices: dict[str, dict[str, Any]],
) -> float:
    if shares_each >= 1:
        return round(sum(shares_each * float(prices[s]["price"]) for s in symbols if s in prices), 2)
    # Dollar mode: allocated total is amount_usd (may skip some symbols under 1 share)
    total = 0.0
    per = _split_amount(float(amount_usd), len(symbols)) if symbols else 0.0
    for sym in symbols:
        info = prices.get(sym)
        if not info:
            continue
        price = float(info["price"])
        qty = int(math.floor(per / price)) if price > 0 else 0
        if qty >= 1:
            total += qty * price
    return round(total, 2)


def check_daily_notional(additional: float) -> list[str]:
    settings = get_settings()
    cap = float(settings.dca_max_daily_notional_usd or 0)
    if cap <= 0:
        return []
    used = db.live_notional_submitted_today_et()
    if used + additional > cap:
        return [
            f"daily LIVE notional ${used:.2f} + this run ${additional:.2f} "
            f"exceeds DCA_MAX_DAILY_NOTIONAL_USD={cap}"
        ]
    return []


def _advance_last_run(plan: dict[str, Any], when: datetime | None = None) -> None:
    """Set last_run_at so the current/past schedule slot will not re-fire."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    iso = when.astimezone(timezone.utc).isoformat()
    nxt = next_due_after(
        schedule_type=plan.get("schedule_type") or "daily",
        time_hhmm=plan.get("time_hhmm") or "10:00",
        timezone=plan.get("timezone") or "America/New_York",
        weekday=int(plan.get("weekday") or 0),
        day_of_month=int(plan.get("day_of_month") or 1),
        after=when,
        now=when,
    )
    nxt_iso = nxt.astimezone(timezone.utc).isoformat() if nxt else None
    db.set_plan_last_run_at(int(plan["id"]), iso, next_due_at=nxt_iso)


def advance_past_slot(plan: dict[str, Any], slot: datetime) -> None:
    """Holiday skip+advance: consume calendar slot without placing orders."""
    slot_utc = slot.astimezone(timezone.utc) if slot.tzinfo else slot.replace(tzinfo=timezone.utc)
    _advance_last_run(plan, when=slot_utc)
    logger.info(
        "Plan %s: skip+advance past non-trading slot %s",
        plan.get("id"),
        slot_utc.isoformat(),
    )


def build_intended_orders(
    plan: dict[str, Any],
    *,
    dry_run: bool,
    prices: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Size orders for preview / execute. Does not place or log."""
    symbols = list(plan.get("symbols") or [])
    if not symbols:
        raise ValueError("Plan has no symbols")
    shares_each = int(plan.get("shares_per_symbol") or 0)
    per_symbol = _split_amount(float(plan["amount_usd"]), len(symbols))
    if prices is None:
        prices = _prices_for_symbols(symbols, dry_run=dry_run)

    allow_msgs = check_symbol_allowlist(symbols)
    cap_msgs = check_plan_caps(
        shares_per_symbol=shares_each,
        amount_usd=float(plan["amount_usd"]),
        n_symbols=len(symbols),
        prices=prices,
    )
    notional = estimate_notional(
        symbols=symbols,
        shares_each=shares_each,
        amount_usd=float(plan["amount_usd"]),
        prices=prices,
    )
    daily_msgs = [] if dry_run else check_daily_notional(notional)
    policy_msgs = allow_msgs + daily_msgs
    over_cap = bool(cap_msgs)
    over_policy = bool(policy_msgs)

    intended: list[dict[str, Any]] = []
    for sym in symbols:
        info = prices[sym]
        price = float(info["price"])
        if shares_each >= 1:
            qty = shares_each
            skip_reason = None
            allocated = round(qty * price, 2)
        else:
            qty = int(math.floor(per_symbol / price)) if price > 0 else 0
            skip_reason = (
                "amount too small for 1 whole share at this price" if qty < 1 else None
            )
            allocated = per_symbol
        intent: dict[str, Any] = {
            "symbol": sym,
            "amount_usd_allocated": allocated,
            "shares_mode": shares_each >= 1,
            "price_used": price,
            "price_placeholder": info["placeholder"],
            "price_note": info["note"],
            "quantity": qty,
            "skipped": qty < 1,
            "skip_reason": skip_reason,
            "over_cap": over_cap,
            "cap_notes": cap_msgs if over_cap else [],
            "policy_notes": policy_msgs if over_policy else [],
        }
        if qty >= 1:
            intent["order"] = schwab_client.build_equity_market_buy(sym, qty)
        intended.append(intent)

    account_hash = _resolve_account_hash(plan, dry_run=dry_run)
    return {
        "plan_id": plan["id"],
        "plan_name": plan["name"],
        "dry_run": dry_run,
        "account_hash": account_hash,
        "amount_usd_total": plan["amount_usd"],
        "per_symbol_usd": per_symbol,
        "shares_per_symbol": shares_each,
        "estimated_notional": notional,
        "over_cap": over_cap,
        "cap_notes": cap_msgs,
        "over_policy": over_policy,
        "policy_notes": policy_msgs,
        "intended_orders": intended,
        "prices": {s: prices[s]["price"] for s in symbols},
        "api_note": (
            "Schwab equity MARKET orders require whole-share quantity. "
            "If shares_per_symbol >= 1, that fixed qty is used; otherwise "
            "dollar amount is converted via floor(amount/price)."
        ),
    }


def preview_plan(plan_id: int) -> dict[str, Any]:
    """Build LIVE/dry preview without submitting. Used for manual LIVE confirm UI."""
    settings = get_settings()
    dry_run = bool(settings.dry_run)
    plan = db.get_plan(plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")
    built = build_intended_orders(plan, dry_run=dry_run)
    cash = None
    cash_details: dict[str, Any] = {}
    if built.get("account_hash"):
        cash_details = schwab_client.get_cash_details(built["account_hash"])
        cash = cash_details.get("cash")
    built["cash_available"] = cash
    built["cash_field_used"] = cash_details.get("field_used")
    built["cash_details"] = {
        "field_used": cash_details.get("field_used"),
        "balances_block": cash_details.get("balances_block"),
        "account_type": cash_details.get("account_type"),
        "cash_fields_seen": cash_details.get("cash_fields_seen") or {},
        "excluded_margin_fields_seen": cash_details.get("excluded_margin_fields_seen") or {},
        "error": cash_details.get("error"),
    }
    notional = float(built["estimated_notional"])
    if cash is not None:
        built["cash_ok"] = float(cash) >= notional
    elif dry_run:
        built["cash_ok"] = None  # unknown; dry-run still simulates
    else:
        built["cash_ok"] = False if settings.dca_require_cash_read else None
    return built


def execute_plan(
    plan_id: int,
    *,
    force_dry_run: bool | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    """
    Run a plan. trigger is 'manual' | 'scheduled' (for logging / last_run semantics).

    Manual and scheduled both advance last_run_at after a successful sizing pass
    that either submitted, dry-ran, or cleanly skipped (cash/policy) — so the same
    calendar slot is not immediately re-fired. Caps/idempotency still apply.
    """
    settings = get_settings()
    # Hard safety: never place live unless env says so
    if not settings.dry_run:
        dry_run = False if force_dry_run is None else bool(force_dry_run)
        if force_dry_run is None:
            dry_run = False
    else:
        dry_run = True

    # Re-derive: env DRY_RUN=true always forces dry
    if settings.dry_run:
        dry_run = True
    elif force_dry_run is True:
        dry_run = True
    else:
        dry_run = False

    lock = _plan_lock(plan_id)
    if not lock.acquire(blocking=False):
        raise PlanBusyError(
            f"Plan {plan_id} already has a run in progress (manual or scheduled)"
        )

    try:
        return _execute_plan_locked(
            plan_id, dry_run=dry_run, settings=settings, trigger=trigger
        )
    finally:
        lock.release()


def _execute_plan_locked(
    plan_id: int, *, dry_run: bool, settings: Any, trigger: str
) -> dict[str, Any]:
    window = int(settings.dca_live_idempotency_sec)
    if not dry_run and window > 0:
        last = _last_live_success.get(plan_id)
        if last is not None and (time.monotonic() - last) < window:
            raise IdempotencyError(
                f"Plan {plan_id}: LIVE submit already succeeded within "
                f"the last {window}s; refusing duplicate"
            )

    plan = db.get_plan(plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found")

    symbols = plan["symbols"]
    if not symbols:
        raise ValueError("Plan has no symbols")

    # Policy: allowlist (LIVE hard-fail; dry-run flags in payload)
    allow_msgs = check_symbol_allowlist(symbols)
    if allow_msgs and not dry_run:
        msg = "policy_reject: " + "; ".join(allow_msgs)
        log_id = db.add_run_log(
            plan_id=plan_id,
            plan_name=plan["name"],
            dry_run=False,
            payload={"status": "skipped_policy", "reason": msg, "trigger": trigger},
            result={"orders": [], "errors": [msg], "status": "skipped_policy", "trigger": trigger},
            error=msg,
        )
        notify.notify_event(
            "policy_reject",
            plan_id=plan_id,
            plan_name=plan["name"],
            message=msg,
            trigger=trigger,
        )
        # Advance so schedule does not hammer the same slot
        _advance_last_run(plan)
        raise PolicyRejectError(msg)

    prices = _prices_for_symbols(symbols, dry_run=dry_run)
    built = build_intended_orders(plan, dry_run=dry_run, prices=prices)
    cap_msgs = built["cap_notes"]
    over_cap = built["over_cap"]
    notional = float(built["estimated_notional"])
    daily_msgs = check_daily_notional(notional) if not dry_run else []
    if daily_msgs and not dry_run:
        msg = "policy_reject: " + "; ".join(daily_msgs)
        log_id = db.add_run_log(
            plan_id=plan_id,
            plan_name=plan["name"],
            dry_run=False,
            payload={**built, "status": "skipped_policy", "trigger": trigger},
            result={"orders": [], "errors": [msg], "status": "skipped_policy", "trigger": trigger},
            error=msg,
        )
        notify.notify_event(
            "policy_reject",
            plan_id=plan_id,
            plan_name=plan["name"],
            message=msg,
            extra={"notional": notional},
            trigger=trigger,
        )
        _advance_last_run(plan)
        raise PolicyRejectError(msg)

    if over_cap and not dry_run:
        raise CapExceededError(
            "LIVE refused — hard cap exceeded: " + "; ".join(cap_msgs)
        )

    strategy = NoopStrategy()
    ctx = StrategyContext(plan=plan, quotes=prices, dry_run=dry_run)
    _extra_intents = strategy.evaluate(ctx)  # noqa: F841

    account_hash = built.get("account_hash")
    if not dry_run and not account_hash:
        raise RuntimeError(
            "LIVE refused — plan.account_hash required "
            "(locked at create; no first-account fallback)"
        )

    # Feature 2: cash precheck — MARGIN-SAFE fields only (never buyingPower).
    # LIVE: skip ALL orders if cash < notional (or cash unreadable when required).
    # Dry-run: still simulate but annotate cash shortfall.
    cash_available: float | None = None
    cash_field_used: str | None = None
    cash_details: dict[str, Any] = {}
    if account_hash and (not dry_run or notional > 0):
        cash_details = schwab_client.get_cash_details(account_hash)
        cash_available = cash_details.get("cash")
        cash_field_used = cash_details.get("field_used")

    if not dry_run and notional > 0:
        skip_status = None
        msg = None
        if cash_available is None and bool(settings.dca_require_cash_read):
            skip_status = "skipped_cash_unknown"
            msg = (
                "skipped_cash_unknown: could not read margin-safe cash "
                f"(field policy: cashAvailableForTrading/cashBalance only; "
                f"error={cash_details.get('error')})"
            )
        elif cash_available is not None and cash_available < notional:
            skip_status = "skipped_insufficient_cash"
            msg = (
                f"skipped_insufficient_cash: cash ${cash_available:.2f} "
                f"(via {cash_field_used}) < planned notional ${notional:.2f} "
                f"— margin buying power NOT used"
            )
        if skip_status and msg:
            payload = {
                **built,
                "status": skip_status,
                "cash_available": cash_available,
                "cash_field_used": cash_field_used,
                "cash_details": {
                    "field_used": cash_field_used,
                    "balances_block": cash_details.get("balances_block"),
                    "cash_fields_seen": cash_details.get("cash_fields_seen") or {},
                    "excluded_margin_fields_seen": cash_details.get(
                        "excluded_margin_fields_seen"
                    )
                    or {},
                },
                "trigger": trigger,
            }
            result = {
                "orders": [
                    {
                        "symbol": i["symbol"],
                        "status": skip_status,
                        "reason": msg,
                    }
                    for i in built["intended_orders"]
                ],
                "errors": [msg],
                "status": skip_status,
                "trigger": trigger,
            }
            log_id = db.add_run_log(
                plan_id=plan_id,
                plan_name=plan["name"],
                dry_run=False,
                payload=payload,
                result=result,
                error=msg,
            )
            notify.notify_event(
                "insufficient_cash" if skip_status == "skipped_insufficient_cash" else "cash_unknown",
                plan_id=plan_id,
                plan_name=plan["name"],
                message=msg,
                extra={
                    "cash_available": cash_available,
                    "cash_field_used": cash_field_used,
                    "notional": notional,
                },
                trigger=trigger,
            )
            # Advance: do not multi-retry the same slot every tick
            _advance_last_run(plan)
            return {"log_id": log_id, "payload": payload, "result": result}

    intended_orders = built["intended_orders"]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    any_live_submitted = False

    for intent in intended_orders:
        sym = intent["symbol"]
        qty = int(intent["quantity"])
        order_body = intent.get("order")
        if qty < 1:
            results.append(
                {"symbol": sym, "status": "skipped", "reason": intent.get("skip_reason")}
            )
            continue

        if dry_run:
            note = "DRY_RUN — order NOT submitted to Schwab"
            if over_cap:
                note += " | OVER_CAP (would be refused in LIVE): " + "; ".join(cap_msgs)
            if allow_msgs:
                note += " | POLICY (would be refused in LIVE): " + "; ".join(allow_msgs)
            if cash_available is not None and cash_available < notional:
                note += (
                    f" | CASH_SHORTFALL: cash ${cash_available:.2f} "
                    f"(via {cash_field_used}) < notional ${notional:.2f} "
                    f"— LIVE would skip (margin BP not used)"
                )
            elif cash_available is None and account_hash:
                note += " | CASH_UNKNOWN: could not read margin-safe cash"
            results.append(
                {
                    "symbol": sym,
                    "status": "dry_run",
                    "order": order_body,
                    "note": note,
                    "over_cap": over_cap,
                }
            )
        else:
            try:
                placed = schwab_client.place_order(account_hash, order_body)
                any_live_submitted = True
                order_summary = None
                oid = (placed or {}).get("order_id")
                if oid:
                    # Brief settle then reconcile Schwab order status (fill vs cancel)
                    try:
                        time.sleep(0.8)
                        raw_order = schwab_client.get_order(account_hash, str(oid))
                        order_summary = schwab_client.summarize_order(raw_order)
                        order_summary["symbol"] = sym
                    except Exception as status_exc:
                        logger.warning(
                            "Post-place order status fetch failed %s: %s", oid, status_exc
                        )
                        order_summary = {
                            "order_id": str(oid),
                            "status": "SUBMITTED",
                            "symbol": sym,
                            "filled_quantity": 0,
                            "cancelable": True,
                            "note": "已提交，成交状态稍后刷新",
                        }
                row = {
                    "symbol": sym,
                    "status": "submitted",
                    "response": placed,
                    "order_id": oid,
                }
                if order_summary is not None:
                    row["order_status"] = order_summary
                results.append(row)
            except Exception as exc:
                errors.append(str(exc))
                results.append({"symbol": sym, "status": "error", "error": str(exc)})
                notify.notify_event(
                    "live_order_error",
                    plan_id=plan_id,
                    plan_name=plan["name"],
                    message=str(exc),
                    extra={"symbol": sym},
                    trigger=trigger,
                )

    if any_live_submitted:
        _last_live_success[plan_id] = time.monotonic()

    from app.services.order_status import derive_outcome_status

    outcome = derive_outcome_status(
        orders=results, errors=errors, dry_run=dry_run, skip_status=None
    )

    payload = {
        **built,
        "cash_available": cash_available,
        "cash_field_used": cash_field_used,
        "cash_details": {
            "field_used": cash_field_used,
            "balances_block": cash_details.get("balances_block"),
            "cash_fields_seen": cash_details.get("cash_fields_seen") or {},
            "excluded_margin_fields_seen": cash_details.get("excluded_margin_fields_seen")
            or {},
        },
        "trigger": trigger,
        "status": outcome,
    }
    # Drop ephemeral prices dict clutter if present
    payload.pop("prices", None)
    result = {"orders": results, "errors": errors, "status": outcome, "trigger": trigger}
    log_id = db.add_run_log(
        plan_id=plan_id,
        plan_name=plan["name"],
        dry_run=dry_run,
        payload=payload,
        result=result,
        error="; ".join(errors) if errors else None,
    )

    # Feature 1: advance last_run_at after successful execute (dry or live),
    # so the same calendar slot won't re-fire. Manual run also advances to now.
    _advance_last_run(plan)

    if errors and not dry_run:
        notify.notify_event(
            "live_run_partial_failure",
            plan_id=plan_id,
            plan_name=plan["name"],
            message="; ".join(errors),
            trigger=trigger,
        )

    # Always notify run summary (email/webhook gated by notify settings).
    symbols_summary = ",".join(
        f"{r.get('symbol')}={r.get('status')}" for r in results if r.get("symbol")
    )
    order_rows: list[dict] = []
    for r in results:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        qty = None
        order_body = r.get("order") if isinstance(r.get("order"), dict) else None
        if order_body:
            legs = order_body.get("orderLegCollection") or []
            if legs and isinstance(legs[0], dict) and legs[0].get("quantity") is not None:
                qty = legs[0].get("quantity")
            elif order_body.get("quantity") is not None:
                qty = order_body.get("quantity")
        os_ = r.get("order_status") if isinstance(r.get("order_status"), dict) else {}
        if qty is None and os_:
            qty = os_.get("filled_quantity") or os_.get("quantity")
        if qty is None:
            for intent in intended_orders:
                if intent.get("symbol") == r.get("symbol"):
                    qty = intent.get("quantity")
                    break
        order_rows.append(
            {
                "symbol": r.get("symbol"),
                "status": r.get("status") or (os_.get("status") if os_ else None),
                "quantity": qty,
                "filled_quantity": (os_ or {}).get("filled_quantity"),
                "avg_price": (os_ or {}).get("average_price")
                or (os_ or {}).get("price"),
                "reason": r.get("reason") or r.get("error") or r.get("note"),
            }
        )
    notify.notify_event(
        "run_complete",
        plan_id=plan_id,
        plan_name=plan["name"],
        message=(
            f"status={outcome} dry_run={dry_run} "
            f"orders={len(results)} errors={len(errors)}"
            + (f" symbols=[{symbols_summary}]" if symbols_summary else "")
        ),
        extra={
            "status": outcome,
            "dry_run": dry_run,
            "order_count": len(results),
            "error_count": len(errors),
            "symbols": [r.get("symbol") for r in results if r.get("symbol")],
            "orders": order_rows,
            "cash_available": cash_available,
            "cash_field_used": cash_field_used,
            "notional": notional,
            "errors": list(errors) if errors else [],
        },
        trigger=trigger,
    )

    return {"log_id": log_id, "payload": payload, "result": result}
