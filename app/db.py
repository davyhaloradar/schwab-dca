"""SQLite persistence for tokens, DCA plans, and run logs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings
from app import crypto_util

NY_HINT = "America/New_York"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token_enc TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                access_expires_at TEXT,
                refresh_expires_at TEXT,
                scope TEXT,
                token_type TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dca_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                symbols TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'weekly',
                weekday INTEGER NOT NULL DEFAULT 0,
                day_of_month INTEGER NOT NULL DEFAULT 1,
                time_hhmm TEXT NOT NULL DEFAULT '10:00',
                timezone TEXT NOT NULL DEFAULT 'America/New_York',
                cron_expr TEXT,
                account_hash TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                plan_name TEXT,
                dry_run INTEGER NOT NULL,
                payload_json TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                -- Soft-keep: deleting a plan nulls plan_id; plan_name + payload remain for audit
                FOREIGN KEY (plan_id) REFERENCES dca_plans(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS t_engine (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                running INTEGER NOT NULL DEFAULT 0,
                symbols TEXT NOT NULL,
                notional_usd REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS t_symbol_state (
                symbol TEXT NOT NULL,
                session_date TEXT NOT NULL,
                open_entry_ts TEXT,
                open_entry_px REAL,
                open_qty INTEGER,
                last_bar_ts TEXT,
                flattened INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, session_date)
            );

            CREATE TABLE IF NOT EXISTS t_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                pnl REAL,
                reason TEXT NOT NULL,
                price_source TEXT NOT NULL DEFAULT 'trigger',
                bar_ts TEXT NOT NULL,
                dry_run INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (session_date, symbol, bar_ts, side)
            );

            CREATE TABLE IF NOT EXISTS t_bars (
                symbol TEXT NOT NULL,
                bar_ts TEXT NOT NULL,
                session_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (symbol, bar_ts)
            );

            CREATE INDEX IF NOT EXISTS idx_t_bars_symbol_session
                ON t_bars (symbol, session_date);
            """
        )
    _ensure_schema()


# --- OAuth tokens ---

def save_tokens(
    access_token: str,
    refresh_token: str,
    *,
    access_expires_at: str | None,
    refresh_expires_at: str | None,
    scope: str | None = None,
    token_type: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO oauth_tokens (
                id, access_token_enc, refresh_token_enc,
                access_expires_at, refresh_expires_at, scope, token_type, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token_enc = excluded.access_token_enc,
                refresh_token_enc = excluded.refresh_token_enc,
                access_expires_at = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                scope = excluded.scope,
                token_type = excluded.token_type,
                updated_at = excluded.updated_at
            """,
            (
                crypto_util.encrypt(access_token),
                crypto_util.encrypt(refresh_token),
                access_expires_at,
                refresh_expires_at,
                scope,
                token_type,
                _utcnow(),
            ),
        )


def get_tokens() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM oauth_tokens WHERE id = 1").fetchone()
    if not row:
        return None
    return {
        "access_token": crypto_util.decrypt(row["access_token_enc"]),
        "refresh_token": crypto_util.decrypt(row["refresh_token_enc"]),
        "access_expires_at": row["access_expires_at"],
        "refresh_expires_at": row["refresh_expires_at"],
        "scope": row["scope"],
        "token_type": row["token_type"],
        "updated_at": row["updated_at"],
    }


def clear_tokens() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM oauth_tokens WHERE id = 1")




def _ensure_schema() -> None:
    with connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dca_plans)").fetchall()}
        if "shares_per_symbol" not in cols:
            conn.execute(
                "ALTER TABLE dca_plans ADD COLUMN shares_per_symbol INTEGER NOT NULL DEFAULT 0"
            )
        # Feature 1: durable due / catch-up. NULL = never run; first fire on next schedule hit.
        if "last_run_at" not in cols:
            conn.execute("ALTER TABLE dca_plans ADD COLUMN last_run_at TEXT")
        # Optional cache of next due (informational; tick recomputes from schedule + last_run_at)
        if "next_due_at" not in cols:
            conn.execute("ALTER TABLE dca_plans ADD COLUMN next_due_at TEXT")


# --- DCA plans ---

def _plan_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "symbols": [s.strip().upper() for s in row["symbols"].split(",") if s.strip()],
        "symbols_raw": row["symbols"],
        "amount_usd": row["amount_usd"],
        "shares_per_symbol": int(row["shares_per_symbol"] if "shares_per_symbol" in row.keys() else 0),
        "schedule_type": row["schedule_type"],
        "weekday": row["weekday"],
        "day_of_month": row["day_of_month"],
        "time_hhmm": row["time_hhmm"],
        "timezone": row["timezone"],
        "cron_expr": row["cron_expr"],
        "account_hash": row["account_hash"],
        "enabled": bool(row["enabled"]),
        "last_run_at": row["last_run_at"] if "last_run_at" in row.keys() else None,
        "next_due_at": row["next_due_at"] if "next_due_at" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_plans() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM dca_plans ORDER BY id").fetchall()
    return [_plan_from_row(r) for r in rows]


def get_plan(plan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM dca_plans WHERE id = ?", (plan_id,)).fetchone()
    return _plan_from_row(row) if row else None


def create_plan(data: dict[str, Any]) -> int:
    now = _utcnow()
    from app.plan_rules import parse_symbols, symbols_csv

    symbols = symbols_csv(parse_symbols(data.get("symbols")))
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO dca_plans (
                name, symbols, amount_usd, shares_per_symbol, schedule_type, weekday, day_of_month,
                time_hhmm, timezone, cron_expr, account_hash, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"],
                symbols,
                float(data.get("amount_usd") or 0),
                int(data.get("shares_per_symbol") or 0),
                data.get("schedule_type", "weekly"),
                int(data.get("weekday", 0)),
                int(data.get("day_of_month", 1)),
                data.get("time_hhmm", "10:00"),
                data.get("timezone", NY_HINT),
                data.get("cron_expr") or None,
                data.get("account_hash") or None,
                1 if data.get("enabled", True) else 0,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_plan(plan_id: int, data: dict[str, Any]) -> None:
    """Update mutable plan fields. symbols + account_hash are immutable after create."""
    with connect() as conn:
        row = conn.execute(
            "SELECT symbols, account_hash FROM dca_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if not row:
            return
        # Defense in depth: symbols always immutable; account_hash immutable once set
        # (empty → non-empty allowed once so older plans can lock in a hash for LIVE)
        symbols = row["symbols"]
        existing_hash = (row["account_hash"] or "").strip() or None
        incoming = data.get("account_hash")
        if isinstance(incoming, str):
            incoming = incoming.strip() or None
        if existing_hash:
            account_hash = existing_hash
        else:
            account_hash = incoming
        conn.execute(
            """
            UPDATE dca_plans SET
                name = ?, symbols = ?, amount_usd = ?, shares_per_symbol = ?, schedule_type = ?,
                weekday = ?, day_of_month = ?, time_hhmm = ?, timezone = ?,
                cron_expr = ?, account_hash = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                data["name"],
                symbols,
                float(data.get("amount_usd") or 0),
                int(data.get("shares_per_symbol") or 0),
                data.get("schedule_type", "weekly"),
                int(data.get("weekday", 0)),
                int(data.get("day_of_month", 1)),
                data.get("time_hhmm", "10:00"),
                data.get("timezone", NY_HINT),
                data.get("cron_expr") or None,
                account_hash,
                1 if data.get("enabled", True) else 0,
                _utcnow(),
                plan_id,
            ),
        )


def set_account_hash_if_empty(plan_id: int, account_hash: str) -> bool:
    """One-way fill: set account_hash only when currently empty. Returns True if updated."""
    h = (account_hash or "").strip()
    if not h:
        return False
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE dca_plans
            SET account_hash = ?, updated_at = ?
            WHERE id = ?
              AND (account_hash IS NULL OR TRIM(account_hash) = '')
            """,
            (h, _utcnow(), plan_id),
        )
        return cur.rowcount > 0


def set_plan_enabled(plan_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE dca_plans SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _utcnow(), plan_id),
        )


def delete_plan(plan_id: int) -> None:
    """Delete plan; run_logs keep rows with plan_id set NULL (FK ON DELETE SET NULL)."""
    with connect() as conn:
        conn.execute("DELETE FROM dca_plans WHERE id = ?", (plan_id,))



def set_plan_last_run_at(
    plan_id: int,
    last_run_at: str,
    *,
    next_due_at: str | None = None,
) -> None:
    """Advance durable due cursor after a successful execute or holiday skip+advance."""
    with connect() as conn:
        if next_due_at is None:
            conn.execute(
                "UPDATE dca_plans SET last_run_at = ?, updated_at = ? WHERE id = ?",
                (last_run_at, _utcnow(), plan_id),
            )
        else:
            conn.execute(
                "UPDATE dca_plans SET last_run_at = ?, next_due_at = ?, updated_at = ? WHERE id = ?",
                (last_run_at, next_due_at, _utcnow(), plan_id),
            )


def set_plan_next_due_at(plan_id: int, next_due_at: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE dca_plans SET next_due_at = ?, updated_at = ? WHERE id = ?",
            (next_due_at, _utcnow(), plan_id),
        )


def live_notional_submitted_today_et() -> float:
    """Sum estimated LIVE notionals from today's (America/New_York) successful-ish run logs.

    Best-effort from payload_json intended_orders; used for DCA_MAX_DAILY_NOTIONAL_USD.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(tz=et)
    day_start = datetime(now_et.year, now_et.month, now_et.day, tzinfo=et).astimezone(timezone.utc)
    start_iso = day_start.isoformat()
    total = 0.0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT payload_json, result_json, error FROM run_logs
            WHERE dry_run = 0 AND created_at >= ?
            ORDER BY id DESC LIMIT 5000
            """,
            (start_iso,),
        ).fetchall()
    for r in rows:
        err = (r["error"] or "")
        # Skip pure policy/cash skips that never placed
        if err.startswith("skipped_") or "insufficient_cash" in err or "policy_reject" in err:
            continue
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except Exception:
            continue
        for intent in payload.get("intended_orders") or []:
            if intent.get("skipped"):
                continue
            qty = int(intent.get("quantity") or 0)
            price = float(intent.get("price_used") or 0)
            if qty >= 1 and price > 0:
                total += qty * price
            else:
                total += float(intent.get("amount_usd_allocated") or 0)
    return round(total, 2)


# --- Run logs ---


def add_run_log(
    *,
    plan_id: int | None,
    plan_name: str | None,
    dry_run: bool,
    payload: Any,
    result: Any = None,
    error: str | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO run_logs (plan_id, plan_name, dry_run, payload_json, result_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                plan_name,
                1 if dry_run else 0,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                error,
                _utcnow(),
            ),
        )
        return int(cur.lastrowid)


def count_run_logs() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM run_logs").fetchone()
    return int(row["c"] if row else 0)


def list_run_logs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "plan_id": r["plan_id"],
                "plan_name": r["plan_name"],
                "dry_run": bool(r["dry_run"]),
                "payload_json": r["payload_json"],
                "result_json": r["result_json"],
                "error": r["error"],
                "created_at": r["created_at"],
            }
        )
    return out


def get_run_log(log_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM run_logs WHERE id = ?", (log_id,)).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "plan_id": r["plan_id"],
        "plan_name": r["plan_name"],
        "dry_run": bool(r["dry_run"]),
        "payload_json": r["payload_json"],
        "result_json": r["result_json"],
        "error": r["error"],
        "created_at": r["created_at"],
    }


def update_run_log_result(log_id: int, result: Any, error: str | None = None) -> None:
    with connect() as conn:
        if error is None:
            conn.execute(
                "UPDATE run_logs SET result_json = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), log_id),
            )
        else:
            conn.execute(
                "UPDATE run_logs SET result_json = ?, error = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), error, log_id),
            )


# --- 5m VWAP 做T monitor ---


def _t_fill_from_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "session_date": r["session_date"],
        "symbol": r["symbol"],
        "side": r["side"],
        "qty": int(r["qty"]),
        "price": float(r["price"]),
        "pnl": None if r["pnl"] is None else float(r["pnl"]),
        "reason": r["reason"],
        "price_source": r["price_source"] or "trigger",
        "bar_ts": r["bar_ts"],
        "dry_run": bool(r["dry_run"]),
        "created_at": r["created_at"],
    }


def get_t_config() -> dict[str, Any]:
    """Singleton engine row. Created from settings the first time."""
    settings = get_settings()
    default_symbols = (settings.t_default_symbols or "QQQM,AAPL").strip() or "QQQM,AAPL"
    try:
        default_notional = float(settings.t_notional_usd or 2000.0)
    except (TypeError, ValueError):
        default_notional = 2000.0
    with connect() as conn:
        row = conn.execute("SELECT * FROM t_engine WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO t_engine (id, running, symbols, notional_usd, updated_at)
                VALUES (1, 0, ?, ?, ?)
                """,
                (default_symbols, default_notional, _utcnow()),
            )
            row = conn.execute("SELECT * FROM t_engine WHERE id = 1").fetchone()
    return {
        "running": bool(row["running"]),
        "symbols": [s.strip().upper() for s in (row["symbols"] or "").split(",") if s.strip()],
        "symbols_raw": row["symbols"],
        "notional_usd": float(row["notional_usd"]),
        "updated_at": row["updated_at"],
    }


def set_t_running(running: bool) -> None:
    get_t_config()
    with connect() as conn:
        conn.execute(
            "UPDATE t_engine SET running = ?, updated_at = ? WHERE id = 1",
            (1 if running else 0, _utcnow()),
        )


def set_t_settings(symbols_csv: str, notional_usd: float) -> None:
    get_t_config()
    with connect() as conn:
        conn.execute(
            "UPDATE t_engine SET symbols = ?, notional_usd = ?, updated_at = ? WHERE id = 1",
            (symbols_csv, float(notional_usd), _utcnow()),
        )


def get_t_state(symbol: str, session_date: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM t_symbol_state WHERE symbol = ? AND session_date = ?",
            (symbol.upper(), session_date),
        ).fetchone()
    if not row:
        return None
    qty = row["open_qty"]
    return {
        "symbol": row["symbol"],
        "session_date": row["session_date"],
        "open_entry_ts": row["open_entry_ts"],
        "open_entry_px": None if row["open_entry_px"] is None else float(row["open_entry_px"]),
        "open_qty": None if qty is None else int(qty),
        "last_bar_ts": row["last_bar_ts"],
        "flattened": bool(row["flattened"]),
    }


def upsert_t_state(
    *,
    symbol: str,
    session_date: str,
    open_entry_ts: str | None,
    open_entry_px: float | None,
    open_qty: int | None,
    last_bar_ts: str | None,
    flattened: bool,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO t_symbol_state (
                symbol, session_date, open_entry_ts, open_entry_px, open_qty,
                last_bar_ts, flattened
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, session_date) DO UPDATE SET
                open_entry_ts = excluded.open_entry_ts,
                open_entry_px = excluded.open_entry_px,
                open_qty = excluded.open_qty,
                last_bar_ts = excluded.last_bar_ts,
                flattened = excluded.flattened
            """,
            (
                symbol.upper(),
                session_date,
                open_entry_ts,
                open_entry_px,
                open_qty,
                last_bar_ts,
                1 if flattened else 0,
            ),
        )


def insert_t_fill(
    *,
    session_date: str,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    pnl: float | None,
    reason: str,
    price_source: str,
    bar_ts: str,
) -> bool:
    """Insert one simulated fill. False if this bar+side was already stored."""
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO t_fills (
                    session_date, symbol, side, qty, price, pnl, reason,
                    price_source, bar_ts, dry_run, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    session_date,
                    symbol.upper(),
                    side,
                    int(qty),
                    float(price),
                    pnl,
                    reason,
                    price_source or "trigger",
                    bar_ts,
                    _utcnow(),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def list_t_fills(session_date: str, *, limit: int = 500) -> list[dict[str, Any]]:
    limit = max(1, min(2000, int(limit)))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM t_fills
            WHERE session_date = ?
            ORDER BY bar_ts ASC, id ASC
            LIMIT ?
            """,
            (session_date, limit),
        ).fetchall()
    return [_t_fill_from_row(r) for r in rows]


def delete_t_fills(session_date: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM t_fills WHERE session_date = ?", (session_date,))


def delete_t_fills_for_symbol(symbol: str) -> int:
    """Delete every simulated fill for one symbol, any session date."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM t_fills WHERE symbol = ?", (symbol.upper(),))
        return int(cur.rowcount or 0)


def delete_t_state_for_symbol(symbol: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM t_symbol_state WHERE symbol = ?", (symbol.upper(),))


def list_live_run_logs(*, plan_id: int | None = None, limit: int = 5000) -> list[dict[str, Any]]:
    """Non-dry-run run logs (optionally for one plan), newest first."""
    with connect() as conn:
        if plan_id is None:
            rows = conn.execute(
                "SELECT * FROM run_logs WHERE dry_run = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_logs WHERE dry_run = 0 AND plan_id = ? ORDER BY id DESC LIMIT ?",
                (plan_id, limit),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "plan_id": r["plan_id"],
                "plan_name": r["plan_name"],
                "dry_run": bool(r["dry_run"]),
                "payload_json": r["payload_json"],
                "result_json": r["result_json"],
                "error": r["error"],
                "created_at": r["created_at"],
            }
        )
    return out
