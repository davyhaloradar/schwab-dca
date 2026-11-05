"""Failure / skip / run notifier: local log + optional webhook + optional email."""
from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _dashboard_url() -> str:
    """Dashboard link for notification bodies. From DASHBOARD_URL or the public base."""
    return get_settings().public_dashboard_url()

# Trigger buckets for DCA_NOTIFY_ON_* gates (case-insensitive match).
_MANUAL_TRIGGERS: frozenset[str] = frozenset({"manual", "run_now", "ui"})
_SCHEDULED_TRIGGERS: frozenset[str] = frozenset({"scheduled"})
_PREVIEW_TRIGGERS: frozenset[str] = frozenset({"preview"})

_KIND_META: dict[str, tuple[str, str]] = {
    # kind -> (subject_tag, title)
    "run_complete": ("定投", "计划执行完成"),
    "insufficient_cash": ("定投·现金不足", "现金不足，已跳过下单"),
    "cash_unknown": ("定投·现金读取失败", "无法读取现金余额"),
    "policy_reject": ("定投·策略拒绝", "策略/限额拒绝执行"),
    "live_order_error": ("定投·下单失败", "实盘下单出错"),
    "live_run_partial_failure": ("定投·部分失败", "本次执行有错误"),
    "low_cash_warning": ("定投·现金预警", "可用现金偏低"),
}

_STATUS_CN: dict[str, str] = {
    "dry_run": "模拟成功",
    "filled": "已成交",
    "partial_fill": "部分成交",
    "submitted": "已提交",
    "submitted_unfilled": "已提交未成交",
    "cancelled": "已取消",
    "error": "错误",
    "skipped": "已跳过",
    "skipped_insufficient_cash": "跳过·现金不足",
    "skipped_cash_unknown": "跳过·现金未知",
    "skipped_policy": "跳过·策略拒绝",
}


def _notify_log_path() -> Path:
    settings = get_settings()
    return Path(settings.database_path).parent / "notify.log"


def _trigger_bucket(trigger: str | None) -> str | None:
    """Return 'manual' | 'scheduled' | 'preview' | None (other / unset)."""
    if trigger is None:
        return None
    key = trigger.strip().lower()
    if not key:
        return None
    if key in _MANUAL_TRIGGERS:
        return "manual"
    if key in _SCHEDULED_TRIGGERS:
        return "scheduled"
    if key in _PREVIEW_TRIGGERS:
        return "preview"
    return None


def _trigger_label_cn(trigger: str | None) -> str:
    bucket = _trigger_bucket(trigger)
    if bucket == "manual":
        return "手动运行"
    if bucket == "scheduled":
        return "自动调度"
    if bucket == "preview":
        return "预览确认"
    if trigger:
        return str(trigger)
    return "系统"


def _channels_skip_reason(
    settings: Any, *, kind: str, trigger: str | None
) -> str | None:
    """Return a reason if webhook/email should be skipped, else None."""
    bucket = _trigger_bucket(trigger)
    if bucket == "manual" and not settings.dca_notify_on_manual:
        return "DCA_NOTIFY_ON_MANUAL=false"
    if bucket == "scheduled" and not settings.dca_notify_on_scheduled:
        return "DCA_NOTIFY_ON_SCHEDULED=false"
    if bucket == "preview" and not settings.dca_notify_on_preview:
        return "DCA_NOTIFY_ON_PREVIEW=false"

    kinds = settings.notify_kinds_set()
    if kinds and (kind or "").strip().lower() not in kinds:
        return f"kind={kind!r} not in DCA_NOTIFY_KINDS"

    return None


def _email_configured(settings: Any) -> bool:
    to_addr = (getattr(settings, "dca_notify_email_to", None) or "").strip()
    user = (getattr(settings, "dca_notify_smtp_user", None) or "").strip()
    password = getattr(settings, "dca_notify_smtp_password", None) or ""
    enabled = getattr(settings, "dca_notify_email_enabled", None)
    if enabled is False:
        return False
    return bool(to_addr and user and password)


def _webhook_configured(settings: Any) -> bool:
    if not getattr(settings, "dca_notify_webhook_enabled", True):
        return False
    return bool((getattr(settings, "dca_notify_webhook_url", None) or "").strip())


def _resolve_from_addr(settings: Any) -> str:
    from_addr = (getattr(settings, "dca_notify_email_from", None) or "").strip()
    if from_addr:
        return from_addr
    to_addr = (getattr(settings, "dca_notify_email_to", None) or "").strip()
    if to_addr:
        return to_addr
    return (getattr(settings, "dca_notify_smtp_user", None) or "").strip()


def _now_shanghai() -> datetime:
    return datetime.now(_SHANGHAI)


def _fmt_money(val: Any) -> str | None:
    if val is None or val == "":
        return None
    try:
        return f"${float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_qty(val: Any) -> str:
    if val is None or val == "":
        return "—"
    try:
        f = float(val)
        if f == int(f):
            return str(int(f))
        return f"{f:g}"
    except (TypeError, ValueError):
        return str(val)


def _status_cn(status: Any) -> str:
    if status is None or status == "":
        return "—"
    key = str(status).strip().lower()
    return _STATUS_CN.get(key, str(status))


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def _build_subject(
    kind: str, *, plan_name: str | None, plan_id: int | None, extra: dict[str, Any]
) -> str:
    tag, title = _KIND_META.get(kind, ("定投", kind))
    name = (plan_name or "").strip() or (
        f"计划#{plan_id}" if plan_id is not None else "Schwab DCA"
    )
    status = extra.get("status")
    if kind == "run_complete" and status:
        title = f"计划执行完成 · {_status_cn(status)}"
    return f"【{tag}】{name} {title}"


def _summary_line(
    kind: str,
    *,
    message: str,
    extra: dict[str, Any],
    trigger: str | None,
) -> str:
    dry = extra.get("dry_run")
    mode = "模拟 DRY-RUN" if dry else ("实盘 LIVE" if dry is False else None)
    parts: list[str] = []
    if mode:
        parts.append(mode)
    parts.append(_trigger_label_cn(trigger))
    if kind == "run_complete" and extra.get("status"):
        parts.append(_status_cn(extra.get("status")))
    if extra.get("cash_available") is not None:
        money = _fmt_money(extra.get("cash_available"))
        if money:
            parts.append(f"现金 {money}")
    if extra.get("notional") is not None and kind in (
        "insufficient_cash",
        "cash_unknown",
        "run_complete",
    ):
        n = _fmt_money(extra.get("notional"))
        if n:
            parts.append(f"计划金额 {n}")
    if not parts:
        return (message or "").strip()[:200]
    return " · ".join(parts)


def _detail_rows(
    kind: str,
    *,
    plan_id: int | None,
    plan_name: str | None,
    message: str,
    extra: dict[str, Any],
    trigger: str | None,
    ts_local: str,
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("计划", (plan_name or "").strip() or (f"#{plan_id}" if plan_id is not None else "—")),
        ("触发", _trigger_label_cn(trigger)),
        ("时间", f"{ts_local}（Asia/Shanghai）"),
    ]
    if extra.get("dry_run") is True:
        rows.append(("模式", "模拟 DRY-RUN（未真实下单）"))
    elif extra.get("dry_run") is False:
        rows.append(("模式", "实盘 LIVE"))
    if extra.get("status") is not None:
        rows.append(("结果", _status_cn(extra.get("status"))))
    if extra.get("cash_available") is not None:
        label = "可用现金"
        field = extra.get("cash_field_used")
        money = _fmt_money(extra.get("cash_available")) or "—"
        if field:
            money = f"{money}（{field}）"
        rows.append((label, money))
    if extra.get("notional") is not None:
        rows.append(("计划金额", _fmt_money(extra.get("notional")) or "—"))
    if extra.get("symbol") and kind == "live_order_error":
        rows.append(("标的", str(extra.get("symbol"))))
    if message and kind not in ("run_complete",):
        rows.append(("说明", message.strip()[:500]))
    return rows


def _order_lines(extra: dict[str, Any]) -> list[dict[str, str]]:
    orders = extra.get("orders")
    out: list[dict[str, str]] = []
    if isinstance(orders, list):
        for o in orders:
            if not isinstance(o, dict):
                continue
            out.append(
                {
                    "symbol": str(o.get("symbol") or "—"),
                    "qty": _fmt_qty(
                        o.get("filled_quantity")
                        if o.get("filled_quantity") is not None
                        else o.get("quantity")
                    ),
                    "status": _status_cn(o.get("status")),
                    "price": _fmt_money(o.get("avg_price")) or "—",
                    "note": str(o.get("reason") or "")[:120],
                }
            )
        if out:
            return out
    symbols = extra.get("symbols")
    if isinstance(symbols, list) and symbols:
        for sym in symbols:
            out.append(
                {
                    "symbol": str(sym),
                    "qty": "—",
                    "status": _status_cn(extra.get("status")),
                    "price": "—",
                    "note": "",
                }
            )
    return out


def _low_cash_plan_lines(extra: dict[str, Any]) -> list[dict[str, str]]:
    plans = extra.get("plans")
    out: list[dict[str, str]] = []
    if not isinstance(plans, list):
        return out
    for p in plans:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "name": str(p.get("name") or f"#{p.get('plan_id')}"),
                "runs": str(p.get("remaining_runs") if p.get("remaining_runs") is not None else "—"),
                "cash": _fmt_money(p.get("cash")) or "—",
                "notional": _fmt_money(p.get("notional")) or "—",
            }
        )
    return out


def _build_email_bodies(
    kind: str,
    *,
    plan_id: int | None,
    plan_name: str | None,
    message: str,
    extra: dict[str, Any],
    trigger: str | None,
) -> tuple[str, str]:
    """Return (text/plain, text/html)."""
    ts = _now_shanghai()
    ts_local = ts.strftime("%Y-%m-%d %H:%M:%S")
    _, title = _KIND_META.get(kind, ("定投", kind))
    if kind == "run_complete" and extra.get("status"):
        title = f"计划执行完成 · {_status_cn(extra.get('status'))}"
    name = (plan_name or "").strip() or (
        f"计划 #{plan_id}" if plan_id is not None else "Schwab DCA"
    )
    summary = _summary_line(kind, message=message, extra=extra, trigger=trigger)
    details = _detail_rows(
        kind,
        plan_id=plan_id,
        plan_name=plan_name,
        message=message,
        extra=extra,
        trigger=trigger,
        ts_local=ts_local,
    )
    orders = _order_lines(extra)
    cash_plans = _low_cash_plan_lines(extra) if kind == "low_cash_warning" else []
    errors = extra.get("errors") if isinstance(extra.get("errors"), list) else []

    # --- plain text ---
    plain_lines = [
        f"【{title}】{name}",
        summary,
        "",
        "— 详情 —",
    ]
    for label, val in details:
        plain_lines.append(f"{label}：{val}")
    if orders:
        plain_lines.append("")
        plain_lines.append("— 标的 —")
        for o in orders:
            line = f"  {o['symbol']}  ×{o['qty']}  {o['status']}"
            if o["price"] != "—":
                line += f"  @ {o['price']}"
            if o["note"]:
                line += f"  ({o['note']})"
            plain_lines.append(line)
    if cash_plans:
        plain_lines.append("")
        plain_lines.append("— 预警计划 —")
        for p in cash_plans:
            plain_lines.append(
                f"  {p['name']}: 剩余约 {p['runs']} 次 · 现金 {p['cash']} / 每期 {p['notional']}"
            )
    if errors:
        plain_lines.append("")
        plain_lines.append("— 错误 —")
        for e in errors[:8]:
            plain_lines.append(f"  · {e}")
    plain_lines.extend(
        [
            "",
            f"控制台：{_dashboard_url()}",
            "— Schwab DCA 定投通知（请勿直接回复）—",
        ]
    )
    plain = "\n".join(plain_lines) + "\n"

    # --- html ---
    detail_trs = "".join(
        f"<tr><td class='k'>{_esc(k)}</td><td class='v'>{_esc(v)}</td></tr>"
        for k, v in details
    )
    order_block = ""
    if orders:
        ors = "".join(
            "<tr>"
            f"<td class='mono'>{_esc(o['symbol'])}</td>"
            f"<td class='mono num'>{_esc(o['qty'])}</td>"
            f"<td>{_esc(o['status'])}</td>"
            f"<td class='mono num'>{_esc(o['price'])}</td>"
            f"<td class='muted'>{_esc(o['note'])}</td>"
            "</tr>"
            for o in orders
        )
        order_block = (
            "<h2 class='sec'>标的明细</h2>"
            "<table class='tbl orders' cellpadding='0' cellspacing='0'>"
            "<thead><tr>"
            "<th>标的</th><th>数量</th><th>状态</th><th>均价</th><th>备注</th>"
            "</tr></thead>"
            f"<tbody>{ors}</tbody></table>"
        )
    cash_block = ""
    if cash_plans:
        crs = "".join(
            "<tr>"
            f"<td>{_esc(p['name'])}</td>"
            f"<td class='mono num'>{_esc(p['runs'])}</td>"
            f"<td class='mono num'>{_esc(p['cash'])}</td>"
            f"<td class='mono num'>{_esc(p['notional'])}</td>"
            "</tr>"
            for p in cash_plans
        )
        cash_block = (
            "<h2 class='sec'>预警计划</h2>"
            "<table class='tbl orders' cellpadding='0' cellspacing='0'>"
            "<thead><tr>"
            "<th>计划</th><th>剩余次数</th><th>现金</th><th>每期金额</th>"
            "</tr></thead>"
            f"<tbody>{crs}</tbody></table>"
        )
    err_block = ""
    if errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in errors[:8])
        err_block = f"<h2 class='sec'>错误</h2><ul class='errs'>{items}</ul>"

    accent = "#2563eb"
    if kind in ("insufficient_cash", "cash_unknown", "low_cash_warning"):
        accent = "#d97706"
    elif kind in ("live_order_error", "live_run_partial_failure", "policy_reject"):
        accent = "#dc2626"
    elif extra.get("dry_run") is True:
        accent = "#7c3aed"

    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{_esc(title)}</title>
<style>
  body {{ margin:0; padding:0; background:#f4f5f7; color:#1f2937;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
         font-size:15px; line-height:1.55; }}
  .wrap {{ max-width:560px; margin:0 auto; padding:24px 14px; }}
  .card {{ background:#fff; border-radius:12px; border:1px solid #e5e7eb;
           overflow:hidden; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .bar {{ height:4px; background:{accent}; }}
  .pad {{ padding:20px 22px 8px; }}
  h1 {{ margin:0 0 6px; font-size:1.15rem; font-weight:650; color:#111827; }}
  .sub {{ margin:0 0 14px; color:#6b7280; font-size:0.92rem; }}
  .sec {{ margin:18px 0 8px; font-size:0.8rem; font-weight:650; color:#6b7280;
          text-transform:none; letter-spacing:0.02em; }}
  table.meta {{ width:100%; border-collapse:collapse; }}
  table.meta td {{ padding:7px 0; border-bottom:1px solid #f3f4f6; vertical-align:top; }}
  table.meta td.k {{ width:28%; color:#6b7280; font-size:0.88rem; }}
  table.meta td.v {{ color:#111827; font-size:0.92rem; }}
  table.tbl {{ width:100%; border-collapse:collapse; font-size:0.88rem; }}
  table.tbl th {{ text-align:left; color:#9ca3af; font-weight:600; padding:6px 8px 6px 0;
                  border-bottom:1px solid #e5e7eb; font-size:0.75rem; }}
  table.tbl td {{ padding:8px 8px 8px 0; border-bottom:1px solid #f3f4f6; vertical-align:top; }}
  .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:0.9em; }}
  .num {{ white-space:nowrap; }}
  .muted {{ color:#9ca3af; font-size:0.82rem; }}
  ul.errs {{ margin:0 0 12px; padding-left:1.2rem; color:#b91c1c; font-size:0.88rem; }}
  .foot {{ padding:14px 22px 18px; color:#9ca3af; font-size:0.78rem; }}
  .foot a {{ color:#2563eb; text-decoration:none; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="bar"></div>
      <div class="pad">
        <h1>{_esc(title)}</h1>
        <p class="sub">{_esc(name)} · {_esc(summary)}</p>
        <h2 class="sec">详情</h2>
        <table class="meta">{detail_trs}</table>
        {order_block}
        {cash_block}
        {err_block}
      </div>
      <div class="foot">
        <a href="{_esc(_dashboard_url())}">打开定投控制台</a>
        · {_esc(ts_local)} Asia/Shanghai
        <br />Schwab DCA 定投通知 · 请勿直接回复本邮件
      </div>
    </div>
  </div>
</body>
</html>
"""
    return plain, html_body


def _send_email(
    settings: Any,
    *,
    kind: str,
    plan_id: int | None,
    plan_name: str | None,
    message: str,
    extra: dict[str, Any],
    trigger: str | None,
) -> None:
    to_addr = (settings.dca_notify_email_to or "").strip()
    from_addr = _resolve_from_addr(settings)
    user = (settings.dca_notify_smtp_user or "").strip()
    password = settings.dca_notify_smtp_password or ""
    host = (settings.dca_notify_smtp_host or "smtp.163.com").strip()
    port = int(settings.dca_notify_smtp_port or 465)

    subject = _build_subject(kind, plan_name=plan_name, plan_id=plan_id, extra=extra)
    plain, html_body = _build_email_bodies(
        kind,
        plan_id=plan_id,
        plan_name=plan_name,
        message=message,
        extra=extra,
        trigger=trigger,
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(plain)
    msg.add_alternative(html_body, subtype="html")

    logger.info(
        "Sending notify email kind=%s to=%s via %s:%s (user=%s) subject=%s",
        kind,
        to_addr,
        host,
        port,
        user,
        subject,
    )
    if port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)


def notify_event(
    kind: str,
    *,
    plan_id: int | None = None,
    plan_name: str | None = None,
    message: str,
    extra: dict[str, Any] | None = None,
    trigger: str | None = None,
) -> None:
    """
    Always append a clear line to data/notify.log.
    Optional webhook + email when policy settings allow the trigger/kind.
    Email is multipart (plain + HTML) with Chinese subjects.
    Best-effort: channel failures are logged, never raised.
    """
    settings = get_settings()
    skip_reason = _channels_skip_reason(settings, kind=kind, trigger=trigger)
    skip_channels = skip_reason is not None
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "plan_id": plan_id,
        "plan_name": plan_name,
        "message": message,
        "extra": extra or {},
        "trigger": trigger,
    }
    if skip_channels:
        payload["channels_skipped"] = True
        payload["channels_skip_reason"] = skip_reason

    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        path = _notify_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.exception("Failed writing notify.log")

    logger.warning(
        "DCA_NOTIFY [%s] plan=%s trigger=%s %s%s",
        kind,
        plan_id,
        trigger,
        message,
        f" (channels skipped: {skip_reason})" if skip_channels else "",
    )

    if skip_channels:
        return

    if _webhook_configured(settings):
        url = (settings.dca_notify_webhook_url or "").strip()
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.warning(
                        "Notify webhook HTTP %s: %s",
                        resp.status_code,
                        resp.text[:300],
                    )
        except Exception:
            logger.exception("Notify webhook POST failed")

    if _email_configured(settings):
        try:
            _send_email(
                settings,
                kind=kind,
                plan_id=plan_id,
                plan_name=plan_name,
                message=message,
                extra=extra or {},
                trigger=trigger,
            )
        except Exception:
            logger.exception("Notify email send failed")


def send_test_email() -> tuple[bool, str]:
    """Send one test message via current SMTP settings (bypasses trigger gates).

    Returns (ok, message_cn). Does not log the SMTP password.
    """
    settings = get_settings()
    if not settings.email_notify_enabled():
        return False, "邮件通知总开关已关闭（DCA_NOTIFY_EMAIL_ENABLED=false）"
    if not _email_configured(settings):
        return False, "邮件未配置完整：需要收件人、SMTP 用户与授权码"
    to_addr = (settings.dca_notify_email_to or "").strip()
    try:
        _send_email(
            settings,
            kind="run_complete",
            plan_id=None,
            plan_name="通知测试",
            message="这是一封来自 Schwab DCA 的测试邮件。若收到说明 SMTP 配置正常。",
            extra={"dry_run": True, "test": True},
            trigger="scheduled",
        )
    except Exception as exc:
        logger.exception("Test notify email failed")
        # Never include password; str(exc) from smtplib is usually safe.
        return False, f"发送失败：{exc}"
    return True, f"测试邮件已发送至 {to_addr}"
