#!/usr/bin/env python3
"""Validate notify_event policy gates and email wiring without sending mail.

Run from repo root:
  .venv/bin/python scripts/test_notify_gates.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmpdir = tempfile.mkdtemp(prefix="dca-notify-test-")
os.environ["DATABASE_PATH"] = str(Path(_tmpdir) / "app.db")
os.environ["DCA_NOTIFY_WEBHOOK_URL"] = "https://example.test/hook"
os.environ["DCA_NOTIFY_WEBHOOK_ENABLED"] = "true"
os.environ["DCA_NOTIFY_EMAIL_ENABLED"] = "true"
os.environ["DCA_NOTIFY_EMAIL_TO"] = "you@example.com"
os.environ["DCA_NOTIFY_SMTP_USER"] = "you@example.com"
os.environ["DASHBOARD_URL"] = "https://example.com/plan/"
os.environ["ALLOWED_HOSTS"] = ""
os.environ["APP_PUBLIC_BASE_URL"] = ""
os.environ["DCA_NOTIFY_SMTP_PASSWORD"] = "fake-auth-code-not-real"
os.environ["DCA_NOTIFY_SMTP_HOST"] = "smtp.163.com"
os.environ["DCA_NOTIFY_SMTP_PORT"] = "465"
os.environ["DCA_NOTIFY_ON_MANUAL"] = "true"
os.environ["DCA_NOTIFY_ON_SCHEDULED"] = "true"
os.environ["DCA_NOTIFY_ON_PREVIEW"] = "false"
os.environ["DCA_NOTIFY_KINDS"] = ""

from app.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()

from app.services import notify  # noqa: E402


def _check_public_hosts() -> list[str]:
    """Callback / allowlist / dashboard URL are configuration, not a personal domain."""
    failures: list[str] = []
    derived = Settings(
        schwab_callback_url="https://broker.example.com/plan/callback",
        app_public_base_url="",
        dashboard_url="",
        allowed_hosts="",
        app_host="127.0.0.1",
        app_port=8765,
        app_root_path="/plan",
    )
    if derived.public_dashboard_url() != "https://broker.example.com/plan/":
        failures.append(f"derived dashboard: {derived.public_dashboard_url()!r}")
    for host in ("broker.example.com", "www.broker.example.com", "localhost", "127.0.0.1"):
        if not derived.host_allowed(host):
            failures.append(f"expected allow {host}")
    for host in ("evil.example", "notbroker.example.com", "broker.example.com.evil"):
        if derived.host_allowed(host):
            failures.append(f"unexpected allow {host}")

    local = Settings(
        schwab_callback_url="https://127.0.0.1:8765/plan/callback",
        app_public_base_url="",
        dashboard_url="",
        allowed_hosts="",
        app_host="127.0.0.1",
        app_port=8765,
        app_root_path="/plan",
    )
    if local.public_dashboard_url() != "https://127.0.0.1:8765/plan/":
        failures.append(f"local dashboard: {local.public_dashboard_url()!r}")
    if local.host_allowed("example.com"):
        failures.append("local default allowed an unrelated public host")

    public = Settings(
        schwab_callback_url="https://127.0.0.1:8765/plan/callback",
        app_public_base_url="https://example.com/",
        dashboard_url="",
        allowed_hosts="",
        app_host="0.0.0.0",
        app_port=8765,
        app_root_path="/plan",
    )
    if public.public_dashboard_url() != "https://example.com/plan/":
        failures.append(f"public base dashboard: {public.public_dashboard_url()!r}")
    if not public.host_allowed("example.com") or not public.host_allowed("www.example.com"):
        failures.append("public base host pair not allowed")
    if public.host_allowed("0.0.0.0"):
        failures.append("wildcard bind address should not be an allowed host")

    explicit = Settings(
        schwab_callback_url="https://other.test/plan/callback",
        app_public_base_url="",
        dashboard_url="https://example.com/plan",
        allowed_hosts="example.com,*.example.com",
        app_host="127.0.0.1",
        app_root_path="/plan",
    )
    if explicit.public_dashboard_url() != "https://example.com/plan/":
        failures.append(f"explicit dashboard: {explicit.public_dashboard_url()!r}")
    if not explicit.host_allowed("a.example.com") or not explicit.host_allowed("example.com"):
        failures.append("explicit wildcard allowlist missed a host")
    if not explicit.host_allowed("broker.example.com"):
        failures.append("wildcard subdomain not allowed")
    if explicit.host_allowed("other.test"):
        failures.append("callback host leaked into explicit allowlist")
    if not explicit.host_allowed("localhost"):
        failures.append("localhost dropped from explicit allowlist")
    if explicit.host_allowed("example.com.evil") or explicit.host_allowed("notexample.com"):
        failures.append("wildcard matched a lookalike host")
    return failures


def _read_last_log() -> dict:
    path = notify._notify_log_path()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1])


def _mock_http_smtp(mock_client, mock_ssl, mock_smtp=None):
    client_inst = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    client_inst.__enter__.return_value = client_inst
    client_inst.__exit__.return_value = False
    client_inst.post.return_value = resp
    mock_client.return_value = client_inst

    smtp_inst = MagicMock()
    smtp_inst.__enter__.return_value = smtp_inst
    smtp_inst.__exit__.return_value = False
    mock_ssl.return_value = smtp_inst
    if mock_smtp is not None:
        mock_smtp.return_value = smtp_inst
    return client_inst, smtp_inst


def _reload_settings(**env):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    get_settings.cache_clear()
    return get_settings()


def main() -> int:
    settings = get_settings()
    assert notify._email_configured(settings), "email should be configured in test env"
    assert settings.dca_notify_smtp_password == "fake-auth-code-not-real"

    failures: list[str] = []

    # --- Manual / run_now / ui: send (ON_MANUAL=true) ---
    for trig in ("manual", "run_now", "ui", "Manual", "RUN_NOW"):
        with (
            patch("app.services.notify.httpx.Client") as mock_client,
            patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
            patch("app.services.notify.smtplib.SMTP") as mock_smtp,
        ):
            _, smtp_inst = _mock_http_smtp(mock_client, mock_ssl, mock_smtp)
            notify.notify_event(
                "run_complete",
                plan_id=1,
                plan_name="QQQM",
                message="manual should send channels",
                extra={
                    "status": "dry_run",
                    "dry_run": True,
                    "orders": [
                        {"symbol": "QQQM", "quantity": 1, "status": "dry_run"},
                    ],
                },
                trigger=trig,
            )
            if not mock_client.called:
                failures.append(f"webhook NOT called for trigger={trig!r}")
            if not mock_ssl.called:
                failures.append(f"SMTP_SSL NOT called for trigger={trig!r}")
            if mock_smtp.called:
                failures.append(f"plain SMTP unexpectedly used for trigger={trig!r}")
            # Subject / multipart checks
            if smtp_inst.send_message.called:
                msg = smtp_inst.send_message.call_args[0][0]
                subj = msg["Subject"] or ""
                if "【定投】" not in subj or "QQQM" not in subj:
                    failures.append(f"subject not CN-styled for {trig!r}: {subj!r}")
                if not msg.is_multipart():
                    failures.append(f"email not multipart for {trig!r}")
        row = _read_last_log()
        if row.get("channels_skipped"):
            failures.append(f"log should NOT skip channels for trigger={trig!r}")

    # --- Preview: skipped by default (ON_PREVIEW=false) ---
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=9,
            plan_name="PreviewPlan",
            message="preview should skip",
            trigger="preview",
        )
        if mock_client.called:
            failures.append("webhook should NOT fire for preview (default off)")
        if mock_ssl.called:
            failures.append("email should NOT fire for preview (default off)")
    row = _read_last_log()
    if not row.get("channels_skipped"):
        failures.append("preview should skip channels by default")

    # --- Scheduled: webhook + email ---
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
        patch("app.services.notify.smtplib.SMTP") as mock_smtp,
    ):
        _, smtp_inst = _mock_http_smtp(mock_client, mock_ssl, mock_smtp)
        notify.notify_event(
            "run_complete",
            plan_id=2,
            plan_name="SchedPlan",
            message="status=dry_run dry_run=True orders=1 errors=0",
            extra={"status": "dry_run", "dry_run": True},
            trigger="scheduled",
        )
        if not mock_client.called:
            failures.append("webhook NOT called for scheduled")
        if not mock_ssl.called:
            failures.append("SMTP_SSL NOT called for scheduled on port 465")
        if mock_smtp.called:
            failures.append("plain SMTP unexpectedly used on port 465")
        if smtp_inst.login.called:
            args = smtp_inst.login.call_args[0]
            if args[0] != "you@example.com":
                failures.append(f"login user unexpected: {args[0]!r}")

    row = _read_last_log()
    if row.get("channels_skipped"):
        failures.append("scheduled should not skip channels")

    # --- None trigger (low-cash): channels allowed ---
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _, smtp_inst = _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "low_cash_warning",
            plan_id=3,
            plan_name="CashPlan",
            message="low cash",
            extra={
                "threshold": 3,
                "plans": [
                    {
                        "plan_id": 3,
                        "name": "CashPlan",
                        "remaining_runs": 2,
                        "cash": 150.5,
                        "notional": 100,
                    }
                ],
            },
            trigger=None,
        )
        if not mock_client.called:
            failures.append("webhook NOT called for trigger=None")
        if not mock_ssl.called:
            failures.append("email NOT called for trigger=None")
        if smtp_inst.send_message.called:
            msg = smtp_inst.send_message.call_args[0][0]
            if "现金预警" not in (msg["Subject"] or ""):
                failures.append(f"low_cash subject wrong: {msg['Subject']!r}")

    # --- ON_MANUAL=false skips manual ---
    _reload_settings(DCA_NOTIFY_ON_MANUAL="false")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=5,
            plan_name="NoManual",
            message="should skip",
            trigger="manual",
        )
        if mock_client.called or mock_ssl.called:
            failures.append("ON_MANUAL=false should skip channels for manual")
    if not _read_last_log().get("channels_skipped"):
        failures.append("ON_MANUAL=false log should mark channels_skipped")
    _reload_settings(DCA_NOTIFY_ON_MANUAL="true")

    # --- ON_SCHEDULED=false skips scheduled ---
    _reload_settings(DCA_NOTIFY_ON_SCHEDULED="false")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=6,
            plan_name="NoSched",
            message="should skip",
            trigger="scheduled",
        )
        if mock_client.called or mock_ssl.called:
            failures.append("ON_SCHEDULED=false should skip scheduled")
    _reload_settings(DCA_NOTIFY_ON_SCHEDULED="true")

    # --- WEBHOOK_ENABLED=false: no webhook, email still ---
    _reload_settings(DCA_NOTIFY_WEBHOOK_ENABLED="false")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=7,
            plan_name="NoHook",
            message="email only",
            trigger="scheduled",
        )
        if mock_client.called:
            failures.append("WEBHOOK_ENABLED=false should not POST")
        if not mock_ssl.called:
            failures.append("email should still send when webhook disabled")
    _reload_settings(DCA_NOTIFY_WEBHOOK_ENABLED="true")

    # --- EMAIL_ENABLED=false: no email, webhook still ---
    _reload_settings(DCA_NOTIFY_EMAIL_ENABLED="false")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=8,
            plan_name="NoMail",
            message="webhook only",
            trigger="scheduled",
        )
        if not mock_client.called:
            failures.append("webhook should send when email disabled")
        if mock_ssl.called:
            failures.append("EMAIL_ENABLED=false should not SMTP")
    _reload_settings(DCA_NOTIFY_EMAIL_ENABLED="true")

    # --- KINDS filter ---
    _reload_settings(DCA_NOTIFY_KINDS="insufficient_cash,low_cash_warning")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "run_complete",
            plan_id=10,
            plan_name="Filtered",
            message="kind filtered",
            trigger="scheduled",
        )
        if mock_client.called or mock_ssl.called:
            failures.append("KINDS filter should skip run_complete")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "insufficient_cash",
            plan_id=10,
            plan_name="Filtered",
            message="cash short",
            extra={"cash_available": 10, "notional": 100},
            trigger="scheduled",
        )
        if not mock_client.called or not mock_ssl.called:
            failures.append("KINDS filter should allow insufficient_cash")
    _reload_settings(DCA_NOTIFY_KINDS="")

    # --- STARTTLS path when port != 465 ---
    _reload_settings(DCA_NOTIFY_SMTP_PORT="587")
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
        patch("app.services.notify.smtplib.SMTP") as mock_smtp,
    ):
        _, smtp_inst = _mock_http_smtp(mock_client, mock_ssl, mock_smtp)
        notify.notify_event(
            "run_complete",
            plan_id=4,
            plan_name="TlsPlan",
            message="starttls check",
            trigger="scheduled",
        )
        if mock_ssl.called:
            failures.append("SMTP_SSL used on port 587")
        if not mock_smtp.called:
            failures.append("SMTP STARTTLS not used on port 587")
        elif not smtp_inst.starttls.called:
            failures.append("starttls() not called on port 587")
    _reload_settings(DCA_NOTIFY_SMTP_PORT="465")

    # --- insufficient_cash subject ---
    with (
        patch("app.services.notify.httpx.Client") as mock_client,
        patch("app.services.notify.smtplib.SMTP_SSL") as mock_ssl,
    ):
        _, smtp_inst = _mock_http_smtp(mock_client, mock_ssl)
        notify.notify_event(
            "insufficient_cash",
            plan_id=11,
            plan_name="VOO周投",
            message="cash short",
            extra={"cash_available": 50.0, "notional": 200.0, "dry_run": False},
            trigger="scheduled",
        )
        msg = smtp_inst.send_message.call_args[0][0]
        if "现金不足" not in (msg["Subject"] or ""):
            failures.append(f"insufficient_cash subject: {msg['Subject']!r}")
        # HTML part present
        html_parts = [
            p for p in msg.iter_parts() if p.get_content_type() == "text/html"
        ]
        if not html_parts:
            failures.append("missing text/html part")
        else:
            body = html_parts[0].get_content()
            if "https://example.com/plan/" not in body:
                failures.append("HTML missing dashboard link")
            if "Asia/Shanghai" not in body:
                failures.append("HTML missing Shanghai timezone label")

    failures.extend(_check_public_hosts())

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print(
        "OK: notify gates (config ON_*/EMAIL/WEBHOOK/KINDS) + "
        "multipart CN email + 465 SSL / 587 STARTTLS"
    )
    print(f"notify.log under {_tmpdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
