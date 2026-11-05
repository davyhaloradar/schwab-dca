"""Enrich run logs with live Schwab order status."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app import db
from app.services import schwab_client

logger = logging.getLogger(__name__)


def _parse_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None




def derive_outcome_status(
    *,
    orders: list[dict[str, Any]] | None,
    errors: list[str] | None = None,
    dry_run: bool = False,
    skip_status: str | None = None,
) -> str:
    """
    Business outcome code persisted on result_json.status / payload.status.

    Never returns bare "ok"/"success" for live submits — use filled /
    cancelled / submitted_unfilled / partial_fill / error / dry_run / skip_*.
    """
    if skip_status:
        return str(skip_status)
    if dry_run:
        return "dry_run"
    errs = list(errors or [])
    items = [o for o in (orders or []) if isinstance(o, dict)]
    if not items and errs:
        return "error"
    if not items:
        return "unknown"

    # Skip-only runs (cash/policy) already stamped per-order
    submit_statuses = [str(o.get("status") or "").lower() for o in items]
    if all(s.startswith("skipped") for s in submit_statuses):
        return submit_statuses[0] if submit_statuses else "skipped"

    cancel_set = {"CANCELED", "CANCELLED", "PENDING_CANCEL"}
    reject_set = {"REJECTED"}
    expire_set = {"EXPIRED"}
    working_set = {
        "WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "NEW", "SUBMITTED",
        "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION",
        "AWAITING_MANUAL_REVIEW", "AWAITING_UR_OUT", "PENDING_ACKNOWLEDGEMENT",
        "PENDING_RECALL", "AWAITING_RELEASE_TIME",
    }

    any_error = bool(errs) or any(s == "error" for s in submit_statuses)
    any_filled = False
    all_filled = True
    saw_live = False
    any_cancelled = False
    all_cancel_empty = True
    any_working = False
    any_rejected = False
    any_expired = False

    for o in items:
        submit = str(o.get("status") or "").lower()
        if submit in ("dry_run", "skipped") or submit.startswith("skipped"):
            all_filled = False
            all_cancel_empty = False
            continue
        if submit == "error":
            all_filled = False
            all_cancel_empty = False
            continue
        summary = o.get("order_status") if isinstance(o.get("order_status"), dict) else {}
        status_u = str(summary.get("status") or "").upper()
        if not status_u and submit == "submitted":
            status_u = "SUBMITTED"
        saw_live = True
        try:
            filled = float(summary.get("filled_quantity") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        try:
            qty = summary.get("quantity")
            qty_f = float(qty) if qty is not None and qty != "" else None
        except (TypeError, ValueError):
            qty_f = None
        if filled > 1e-9:
            any_filled = True
        fully = (qty_f is not None and filled + 1e-9 >= qty_f and qty_f > 0) or status_u == "FILLED"
        if not fully:
            all_filled = False
        if status_u in cancel_set:
            any_cancelled = True
            if filled > 1e-9:
                all_cancel_empty = False
        else:
            all_cancel_empty = False
        if status_u in working_set:
            any_working = True
        if status_u in reject_set:
            any_rejected = True
        if status_u in expire_set:
            any_expired = True

    if not saw_live:
        if any_error:
            return "error"
        return "unknown"

    if any_error and not any_filled:
        return "error"
    if any_error and any_filled:
        return "partial_error"
    if all_filled and any_filled:
        return "filled"
    if any_filled and (any_cancelled or any_working):
        return "partial_fill"
    if any_cancelled and not any_filled:
        return "cancelled"
    if any_rejected and not any_filled:
        return "rejected"
    if any_expired and not any_filled:
        return "expired"
    if any_working or not any_filled:
        return "submitted_unfilled"
    return "submitted_unfilled"


def extract_live_orders(log: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull account_hash + order_id (+ symbol) from a stored run log."""
    payload = _parse_json(log.get("payload_json")) or {}
    result = _parse_json(log.get("result_json")) or {}
    account_hash = payload.get("account_hash")
    if not account_hash:
        # maybe nested in result after refresh
        account_hash = result.get("account_hash")
    out: list[dict[str, Any]] = []
    for item in result.get("orders") or []:
        if not isinstance(item, dict):
            continue
        resp = item.get("response") or {}
        order_id = resp.get("order_id") or item.get("order_id")
        if not order_id:
            continue
        out.append(
            {
                "symbol": item.get("symbol"),
                "order_id": str(order_id),
                "account_hash": account_hash,
                "submit_status": item.get("status"),
                "cached": item.get("order_status"),
            }
        )
    return out



def _assert_log_account_matches_plan(log: dict[str, Any], account_hash: str) -> None:
    """If the plan still exists and has a locked hash, refuse mismatch vs log payload."""
    pid = log.get("plan_id")
    if pid is None:
        return
    plan = db.get_plan(int(pid))
    if not plan:
        return
    plan_hash = (plan.get("account_hash") or "").strip()
    if not plan_hash:
        return
    if (account_hash or "").strip() != plan_hash:
        raise RuntimeError(
            "account_hash 与计划不一致，拒绝操作 "
            "(log payload vs plan.account_hash)"
        )


def enrich_log(log: dict[str, Any], *, refresh: bool = True) -> dict[str, Any]:
    """Attach order_statuses list for the template. Optionally hit Schwab."""
    log = dict(log)
    live = extract_live_orders(log)
    statuses: list[dict[str, Any]] = []

    if log.get("dry_run"):
        result = _parse_json(log.get("result_json")) or {}
        for item in result.get("orders") or []:
            if isinstance(item, dict) and item.get("status") == "dry_run":
                statuses.append(
                    {
                        "symbol": item.get("symbol"),
                        "order_id": None,
                        "status": "DRY_RUN",
                        "cancelable": False,
                        "note": "未真实下单",
                    }
                )
        log["order_statuses"] = statuses
        return log

    if not live:
        log["order_statuses"] = []
        return log

    # Boundary: if plan still exists, refuse Schwab calls on mismatched account_hash
    if refresh:
        try:
            _assert_log_account_matches_plan(log, (live[0].get("account_hash") or ""))
        except RuntimeError as exc:
            log["order_statuses"] = [
                {
                    "symbol": e.get("symbol"),
                    "order_id": e.get("order_id"),
                    "status": "HASH_MISMATCH",
                    "cancelable": False,
                    "error": str(exc),
                }
                for e in live
            ]
            return log

    for entry in live:
        row: dict[str, Any] = {
            "symbol": entry.get("symbol"),
            "order_id": entry["order_id"],
            "account_hash": entry.get("account_hash"),
            "status": (entry.get("cached") or {}).get("status") if isinstance(entry.get("cached"), dict) else entry.get("submit_status") or "SUBMITTED",
            "cancelable": False,
        }
        if refresh and entry.get("account_hash"):
            try:
                raw = schwab_client.get_order(entry["account_hash"], entry["order_id"])
                summary = schwab_client.summarize_order(raw)
                row.update(summary)
                row["order_id"] = entry["order_id"]
                row["account_hash"] = entry["account_hash"]
                row["raw_ok"] = True
            except Exception as exc:
                logger.warning("Order status fetch failed %s: %s", entry["order_id"], exc)
                row["error"] = str(exc)
                row["cancelable"] = False
        statuses.append(row)

    # Persist latest summaries + business outcome (never leave false "ok"/成功)
    if refresh and statuses:
        result = _parse_json(log.get("result_json")) or {}
        by_id = {s["order_id"]: s for s in statuses if s.get("order_id")}
        for item in result.get("orders") or []:
            if not isinstance(item, dict):
                continue
            oid = (item.get("response") or {}).get("order_id") or item.get("order_id")
            if oid and str(oid) in by_id:
                # Do not persist account_hash into UI-facing summary blobs
                summary = dict(by_id[str(oid)])
                summary.pop("account_hash", None)
                item["order_status"] = summary
        outcome = derive_outcome_status(
            orders=result.get("orders") or [],
            errors=result.get("errors") or [],
            dry_run=False,
        )
        result["status"] = outcome
        payload = _parse_json(log.get("payload_json")) or {}
        # Keep trigger on result (independent of fill/cancel outcome)
        trig = payload.get("trigger") or result.get("trigger")
        if trig and not result.get("trigger"):
            result["trigger"] = trig
        if payload:
            payload["status"] = outcome
            if trig and not payload.get("trigger"):
                payload["trigger"] = trig
        try:
            with db.connect() as conn:
                if payload:
                    conn.execute(
                        "UPDATE run_logs SET result_json = ?, payload_json = ? WHERE id = ?",
                        (
                            json.dumps(result, ensure_ascii=False, default=str),
                            json.dumps(payload, ensure_ascii=False, default=str),
                            log["id"],
                        ),
                    )
                    log["payload_json"] = json.dumps(payload, ensure_ascii=False, default=str)
                else:
                    conn.execute(
                        "UPDATE run_logs SET result_json = ? WHERE id = ?",
                        (json.dumps(result, ensure_ascii=False, default=str), log["id"]),
                    )
            log["result_json"] = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            logger.exception("Failed to persist order status on log %s", log["id"])

    # Strip account_hash from template-facing rows
    for s in statuses:
        s.pop("account_hash", None)

    log["order_statuses"] = statuses
    return log


def cancel_log_order(log_id: int, order_id: str) -> dict[str, Any]:
    log = db.get_run_log(log_id)
    if not log:
        raise ValueError("Run log not found")
    if log.get("dry_run"):
        raise RuntimeError("Dry-run 记录没有真实订单可撤")
    live = extract_live_orders(log)
    match = next((x for x in live if x["order_id"] == str(order_id)), None)
    if not match or not match.get("account_hash"):
        raise ValueError("该日志中找不到此订单或缺少 account_hash")
    # Boundary: order must belong to this log; hash must match plan if plan exists
    _assert_log_account_matches_plan(log, match["account_hash"])
    # Verify still cancelable
    raw = schwab_client.get_order(match["account_hash"], order_id)
    summary = schwab_client.summarize_order(raw)
    if not summary.get("cancelable"):
        raise RuntimeError(f"订单状态为 {summary.get('status')}，不可撤单")
    cancelled = schwab_client.cancel_order(match["account_hash"], order_id)
    # Refresh once more
    try:
        raw2 = schwab_client.get_order(match["account_hash"], order_id)
        summary = schwab_client.summarize_order(raw2)
    except Exception:
        summary["status"] = "PENDING_CANCEL"
        summary["cancelable"] = False
    result = _parse_json(log.get("result_json")) or {}
    for item in result.get("orders") or []:
        if not isinstance(item, dict):
            continue
        oid = (item.get("response") or {}).get("order_id") or item.get("order_id")
        if str(oid) == str(order_id):
            item["order_status"] = summary
            item["cancel_response"] = cancelled
    outcome = derive_outcome_status(
        orders=result.get("orders") or [],
        errors=result.get("errors") or [],
        dry_run=False,
    )
    result["status"] = outcome
    payload = _parse_json(log.get("payload_json")) or {}
    trig = (payload.get("trigger") if payload else None) or result.get("trigger")
    if trig and not result.get("trigger"):
        result["trigger"] = trig
    db.update_run_log_result(log_id, result)
    # Keep payload status in sync when possible
    try:
        if payload:
            payload["status"] = outcome
            if trig and not payload.get("trigger"):
                payload["trigger"] = trig
            with db.connect() as conn:
                conn.execute(
                    "UPDATE run_logs SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False, default=str), log_id),
                )
    except Exception:
        logger.exception("Failed to sync payload status after cancel on log %s", log_id)
    return {"ok": True, "order": summary, "cancel": cancelled}


def _filled_legs_from_result(result_json: str | None) -> list[tuple[str, float, float]]:
    """Extract (symbol, average_price, filled_qty) for live filled legs from a run result."""
    result = _parse_json(result_json) or {}
    out: list[tuple[str, float, float]] = []
    for item in result.get("orders") or []:
        if not isinstance(item, dict):
            continue
        # Skip dry-run / skipped submit statuses
        submit = str(item.get("status") or "").lower()
        if submit in ("dry_run", "skipped"):
            continue
        summary = item.get("order_status")
        if not isinstance(summary, dict):
            continue
        status_u = str(summary.get("status") or "").upper()
        if status_u == "DRY_RUN":
            continue
        try:
            filled = float(summary.get("filled_quantity") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        if filled <= 1e-9:
            continue
        try:
            avg = float(summary.get("average_price") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if avg <= 0:
            continue
        symbol = (
            summary.get("symbol")
            or item.get("symbol")
            or (summary.get("symbols") or [None])[0]
            or "?"
        )
        symbol = str(symbol).strip().upper() or "?"
        out.append((symbol, avg, filled))
    return out


def dca_holdings_by_plan(plan_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
    """
    Aggregate live DCA fills into per-plan holdings.

    Uses the same live-fill filter as avg cost / chart markers
    (_filled_legs_from_result: skips dry-run / skipped / unfilled).

    Returns {plan_id: {
      "by_symbol": {sym: {"shares", "cost_basis", "avg_cost"}},
      "total_shares": float,       # sum of filled qty across symbols
      "total_cost_basis": float,   # sum(avg * qty) — 总定投成本
    }}. Plans with no live fills are absent.
    """
    logs = db.list_live_run_logs(limit=5000)
    # notional / qty accumulators: plan_id -> symbol -> [notional, qty]
    acc: dict[int, dict[str, list[float]]] = {}
    wanted = set(plan_ids) if plan_ids is not None else None
    # Cache plan symbol allow-lists (skip fills for symbols not on the plan)
    plan_syms: dict[int, set[str] | None] = {}
    for log in logs:
        pid = log.get("plan_id")
        if pid is None:
            continue
        pid = int(pid)
        if wanted is not None and pid not in wanted:
            continue
        if pid not in plan_syms:
            plan = db.get_plan(pid)
            if plan is None:
                plan_syms[pid] = None  # deleted plan: keep all historical symbols
            else:
                plan_syms[pid] = {str(s).strip().upper() for s in (plan.get("symbols") or [])}
        allow = plan_syms[pid]
        for symbol, avg, filled in _filled_legs_from_result(log.get("result_json")):
            if allow is not None and symbol not in allow:
                continue
            bucket = acc.setdefault(pid, {}).setdefault(symbol, [0.0, 0.0])
            bucket[0] += avg * filled
            bucket[1] += filled
    out: dict[int, dict[str, Any]] = {}
    for pid, by_sym in acc.items():
        symbols: dict[str, dict[str, float]] = {}
        total_shares = 0.0
        total_cost = 0.0
        for sym, (notional, qty) in by_sym.items():
            if qty <= 1e-9:
                continue
            avg_cost = round(notional / qty, 6)
            cost_basis = round(notional, 2)
            shares = round(qty, 6) if abs(qty - round(qty)) > 1e-9 else float(int(round(qty)))
            symbols[sym] = {
                "shares": shares,
                "cost_basis": cost_basis,
                "avg_cost": avg_cost,
            }
            total_shares += qty
            total_cost += notional
        if symbols:
            # Prefer whole-share display when all legs are integers
            if abs(total_shares - round(total_shares)) < 1e-9:
                total_shares_out: float = float(int(round(total_shares)))
            else:
                total_shares_out = round(total_shares, 6)
            out[pid] = {
                "by_symbol": symbols,
                "total_shares": total_shares_out,
                "total_cost_basis": round(total_cost, 2),
            }
    return out


def enrich_holdings_market_value(
    holdings: dict[int, dict[str, Any]],
    market_prices: dict[str, float | None] | None,
) -> dict[int, dict[str, Any]]:
    """
    Attach per-symbol and plan-level market_value using last quotes.

    total_market_value is the sum of shares*price for symbols with a quote.
    If no symbol has a quote, total_market_value is None.
    Mutates and returns the same holdings dict.
    """
    prices = market_prices or {}
    for _pid, h in holdings.items():
        mv_sum = 0.0
        any_quote = False
        for sym, row in (h.get("by_symbol") or {}).items():
            px = prices.get(sym)
            if px is None:
                row["market_value"] = None
                continue
            try:
                px_f = float(px)
            except (TypeError, ValueError):
                row["market_value"] = None
                continue
            shares = float(row.get("shares") or 0)
            mv = round(shares * px_f, 2)
            row["market_value"] = mv
            mv_sum += mv
            any_quote = True
        h["total_market_value"] = round(mv_sum, 2) if any_quote else None
    return holdings


def avg_fill_costs_by_plan(plan_ids: list[int] | None = None) -> dict[int, dict[str, float]]:
    """
    Weighted average fill price per symbol for each plan, from cached live order summaries.

    Formula per symbol: sum(average_price * filled_qty) / sum(filled_qty)
    Excludes dry-run logs and unfilled (filled_qty == 0) legs.
    Returns {plan_id: {symbol: avg_price}}. Missing plans / no fills → absent or {}.
    """
    holdings = dca_holdings_by_plan(plan_ids)
    out: dict[int, dict[str, float]] = {}
    for pid, h in holdings.items():
        prices = {
            sym: float(row["avg_cost"])
            for sym, row in (h.get("by_symbol") or {}).items()
            if row.get("avg_cost") is not None
        }
        if prices:
            out[pid] = prices
    return out


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _trading_day_str(dt: datetime) -> str:
    """Map a timestamp to America/New_York calendar date (YYYY-MM-DD)."""
    try:
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(et).date().isoformat()
    except Exception:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()


def live_fill_markers_for_plan(plan_id: int) -> dict[str, list[dict[str, Any]]]:
    """
    Live (non-dry-run) DCA fill markers keyed by symbol.

    Multiple fills on the same America/New_York trading day for a symbol are
    merged into ONE marker at volume-weighted average price (VWAP).

    Each marker: {date, price, filled_qty, fill_count, log_ids}.
      - price: VWAP = sum(avg * qty) / sum(qty)
      - filled_qty: total shares that day
      - fill_count: number of live fill legs aggregated
    Date prefers order close_time / entered_time, else run_log.created_at (ET day).
    Excludes dry-run / skipped / unfilled (qty==0 or avg<=0) legs.
    """
    logs = db.list_live_run_logs(plan_id=plan_id, limit=5000)
    plan = db.get_plan(plan_id)
    allow = (
        {str(s).strip().upper() for s in (plan.get("symbols") or [])}
        if plan is not None
        else None
    )
    # (symbol, day) -> [notional, qty, fill_count, log_ids]
    buckets: dict[tuple[str, str], list[Any]] = {}
    for log in logs:
        result = _parse_json(log.get("result_json")) or {}
        created = _parse_iso_dt(log.get("created_at"))
        fallback_day = _trading_day_str(created) if created else None
        log_id = log.get("id")
        for item in result.get("orders") or []:
            if not isinstance(item, dict):
                continue
            submit = str(item.get("status") or "").lower()
            if submit in ("dry_run", "skipped"):
                continue
            summary = item.get("order_status")
            if not isinstance(summary, dict):
                continue
            if str(summary.get("status") or "").upper() == "DRY_RUN":
                continue
            try:
                filled = float(summary.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                filled = 0.0
            if filled <= 1e-9:
                continue
            try:
                avg = float(summary.get("average_price") or 0)
            except (TypeError, ValueError):
                avg = 0.0
            if avg <= 0:
                continue
            symbol = (
                summary.get("symbol")
                or item.get("symbol")
                or (summary.get("symbols") or [None])[0]
                or "?"
            )
            symbol = str(symbol).strip().upper() or "?"
            if allow is not None and symbol not in allow:
                continue
            day = None
            for key in ("close_time", "entered_time"):
                dt = _parse_iso_dt(summary.get(key) if isinstance(summary.get(key), str) else None)
                if dt:
                    day = _trading_day_str(dt)
                    break
            if not day:
                day = fallback_day
            if not day:
                continue
            key = (symbol, day)
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = [avg * filled, filled, 1, [log_id] if log_id is not None else []]
            else:
                bucket[0] += avg * filled
                bucket[1] += filled
                bucket[2] += 1
                if log_id is not None and log_id not in bucket[3]:
                    bucket[3].append(log_id)
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for (symbol, day), (notional, qty, fill_count, log_ids) in buckets.items():
        if qty <= 1e-9:
            continue
        by_sym.setdefault(symbol, []).append(
            {
                "date": day,
                "price": round(notional / qty, 6),
                "filled_qty": qty,
                "fill_count": int(fill_count),
                "log_ids": log_ids,
            }
        )
    # Oldest first for chart tooltips
    for sym in by_sym:
        by_sym[sym].sort(key=lambda m: m["date"])
    return by_sym
