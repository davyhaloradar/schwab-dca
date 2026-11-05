"""Durable DCA due / catch-up helpers (timezone-aware, pure).

Holiday policy (documented):
  Prefer SKIP + ADVANCE calendar slot — if the scheduled wall-clock slot falls on a
  non-US-equity trading day (weekend already excluded for daily/weekly Mon–Fri
  semantics; NYSE holiday for weekday slots), we do NOT roll the buy to the next
  trading day. Instead the tick advances last_run_at past that calendar slot with
  no order, so buys never stack after holidays.

Catch-up policy:
  If the process was down across one or more slots, fire ONCE for the latest missed
  due slot only (no multi-fire of every missed occurrence).
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from app.services.market_calendar import is_us_equity_trading_day

ET = ZoneInfo("America/New_York")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    parts = (hhmm or "10:00").split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return max(0, min(23, hour)), max(0, min(59, minute))


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "America/New_York")
    except Exception:
        return ET


def parse_last_run_at(value: str | None) -> dt.datetime | None:
    """Parse ISO last_run_at to aware UTC datetime, or None if never run."""
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _at_local(d: dt.date, hour: int, minute: int, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)


def _iter_candidate_dates_back(
    *,
    schedule_type: str,
    weekday: int,
    day_of_month: int,
    now_local: dt.datetime,
    limit: int = 400,
) -> list[dt.date]:
    """Recent candidate calendar dates (newest first) matching the schedule shape."""
    st = (schedule_type or "daily").lower()
    if st == "cron":
        st = "daily"
    today = now_local.date()
    out: list[dt.date] = []
    if st == "weekly":
        wd = max(0, min(6, int(weekday or 0)))
        d = today
        for _ in range(limit):
            if d.weekday() == wd:
                out.append(d)
            d -= dt.timedelta(days=1)
        return out
    if st == "monthly":
        dom = max(1, min(28, int(day_of_month or 1)))
        y, m = today.year, today.month
        for _ in range(limit // 28 + 2):
            try:
                candidate = dt.date(y, m, dom)
            except ValueError:
                candidate = None
            if candidate is not None and candidate <= today:
                out.append(candidate)
            m -= 1
            if m < 1:
                m = 12
                y -= 1
        return out
    # daily: Mon–Fri only (matches prior CronTrigger day_of_week=mon-fri)
    d = today
    for _ in range(limit):
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def latest_slot_at_or_before(
    *,
    schedule_type: str,
    time_hhmm: str,
    timezone: str,
    weekday: int = 0,
    day_of_month: int = 1,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    """Most recent schedule wall-clock slot <= now (in plan timezone), as aware dt."""
    tz = _tz(timezone)
    if now is None:
        now = dt.datetime.now(tz=dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now_local = now.astimezone(tz)
    hour, minute = _parse_hhmm(time_hhmm)
    for d in _iter_candidate_dates_back(
        schedule_type=schedule_type,
        weekday=weekday,
        day_of_month=day_of_month,
        now_local=now_local,
    ):
        slot = _at_local(d, hour, minute, tz)
        if slot <= now_local:
            return slot
    return None


def next_due_after(
    *,
    schedule_type: str,
    time_hhmm: str,
    timezone: str,
    weekday: int = 0,
    day_of_month: int = 1,
    after: dt.datetime | None = None,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    """Next schedule wall-clock slot strictly after `after` (default: now)."""
    tz = _tz(timezone)
    if now is None:
        now = dt.datetime.now(tz=dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    if after is None:
        after = now
    elif after.tzinfo is None:
        after = after.replace(tzinfo=dt.timezone.utc)
    after_local = after.astimezone(tz)
    hour, minute = _parse_hhmm(time_hhmm)
    st = (schedule_type or "daily").lower()
    if st == "cron":
        st = "daily"
    # Walk forward up to ~14 months of days
    d = after_local.date()
    for _ in range(450):
        ok = False
        if st == "weekly":
            ok = d.weekday() == max(0, min(6, int(weekday or 0)))
        elif st == "monthly":
            ok = d.day == max(1, min(28, int(day_of_month or 1)))
        else:
            ok = d.weekday() < 5
        if ok:
            slot = _at_local(d, hour, minute, tz)
            if slot > after_local:
                return slot
        d += dt.timedelta(days=1)
    return None


def due_status(
    plan: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """
    Compute due state for a plan.

    Returns dict with:
      slot: latest calendar slot <= now (aware) or None
      is_due: True if we should fire a buy for that slot now
      skip_advance: True if slot is past but non-trading — advance last_run_at, no buy
      next_due_at: next future slot (may still land on holiday; tick will skip+advance)
      reason: short explanation
    """
    if now is None:
        now = dt.datetime.now(tz=dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    slot = latest_slot_at_or_before(
        schedule_type=plan.get("schedule_type") or "daily",
        time_hhmm=plan.get("time_hhmm") or "10:00",
        timezone=plan.get("timezone") or "America/New_York",
        weekday=int(plan.get("weekday") or 0),
        day_of_month=int(plan.get("day_of_month") or 1),
        now=now,
    )
    last = parse_last_run_at(plan.get("last_run_at"))
    nxt = next_due_after(
        schedule_type=plan.get("schedule_type") or "daily",
        time_hhmm=plan.get("time_hhmm") or "10:00",
        timezone=plan.get("timezone") or "America/New_York",
        weekday=int(plan.get("weekday") or 0),
        day_of_month=int(plan.get("day_of_month") or 1),
        after=now,
        now=now,
    )

    if slot is None:
        return {
            "slot": None,
            "is_due": False,
            "skip_advance": False,
            "next_due_at": nxt,
            "reason": "no_slot",
        }

    slot_utc = slot.astimezone(dt.timezone.utc)
    # Already consumed this (or a later) slot
    if last is not None and last >= slot_utc:
        return {
            "slot": slot,
            "is_due": False,
            "skip_advance": False,
            "next_due_at": nxt,
            "reason": "already_run",
        }

    # Holiday / closed: skip buy, advance past calendar slot (no stacking)
    if not is_us_equity_trading_day(slot):
        return {
            "slot": slot,
            "is_due": False,
            "skip_advance": True,
            "next_due_at": nxt,
            "reason": "non_trading_day_skip_advance",
        }

    return {
        "slot": slot,
        "is_due": True,
        "skip_advance": False,
        "next_due_at": nxt,
        "reason": "due",
    }


def is_due(plan: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    return bool(due_status(plan, now=now).get("is_due"))
