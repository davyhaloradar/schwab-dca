"""US equity regular-session trading day checks (NYSE calendar)."""
from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Fixed-date / observed NYSE holidays are approximated; weekends always closed.
# Prefer exchange_calendars when installed.
try:
    import exchange_calendars as xcals  # type: ignore

    _XNYS = xcals.get_calendar("XNYS")
except Exception:  # noqa: BLE001
    _XNYS = None
    logger.info("exchange_calendars not available; using weekend + holiday set fallback")


def _fallback_holidays(year: int) -> set[dt.date]:
    """Common NYSE full-day closures (observed). Good enough for DCA skip."""
    from datetime import date, timedelta

    def observed(d: date) -> date:
        if d.weekday() == 5:  # Sat → Fri
            return d - timedelta(days=1)
        if d.weekday() == 6:  # Sun → Mon
            return d + timedelta(days=1)
        return d

    # nth weekday helper
    def nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
        d = date(y, month, 1)
        count = 0
        while True:
            if d.weekday() == weekday:
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)

    def last_weekday(y: int, month: int, weekday: int) -> date:
        if month == 12:
            d = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            d = date(y, month + 1, 1) - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d

    hol = {
        observed(date(year, 1, 1)),  # New Year
        nth_weekday(year, 1, 0, 3),  # MLK
        nth_weekday(year, 2, 0, 3),  # Presidents
        # Good Friday — approximate via easter
        _good_friday(year),
        last_weekday(year, 5, 0),  # Memorial
        observed(date(year, 6, 19)),  # Juneteenth
        observed(date(year, 7, 4)),  # Independence
        nth_weekday(year, 9, 0, 1),  # Labor
        nth_weekday(year, 11, 3, 4),  # Thanksgiving
        observed(date(year, 12, 25)),  # Christmas
    }
    return hol


def _good_friday(year: int) -> dt.date:
    # Anonymous Gregorian algorithm → Easter Sunday, then -2
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = dt.date(year, month, day)
    return easter - dt.timedelta(days=2)


@lru_cache(maxsize=8)
def _holiday_set(year: int) -> frozenset[dt.date]:
    return frozenset(_fallback_holidays(year))


def is_us_equity_trading_day(when: dt.datetime | None = None) -> bool:
    """True if NYSE is open for a regular session on that calendar date (ET)."""
    if when is None:
        when = dt.datetime.now(tz=ET)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=ET)
    else:
        when = when.astimezone(ET)
    d = when.date()

    if _XNYS is not None:
        try:
            return bool(_XNYS.is_session(d))
        except Exception:  # noqa: BLE001
            logger.exception("XNYS calendar check failed; falling back")

    if d.weekday() >= 5:
        return False
    return d not in _holiday_set(d.year)
