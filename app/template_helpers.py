"""Jinja helpers for display: China time, qty formatting, fill badges."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        # Normalize Z and common SQLite/ISO forms
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def china_time(value: Any, with_label: bool = True) -> str:
    """Format timestamp as China time: 2026-10-30 12:32:48（中国时间）."""
    dt = _parse_dt(value)
    if dt is None:
        return str(value) if value is not None else "—"
    local = dt.astimezone(CHINA_TZ)
    text = local.strftime("%Y-%m-%d %H:%M:%S")
    if with_label:
        return f"{text}（中国时间）"
    return text


def china_time_html(value: Any) -> str:
    """Primary China time + muted UTC line (safe HTML fragment)."""
    dt = _parse_dt(value)
    if dt is None:
        raw = str(value) if value is not None else "—"
        return f'<span class="ts-china">{raw}</span>'
    local = dt.astimezone(CHINA_TZ)
    utc = dt.astimezone(timezone.utc)
    china = local.strftime("%Y-%m-%d %H:%M:%S")
    utc_s = utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f'<time class="ts-china" datetime="{utc.isoformat()}">'
        f"{china}<span class=\"ts-label\">（中国时间）</span></time>"
        f'<div class="muted ts-utc">{utc_s}</div>'
    )


def fmt_qty(value: Any) -> str:
    """Prefer integers for whole shares; keep decimals otherwise."""
    if value is None or value == "":
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    # trim trailing zeros
    s = f"{f:.6f}".rstrip("0").rstrip(".")
    return s


def fill_label(filled: Any, qty: Any, status: Any = None) -> dict[str, str]:
    """
    Return {text, badge, badge_class} for fill progress.
    e.g. 已成交 0 / 1 + 未成交 badge when pending with 0 filled.
    """
    status_u = (str(status) if status is not None else "").upper()
    if status_u == "DRY_RUN":
        return {"text": "—", "badge": "", "badge_class": ""}

    fq = None
    qq = None
    try:
        if filled is not None and filled != "":
            fq = float(filled)
    except (TypeError, ValueError):
        fq = None
    try:
        if qty is not None and qty != "":
            qq = float(qty)
    except (TypeError, ValueError):
        qq = None

    if fq is None and qq is None:
        return {"text": "—", "badge": "", "badge_class": ""}

    filled_s = fmt_qty(fq if fq is not None else 0)
    qty_s = fmt_qty(qq) if qq is not None else "?"
    text = f"已成交 {filled_s} / {qty_s}"

    filled_n = fq if fq is not None else 0.0
    qty_n = qq if qq is not None else None

    terminal_empty = status_u in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED")
    if qty_n is not None and filled_n + 1e-9 >= qty_n and qty_n > 0:
        return {"text": text, "badge": "全部成交", "badge_class": "badge-ok"}
    if filled_n > 1e-9 and (qty_n is None or filled_n + 1e-9 < qty_n):
        return {"text": text, "badge": "部分成交", "badge_class": "badge-warn"}
    if filled_n <= 1e-9:
        if terminal_empty:
            return {"text": text, "badge": "未成交", "badge_class": "badge-warn"}
        if status_u in ("FILLED",):
            # odd edge: status filled but qty 0 — still show 未成交 if numbers say so
            return {"text": text, "badge": "未成交", "badge_class": "badge-muted"}
        return {"text": text, "badge": "未成交", "badge_class": "badge-muted"}
    return {"text": text, "badge": "", "badge_class": ""}



def fmt_price(value: Any) -> str:
    """Format a USD price for display, e.g. 512.34 → 512.34."""
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f <= 0:
        return ""
    # Up to 4 decimals for fractional-ish fills; trim trailing zeros
    s = f"{f:.4f}".rstrip("0").rstrip(".")
    return s


def fill_price_label(average_price: Any, filled: Any, status: Any = None) -> str:
    """
    Chinese label for execution / average fill price.
    Filled (>0) with a price → 「成交价 $xxx」; otherwise 「成交价 —」.
    Dry-run returns empty (omit).
    """
    status_u = (str(status) if status is not None else "").upper()
    if status_u == "DRY_RUN":
        return ""

    filled_n = 0.0
    try:
        if filled is not None and filled != "":
            filled_n = float(filled)
    except (TypeError, ValueError):
        filled_n = 0.0

    price_s = fmt_price(average_price)
    if filled_n > 1e-9 and price_s:
        return f"成交价 ${price_s}"
    return "成交价 —"



def pnl_label(avg_cost: Any, last_price: Any) -> str:
    """
    Chinese PnL percent vs avg cost.
    Both present → 「盈亏 +1.2%」/「盈亏 -0.5%」 (HTML span with up/down class).
    Missing either → 「盈亏 —」 (muted).
    """
    try:
        avg = float(avg_cost) if avg_cost is not None and avg_cost != "" else None
    except (TypeError, ValueError):
        avg = None
    try:
        last = float(last_price) if last_price is not None and last_price != "" else None
    except (TypeError, ValueError):
        last = None
    if avg is None or avg <= 0 or last is None:
        return '<span class="muted pnl">盈亏 —</span>'
    pct = (last - avg) / avg * 100.0
    if pct > 0:
        return f'<span class="pnl pnl-up">盈亏 +{pct:.1f}%</span>'
    if pct < 0:
        return f'<span class="pnl pnl-down">盈亏 {pct:.1f}%</span>'
    return '<span class="pnl pnl-flat">盈亏 +0.0%</span>'




def _parse_log_json(log: Any, key: str) -> dict | None:
    import json

    if not isinstance(log, dict):
        return None
    raw = log.get(key)
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def order_status_cn(status: Any) -> dict[str, str]:
    """Map a Schwab order status to Chinese business wording for the UI."""
    s = (str(status) if status is not None else "").strip().upper()
    mapping = {
        "FILLED": ("已成交", "badge-ok"),
        "DRY_RUN": ("模拟·未真实下单", "badge-dry"),
        "CANCELED": ("已撤单", "badge-warn"),
        "CANCELLED": ("已撤单", "badge-warn"),
        "PENDING_CANCEL": ("撤单中", "badge-warn"),
        "REJECTED": ("已拒绝", "badge-warn"),
        "EXPIRED": ("已过期", "badge-warn"),
        "WORKING": ("未成交·已提交", "badge-muted"),
        "QUEUED": ("未成交·已提交", "badge-muted"),
        "ACCEPTED": ("未成交·已提交", "badge-muted"),
        "PENDING_ACTIVATION": ("未成交·已提交", "badge-muted"),
        "AWAITING_PARENT_ORDER": ("未成交·已提交", "badge-muted"),
        "AWAITING_CONDITION": ("未成交·已提交", "badge-muted"),
        "NEW": ("未成交·已提交", "badge-muted"),
        "SUBMITTED": ("未成交·已提交", "badge-muted"),
        "PARTIAL": ("部分成交", "badge-warn"),
        "AWAITING_MANUAL_REVIEW": ("审核中", "badge-muted"),
        "HASH_MISMATCH": ("账户不一致", "badge-warn"),
        "UNKNOWN": ("状态未知", "badge-muted"),
    }
    if s in mapping:
        label, badge = mapping[s]
        return {"code": s or "unknown", "label": label, "badge_class": badge}
    if not s:
        return {"code": "unknown", "label": "状态未知", "badge_class": "badge-muted"}
    # Unknown raw status — never dump API jargon as the primary label
    return {"code": s, "label": "处理中", "badge_class": "badge-muted"}


def _collect_order_summaries(log: Any) -> list[dict]:
    """Prefer enrich_log order_statuses; else cached order_status in result_json."""
    out: list[dict] = []
    if isinstance(log, dict) and log.get("order_statuses"):
        for o in log["order_statuses"]:
            if isinstance(o, dict):
                out.append(o)
        if out:
            return out
    result = _parse_log_json(log, "result_json") or {}
    for item in result.get("orders") or []:
        if not isinstance(item, dict):
            continue
        summary = item.get("order_status")
        if isinstance(summary, dict):
            row = dict(summary)
            row.setdefault("symbol", item.get("symbol"))
            out.append(row)
        else:
            # Submit-time status only (no Schwab reconcile yet)
            submit = str(item.get("status") or "").lower()
            if submit == "dry_run":
                out.append({"status": "DRY_RUN", "symbol": item.get("symbol"), "filled_quantity": 0})
            elif submit in ("submitted", "ok"):
                out.append(
                    {
                        "status": "SUBMITTED",
                        "symbol": item.get("symbol"),
                        "filled_quantity": 0,
                        "quantity": None,
                    }
                )
            elif submit in ("skipped", "skipped_insufficient_cash", "skipped_cash_unknown", "skipped_policy"):
                out.append({"status": submit.upper(), "symbol": item.get("symbol"), "submit_only": True})
            elif submit == "error":
                out.append({"status": "ERROR", "symbol": item.get("symbol"), "error": item.get("error")})
    return out


def _filled_qty(o: dict) -> float:
    try:
        return float(o.get("filled_quantity") or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_qty(o: dict) -> float | None:
    try:
        if o.get("quantity") is None or o.get("quantity") == "":
            return None
        return float(o["quantity"])
    except (TypeError, ValueError):
        return None


def run_status_info(log: Any) -> dict[str, str]:
    """
    Derive run outcome for UI from skip flags, dry_run, and reconciled Schwab
    order status — never green「成功」merely because execute_plan returned ok.

    Returns {code, label, badge_class, trigger_label, trigger_code}.
    """
    result = _parse_log_json(log, "result_json") or {}
    payload = _parse_log_json(log, "payload_json") or {}
    err = (log.get("error") if isinstance(log, dict) else None) or ""
    err_l = str(err).lower()
    status_code = str(result.get("status") or payload.get("status") or "").strip()
    blob = f"{status_code} {err}".lower()

    # Trigger is independent of result (dry/cancel/fill). Prefer payload, then
    # result_json (persisted on execute / preserved on reconcile). Legacy rows
    # without the field show 未记录 — never a blank dash.
    trigger = str(
        payload.get("trigger") or result.get("trigger") or ""
    ).strip().lower()
    if trigger in ("scheduled", "schedule", "cron", "due"):
        trigger_label, trigger_code = "已调度", "scheduled"
    elif trigger in ("manual", "run_now", "preview", "ui"):
        trigger_label, trigger_code = "手动运行", "manual"
    else:
        trigger_label, trigger_code = "未记录", "unrecorded"

    base_extra = {"trigger_label": trigger_label, "trigger_code": trigger_code}

    def _ret(code: str, label: str, badge_class: str) -> dict[str, str]:
        return {"code": code, "label": label, "badge_class": badge_class, **base_extra}

    if "skipped_insufficient_cash" in blob or "insufficient_cash" in blob:
        return _ret("skipped_insufficient_cash", "跳过·现金不足", "badge-warn")
    if "skipped_cash_unknown" in blob or "cash_unknown" in blob:
        return _ret("skipped_cash_unknown", "跳过·现金读取失败", "badge-warn")
    if "skipped_policy" in blob or "policy_reject" in blob:
        return _ret("skipped_policy", "跳过·策略拒绝", "badge-warn")
    if status_code.startswith("skipped_") or err_l.startswith("skipped_"):
        return _ret(status_code or "skipped", "跳过", "badge-warn")

    dry = bool(log.get("dry_run")) if isinstance(log, dict) else False
    if dry or status_code == "dry_run":
        return _ret("dry_run", "模拟成功", "badge-dry")

    # Prefer explicit outcome status persisted after reconcile.
    # Legacy "ok"/"success" are NOT success — fall through to order reconcile.
    outcome_map = {
        "filled": ("已成交", "badge-ok"),
        "cancelled": ("已撤单", "badge-warn"),
        "canceled": ("已撤单", "badge-warn"),
        "partial_fill": ("部分成交", "badge-warn"),
        "submitted_unfilled": ("未成交·已提交", "badge-muted"),
        "rejected": ("已拒绝", "badge-warn"),
        "expired": ("已过期", "badge-warn"),
        "error": ("失败", "badge-warn"),
        "partial_error": ("部分失败", "badge-warn"),
    }
    sc_l = status_code.lower()
    if sc_l in ("ok", "success", "succeeded"):
        # Old logs stamped ok on submit — ignore; derive from orders below
        pass
    elif sc_l in outcome_map:
        label, badge = outcome_map[sc_l]
        return _ret(sc_l, label, badge)

    summaries = _collect_order_summaries(log)
    if summaries:
        # Aggregate from live Schwab (or cached) order rows
        statuses = [str(o.get("status") or "").upper() for o in summaries]
        fills = [_filled_qty(o) for o in summaries]
        qtys = [_order_qty(o) for o in summaries]
        any_error = any(s == "ERROR" or o.get("error") for o, s in zip(summaries, statuses))
        cancel_set = {"CANCELED", "CANCELLED", "PENDING_CANCEL"}
        reject_set = {"REJECTED"}
        expire_set = {"EXPIRED"}
        working_set = {
            "WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "NEW", "SUBMITTED",
            "AWAITING_PARENT_ORDER", "AWAITING_CONDITION", "AWAITING_STOP_CONDITION",
            "AWAITING_MANUAL_REVIEW", "AWAITING_UR_OUT", "PENDING_ACKNOWLEDGEMENT",
            "PENDING_RECALL", "AWAITING_RELEASE_TIME",
        }

        all_filled = True
        any_filled = False
        all_cancelled_empty = True
        any_cancelled = False
        any_working = False
        any_rejected = False
        any_expired = False
        for o, s, fq, qq in zip(summaries, statuses, fills, qtys):
            if s == "DRY_RUN" or o.get("submit_only"):
                all_filled = False
                all_cancelled_empty = False
                continue
            if fq > 1e-9:
                any_filled = True
            fully = (qq is not None and fq + 1e-9 >= qq and qq > 0) or s == "FILLED"
            if not fully:
                all_filled = False
            if s in cancel_set:
                any_cancelled = True
                if fq > 1e-9:
                    all_cancelled_empty = False
            else:
                all_cancelled_empty = False
            if s in working_set:
                any_working = True
            if s in reject_set:
                any_rejected = True
            if s in expire_set:
                any_expired = True

        if any_error and not any_filled:
            return _ret("error", "失败", "badge-warn")
        if any_error and any_filled:
            return _ret("partial_error", "部分失败", "badge-warn")
        if all_filled and any_filled:
            return _ret("filled", "已成交", "badge-ok")
        if any_filled and (any_cancelled or any_working):
            return _ret("partial_fill", "部分成交", "badge-warn")
        if any_cancelled and all_cancelled_empty and not any_filled:
            return _ret("cancelled", "已撤单", "badge-warn")
        if any_cancelled and not any_filled:
            return _ret("cancelled", "已撤单", "badge-warn")
        if any_rejected and not any_filled:
            return _ret("rejected", "已拒绝", "badge-warn")
        if any_expired and not any_filled:
            return _ret("expired", "已过期", "badge-warn")
        if any_working or (statuses and not any_filled):
            return _ret("submitted_unfilled", "未成交·已提交", "badge-muted")

    if err.strip():
        return _ret("error", "失败", "badge-warn")

    # Live run logged as ok/submitted but no order rows / no reconcile yet
    if not dry:
        orders = result.get("orders") or []
        if any(isinstance(o, dict) and str(o.get("status") or "").lower() == "submitted" for o in orders):
            return _ret("submitted_unfilled", "未成交·已提交", "badge-muted")
        if orders:
            return _ret("submitted_unfilled", "未成交·已提交", "badge-muted")
        return _ret("unknown", "无订单", "badge-muted")

    return _ret(status_code or "unknown", "未知", "badge-muted")


def register_template_helpers(env) -> None:
    env.filters["china_time"] = china_time
    env.filters["fmt_qty"] = fmt_qty
    env.globals["china_time_html"] = china_time_html
    env.globals["fill_label"] = fill_label
    env.globals["fill_price_label"] = fill_price_label
    env.globals["fmt_qty"] = fmt_qty
    env.globals["fmt_price"] = fmt_price
    env.globals["pnl_label"] = pnl_label
    env.globals["run_status_info"] = run_status_info
    env.globals["order_status_cn"] = order_status_cn
