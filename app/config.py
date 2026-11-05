"""Application settings loaded from environment / .env."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Always trusted for CSRF / internal redirects (local dev and loopback).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})
# Bind-all addresses are not browser Host headers.
_WILDCARD_BIND = frozenset({"", "0.0.0.0", "::"})


def _hostname_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
    except Exception:
        return ""
    return (parsed.hostname or "").lower().strip()


def _origin_from_url(url: str) -> str:
    """scheme://host[:port] with default ports omitted. Empty if not http(s)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    port = parsed.port
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    if ":" in host:
        host_part = f"[{host}]"
    else:
        host_part = host
    if port and not default_port:
        return f"{parsed.scheme}://{host_part}:{port}"
    return f"{parsed.scheme}://{host_part}"


def _is_ip_literal(host: str) -> bool:
    if ":" in host:
        return True
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        number = int(part)
        if number < 0 or number > 255:
            return False
    return True


def _normalize_request_host(host: str | None) -> str:
    if not host:
        return ""
    value = host.strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 1 else ""
    if "://" in value:
        return _hostname_from_url(value)
    # Drop a trailing :port. Bare IPv6 has more than one colon and is left as-is
    # only when it was not wrapped in brackets (unusual for Host headers).
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.strip().strip(".")


def _www_counterpart(host: str) -> str | None:
    """Apex ↔ www pair. Not applied to localhost, IPs, or single-label names."""
    if not host or host in _LOCAL_HOSTS or _is_ip_literal(host) or "." not in host:
        return None
    if host.startswith("www.") and len(host) > 4:
        return host[4:] or None
    if not host.startswith("www."):
        return "www." + host
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    schwab_app_key: str = ""
    schwab_app_secret: str = ""
    # Must match the Schwab Developer Portal callback exactly.
    # Default is local HTTPS under APP_ROOT_PATH=/plan.
    # Production example: https://example.com/plan/callback
    schwab_callback_url: str = "https://127.0.0.1:8765/plan/callback"

    app_host: str = "127.0.0.1"
    app_port: int = 8765

    dry_run: bool = True

    # URL prefix when reverse-proxied (e.g. https://example.com/plan).
    app_root_path: str = "/plan"

    # Public origin, no path (https://example.com). Empty → SCHWAB_CALLBACK_URL origin.
    app_public_base_url: str = ""
    # Full dashboard link in notification emails. Empty → public origin + APP_ROOT_PATH.
    dashboard_url: str = ""
    # Comma-separated CSRF / redirect host allowlist. Empty → derive from the
    # callback URL (and APP_PUBLIC_BASE_URL / DASHBOARD_URL when set), plus
    # localhost and 127.0.0.1. The apex and www forms of each public host are
    # both included. Other subdomains: list them, or use *.example.com.
    allowed_hosts: str = ""

    token_key_file: str = str(DATA_DIR / "token.key")
    database_path: str = str(DATA_DIR / "app.db")

    # Fund-safety hard caps (LIVE refuse; dry-run may flag)
    dca_max_shares_per_symbol: int = 5
    dca_max_notional_usd: float = 2000.0
    # Reject a second successful LIVE submit for the same plan within this window
    dca_live_idempotency_sec: int = 60

    # Order policy (Feature 3)
    # Comma-separated allowlist; empty = allow any valid ticker
    dca_symbol_allowlist: str = ""
    # Cumulative LIVE notional across all plans for the current US/ET calendar day
    dca_max_daily_notional_usd: float = 5000.0

    # Notifications (Feature 2) — always logs to data/notify.log; channels are gated.
    dca_notify_webhook_url: str = ""
    # Master webhook on/off (empty URL still disables POST). Default on when URL set.
    dca_notify_webhook_enabled: bool = True

    # Email notifications (163 SMTP). Sends only when TO + USER + PASSWORD are set.
    # DCA_NOTIFY_EMAIL_ENABLED=false forces off even if credentials are present.
    dca_notify_email_enabled: bool | None = None
    dca_notify_email_to: str = ""
    dca_notify_email_from: str = ""  # default: TO, else SMTP user
    dca_notify_smtp_host: str = "smtp.163.com"
    dca_notify_smtp_port: int = 465  # 465=SSL; other ports use STARTTLS (e.g. 587)
    dca_notify_smtp_user: str = ""  # 163 full email
    dca_notify_smtp_password: str = ""  # 163 authorization code, not login password

    # Which run triggers may send webhook/email (notify.log always written).
    dca_notify_on_manual: bool = True  # manual / run_now / ui
    dca_notify_on_scheduled: bool = True
    dca_notify_on_preview: bool = False  # preview confirmations — off by default
    # Optional kind allowlist (comma-separated). Empty = all kinds.
    # e.g. run_complete,insufficient_cash,live_order_error,low_cash_warning
    dca_notify_kinds: str = ""

    # Due tick interval seconds (Feature 1 durable scanner)
    dca_due_tick_sec: int = 120

    # Cash monitoring (MARGIN-SAFE: never use buyingPower)
    # Warn on dashboard / notify when remaining scheduled runs ≤ this threshold
    dca_cash_warn_runs: int = 3
    # Min seconds between low-cash notify webhook/log spam (default 6h)
    dca_cash_warn_cooldown_sec: int = 21600
    # LIVE: if cash balance cannot be read, skip orders (safer on margin accounts)
    dca_require_cash_read: bool = True

    # 5-minute VWAP 做T monitor. v1 never places Schwab orders (T_LIVE_ORDERS is ignored).
    t_tick_sec: int = 5
    t_default_symbols: str = "QQQM,AAPL"
    t_notional_usd: float = 2000.0
    t_live_orders: bool = False

    def symbol_allowlist_set(self) -> set[str]:
        raw = (self.dca_symbol_allowlist or "").strip()
        if not raw:
            return set()
        return {s.strip().upper() for s in raw.replace(";", ",").split(",") if s.strip()}

    def notify_kinds_set(self) -> set[str]:
        """Empty set means no kind filter (allow all)."""
        raw = (self.dca_notify_kinds or "").strip()
        if not raw:
            return set()
        return {s.strip().lower() for s in raw.replace(";", ",").split(",") if s.strip()}

    def email_notify_enabled(self) -> bool:
        """True when email channel should attempt send (credentials still required)."""
        if self.dca_notify_email_enabled is False:
            return False
        return True

    def public_dashboard_url(self) -> str:
        """Dashboard link for notification emails (DASHBOARD_URL, else public origin)."""
        explicit = (self.dashboard_url or "").strip()
        if explicit:
            return explicit if explicit.endswith("/") else explicit + "/"
        base = (self.app_public_base_url or "").strip().rstrip("/")
        if not base:
            base = _origin_from_url(self.schwab_callback_url)
        if not base:
            host = (self.app_host or "127.0.0.1").strip() or "127.0.0.1"
            if host in _WILDCARD_BIND:
                host = "127.0.0.1"
            base = f"http://{host}:{int(self.app_port)}"
        root = (self.app_root_path or "").strip()
        if root and not root.startswith("/"):
            root = "/" + root
        root = root.rstrip("/")
        return f"{base}{root}/"

    def allowed_host_rules(self) -> tuple[set[str], set[str]]:
        """Return (exact hosts, suffix bases). localhost and 127.0.0.1 are always exact.

        An empty ALLOWED_HOSTS derives public hosts from the callback URL and,
        when set, APP_PUBLIC_BASE_URL and DASHBOARD_URL. An explicit list replaces
        that derivation (it does not union with the callback host).
        """
        exact: set[str] = set(_LOCAL_HOSTS)
        suffixes: set[str] = set()
        raw = (self.allowed_hosts or "").strip()
        if not raw:
            seeds: set[str] = set()
            for candidate in (
                _hostname_from_url(self.schwab_callback_url),
                _hostname_from_url(self.app_public_base_url),
                _hostname_from_url(self.dashboard_url),
                (self.app_host or "").strip().lower(),
            ):
                if candidate and candidate not in _WILDCARD_BIND:
                    seeds.add(candidate)
            for host in list(seeds):
                pair = _www_counterpart(host)
                if pair:
                    seeds.add(pair)
            exact |= seeds
            return exact, suffixes

        for entry in raw.replace(";", ",").split(","):
            item = entry.strip().lower()
            if not item:
                continue
            if item.startswith("*."):
                host = _normalize_request_host(item[2:])
                if host:
                    suffixes.add(host)
                    exact.add(host)
                continue
            if item.startswith(".") and not item.startswith(".."):
                host = _normalize_request_host(item[1:])
                if host:
                    suffixes.add(host)
                    exact.add(host)
                continue
            host = _normalize_request_host(item)
            if host and host not in _WILDCARD_BIND:
                exact.add(host)
        return exact, suffixes

    def host_allowed(self, host: str | None) -> bool:
        """True if this browser host may supply Origin/Referer or an internal redirect."""
        normalized = _normalize_request_host(host)
        if not normalized:
            return False
        exact, suffixes = self.allowed_host_rules()
        if normalized in exact:
            return True
        return any(
            normalized == suffix or normalized.endswith("." + suffix) for suffix in suffixes
        )

    # Schwab OAuth / API base URLs
    schwab_authorize_url: str = "https://api.schwabapi.com/v1/oauth/authorize"
    schwab_token_url: str = "https://api.schwabapi.com/v1/oauth/token"
    schwab_trader_base: str = "https://api.schwabapi.com/trader/v1"
    schwab_marketdata_base: str = "https://api.schwabapi.com/marketdata/v1"

    @property
    def oauth_configured(self) -> bool:
        return bool(self.schwab_app_key and self.schwab_app_secret)


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()


def set_env_keys(updates: dict[str, str]) -> None:
    """Write KEY=value pairs into process env and .env; clear settings cache.

    Values must be single-line (no newlines). Empty string is a valid value
    (writes KEY=). Callers decide whether to omit a key to leave it unchanged.
    """
    if not updates:
        get_settings.cache_clear()
        return
    cleaned: dict[str, str] = {}
    for key, value in updates.items():
        if not key or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid env key: {key!r}")
        if "\n" in value or "\r" in value:
            raise ValueError(f"env value for {key} must be single-line")
        cleaned[key] = value
        os.environ[key] = value

    env_path = ROOT / ".env"
    if env_path.exists():
        raw = env_path.read_text(encoding="utf-8")
    else:
        raw = ""
    for key, value in cleaned.items():
        line = f"{key}={value}"
        if re.search(rf"(?m)^{re.escape(key)}=", raw):
            raw = re.sub(rf"(?m)^{re.escape(key)}=.*$", line, raw)
        else:
            if raw and not raw.endswith("\n"):
                raw += "\n"
            raw += line + "\n"
    if raw and not raw.endswith("\n"):
        raw += "\n"
    env_path.write_text(raw, encoding="utf-8")
    get_settings.cache_clear()


def _persist_env_bool(env_key: str, enabled: bool) -> None:
    """Write KEY=true|false into process env and .env; clear settings cache."""
    set_env_keys({env_key: "true" if enabled else "false"})


def set_dry_run(enabled: bool) -> bool:
    """Persist DRY_RUN to .env + process env and clear settings cache.

    Returns the new dry_run value. Takes effect for subsequent get_settings()
    / execute_plan() without restarting the process.
    """
    _persist_env_bool("DRY_RUN", enabled)
    return get_settings().dry_run


def set_notify_email_enabled(enabled: bool) -> bool:
    """Persist DCA_NOTIFY_EMAIL_ENABLED; returns effective email_notify_enabled()."""
    _persist_env_bool("DCA_NOTIFY_EMAIL_ENABLED", enabled)
    return get_settings().email_notify_enabled()


def set_notify_on_manual(enabled: bool) -> bool:
    """Persist DCA_NOTIFY_ON_MANUAL; returns the new setting."""
    _persist_env_bool("DCA_NOTIFY_ON_MANUAL", enabled)
    return get_settings().dca_notify_on_manual


def set_notify_smtp_settings(
    *,
    email_to: str,
    email_from: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str | None,
) -> Settings:
    """Persist notify email destination + SMTP knobs.

    smtp_password=None means leave DCA_NOTIFY_SMTP_PASSWORD unchanged
    (UI blank submit). Empty string clears the password.
    """
    updates: dict[str, str] = {
        "DCA_NOTIFY_EMAIL_TO": (email_to or "").strip(),
        "DCA_NOTIFY_EMAIL_FROM": (email_from or "").strip(),
        "DCA_NOTIFY_SMTP_HOST": (smtp_host or "").strip() or "smtp.163.com",
        "DCA_NOTIFY_SMTP_PORT": str(int(smtp_port)),
        "DCA_NOTIFY_SMTP_USER": (smtp_user or "").strip(),
    }
    if smtp_password is not None:
        # Do not strip interior spaces; auth codes can be exact.
        updates["DCA_NOTIFY_SMTP_PASSWORD"] = smtp_password.replace("\r", "").replace(
            "\n", ""
        )
    set_env_keys(updates)
    return get_settings()
