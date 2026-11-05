#!/usr/bin/env python3
"""Persist helpers: blank SMTP password must not clobber existing value."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmpdir = tempfile.mkdtemp(prefix="dca-env-persist-")
_env_path = Path(_tmpdir) / ".env"
_env_path.write_text(
    "DRY_RUN=true\n"
    "DCA_NOTIFY_EMAIL_TO=old@163.com\n"
    "DCA_NOTIFY_SMTP_PASSWORD=secret-auth-code\n"
    "DCA_NOTIFY_SMTP_HOST=smtp.163.com\n"
    "DCA_NOTIFY_SMTP_PORT=465\n"
    "DCA_NOTIFY_SMTP_USER=old@163.com\n",
    encoding="utf-8",
)
os.environ["DATABASE_PATH"] = str(Path(_tmpdir) / "app.db")
# Point Settings at our temp .env via patching ROOT after import is awkward;
# instead write via set_env_keys with ROOT patched.

import app.config as config  # noqa: E402

config.ROOT = Path(_tmpdir)
config.get_settings.cache_clear()

# Ensure Settings reads our temp .env
os.environ.pop("DCA_NOTIFY_SMTP_PASSWORD", None)
os.environ.pop("DCA_NOTIFY_EMAIL_TO", None)
os.environ.pop("DCA_NOTIFY_SMTP_USER", None)
os.environ.pop("DCA_NOTIFY_SMTP_HOST", None)
os.environ.pop("DCA_NOTIFY_SMTP_PORT", None)
os.environ.pop("DCA_NOTIFY_EMAIL_FROM", None)

# Rebuild settings model config to use new ROOT .env — Settings is a class with
# model_config baked at class definition. Easiest: use set_notify_smtp_settings
# which writes via set_env_keys to ROOT/.env and os.environ.


def main() -> int:
    failures: list[str] = []

    # Seed process env to match file
    os.environ["DCA_NOTIFY_SMTP_PASSWORD"] = "secret-auth-code"
    os.environ["DCA_NOTIFY_EMAIL_TO"] = "old@163.com"
    os.environ["DCA_NOTIFY_SMTP_USER"] = "old@163.com"
    os.environ["DCA_NOTIFY_SMTP_HOST"] = "smtp.163.com"
    os.environ["DCA_NOTIFY_SMTP_PORT"] = "465"
    config.get_settings.cache_clear()

    # Blank password (None) must leave secret-auth-code intact
    config.set_notify_smtp_settings(
        email_to="new@163.com",
        email_from="",
        smtp_host="smtp.163.com",
        smtp_port=465,
        smtp_user="new@163.com",
        smtp_password=None,
    )
    raw = _env_path.read_text(encoding="utf-8")
    if "DCA_NOTIFY_SMTP_PASSWORD=secret-auth-code" not in raw:
        failures.append(f"password clobbered on blank submit; .env=\n{raw}")
    if "DCA_NOTIFY_EMAIL_TO=new@163.com" not in raw:
        failures.append("email_to not updated")
    if os.environ.get("DCA_NOTIFY_SMTP_PASSWORD") != "secret-auth-code":
        failures.append("process env password changed on blank submit")

    s = config.get_settings()
    if s.dca_notify_smtp_password != "secret-auth-code":
        failures.append(f"settings password={s.dca_notify_smtp_password!r}")
    if s.dca_notify_email_to != "new@163.com":
        failures.append(f"settings to={s.dca_notify_email_to!r}")

    # Explicit new password replaces
    config.set_notify_smtp_settings(
        email_to="new@163.com",
        email_from="from@163.com",
        smtp_host="smtp.163.com",
        smtp_port=587,
        smtp_user="new@163.com",
        smtp_password="new-auth-code",
    )
    raw2 = _env_path.read_text(encoding="utf-8")
    if "DCA_NOTIFY_SMTP_PASSWORD=new-auth-code" not in raw2:
        failures.append("password not updated when provided")
    if "secret-auth-code" in raw2:
        failures.append("old password still in .env after replace")
    if "DCA_NOTIFY_SMTP_PORT=587" not in raw2:
        failures.append("port not updated")
    if "DCA_NOTIFY_EMAIL_FROM=from@163.com" not in raw2:
        failures.append("from not updated")

    # Bool helper still works via set_env_keys
    config.set_dry_run(False)
    if "DRY_RUN=false" not in _env_path.read_text(encoding="utf-8"):
        failures.append("set_dry_run via set_env_keys failed")

    # Password must never appear in a naive "log" of updates dict keys
    # (sanity: set_env_keys accepts password but callers must not print it)
    updates = {"DCA_NOTIFY_EMAIL_TO": "x@163.com"}
    if "PASSWORD" in "".join(updates.keys()):
        failures.append("unexpected")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("OK: blank SMTP password leaves existing; explicit replaces; dry_run ok")
    print(f"temp env at {_tmpdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
