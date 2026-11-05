#!/usr/bin/env python3
"""5-minute VWAP 做T monitor: signal rules, persistence, CSRF, page.

Run from repo root:
  python scripts/test_vwap_mr.py
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmpdir = tempfile.mkdtemp(prefix="dca-vwap-t-")
os.environ["DATABASE_PATH"] = str(Path(_tmpdir) / "app.db")
os.environ["TOKEN_KEY_FILE"] = str(Path(_tmpdir) / "token.key")
os.environ["DRY_RUN"] = "true"
os.environ["APP_ROOT_PATH"] = "/plan"
os.environ["T_TICK_SEC"] = "60"
os.environ["T_DEFAULT_SYMBOLS"] = "QQQM,AAPL"
os.environ["T_NOTIONAL_USD"] = "2000"
os.environ["T_LIVE_ORDERS"] = "false"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app import db  # noqa: E402
from app.services import t_engine  # noqa: E402
from app.services.vwap_mr import (  # noqa: E402
    ET,
    Intrabar,
    SymbolParams,
    chart_time,
    is_rth_bar,
    level_prices,
    on_closed_bar,
    params_for,
    whole_shares,
    with_session_vwap,
)

_PARAMS = SymbolParams(100.0, 5.0, 100.0, 2000.0, "test")


def _bar(hm: str, o: float, h: float, low: float, c: float, vol: float = 1000.0, day: str = "2026-10-30") -> Intrabar:
    hour, minute = (int(x) for x in hm.split(":"))
    y, m, d = (int(x) for x in day.split("-"))
    start = datetime(y, m, d, hour, minute, tzinfo=ET)
    return Intrabar(start, o, h, low, c, vol)


class VwapRulesTest(unittest.TestCase):
    def test_session_vwap_typical_price(self) -> None:
        bars = [
            _bar("10:00", 10, 10, 10, 10, 100),
            _bar("10:05", 12, 13, 11, 12, 100),
            _bar("10:10", 12, 13, 11, 12, 0),
        ]
        priced = with_session_vwap(bars)
        self.assertAlmostEqual(priced[0][1], 10.0)
        self.assertAlmostEqual(priced[1][1], 11.0)
        self.assertAlmostEqual(priced[2][1], 11.0)

    def test_backtest_params(self) -> None:
        qqqm = params_for("QQQM", 2000)
        aapl = params_for("AAPL", 2000)
        other = params_for("ZZZZ", 2000)
        self.assertAlmostEqual(qqqm.entry_bps, 31.306446067215525)
        self.assertAlmostEqual(qqqm.exit_bps, 5.0)
        self.assertAlmostEqual(qqqm.stop_bps, 62.61289213443105)
        self.assertAlmostEqual(aapl.entry_bps, 48.552878033309874)
        self.assertEqual(aapl.param_source, "backtest")
        self.assertEqual(other.param_source, "qqqm_default")
        self.assertAlmostEqual(other.entry_bps, qqqm.entry_bps)
        self.assertEqual(whole_shares(295, 2000), 6)
        self.assertEqual(whole_shares(2500, 2000), 0)

    def test_vwap_levels_follow_bps(self) -> None:
        aapl = params_for("AAPL", 2000)
        levels = level_prices(349.53, aapl)
        self.assertAlmostEqual(levels["entry"], 349.53 * (1 - aapl.entry_bps / 10000))
        self.assertAlmostEqual(levels["exit"], 349.53 * (1 - 5 / 10000))
        self.assertAlmostEqual(levels["stop"], 349.53 * (1 - aapl.stop_bps / 10000))
        self.assertLess(levels["stop"], levels["entry"])
        self.assertLess(levels["entry"], levels["exit"])
        self.assertLess(levels["exit"], 349.53)
        # AAPL stop is about 97.1 bps under VWAP.
        self.assertAlmostEqual((349.53 - levels["stop"]) / 349.53 * 10000, 97.1, delta=0.1)

    def test_group_sessions_keeps_each_day(self) -> None:
        from app.services.t_market import group_sessions

        bars = [
            _bar("10:00", 1, 1, 1, 1, day="2026-10-29"),
            _bar("10:05", 1, 1, 1, 1, day="2026-10-29"),
            _bar("10:00", 2, 2, 2, 2, day="2026-10-30"),
        ]
        grouped = group_sessions(bars)
        self.assertEqual(sorted(grouped), ["2026-10-29", "2026-10-30"])
        self.assertEqual(len(grouped["2026-10-29"]), 2)
        self.assertEqual(len(grouped["2026-10-30"]), 1)

    def test_rth_and_chart_clock(self) -> None:
        self.assertTrue(is_rth_bar(_bar("09:30", 1, 1, 1, 1).start))
        self.assertTrue(is_rth_bar(_bar("15:55", 1, 1, 1, 1).start))
        self.assertFalse(is_rth_bar(_bar("09:25", 1, 1, 1, 1).start))
        self.assertFalse(is_rth_bar(_bar("16:00", 1, 1, 1, 1).start))
        self.assertFalse(is_rth_bar(_bar("10:00", 1, 1, 1, 1, day="2026-10-31").start))  # Saturday
        et = datetime(2026, 10, 30, 9, 30, tzinfo=ET)
        self.assertEqual(chart_time(et), int(datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc).timestamp()))

    def test_entry_exit_stop_and_flat(self) -> None:
        quiet = _bar("10:00", 100, 100.2, 99.9, 100)
        pos, fill, flat = on_closed_bar(quiet, 100.0, None, _PARAMS, day_flattened=False)
        self.assertIsNone(fill)
        self.assertFalse(flat)

        dip = _bar("10:05", 100, 100, 98.5, 99)
        pos, fill, flat = on_closed_bar(dip, 100.0, None, _PARAMS, day_flattened=False)
        self.assertIsNotNone(fill)
        assert fill is not None and pos is not None
        self.assertEqual(fill.side, "BUY")
        self.assertEqual(fill.reason, "entry_vwap")
        self.assertAlmostEqual(fill.price, 99.0)
        self.assertEqual(fill.qty, 20)
        self.assertEqual(fill.price_source, "trigger")

        gap = _bar("10:10", 98, 99, 97.5, 98.2)
        _, gap_fill, _ = on_closed_bar(gap, 100.0, None, _PARAMS, day_flattened=False)
        assert gap_fill is not None
        self.assertEqual(gap_fill.reason, "entry_gap")
        self.assertAlmostEqual(gap_fill.price, 98.0)

        # Still below VWAP and above the stop: one open T, no second fill.
        hold = _bar("10:12", 99.2, 99.4, 99.1, 99.3)
        held, again, _ = on_closed_bar(hold, 100.0, pos, _PARAMS, day_flattened=False)
        self.assertIsNone(again)
        self.assertEqual(held.qty, pos.qty)

        rally = _bar("10:15", 99.2, 100.4, 99.2, 100.1)
        flat_pos, sold, did_flat = on_closed_bar(rally, 100.0, pos, _PARAMS, day_flattened=False)
        self.assertIsNone(flat_pos)
        assert sold is not None
        self.assertEqual(sold.side, "SELL")
        self.assertEqual(sold.reason, "mr_vwap")
        self.assertAlmostEqual(sold.price, 100.0)
        self.assertAlmostEqual(sold.pnl, (100.0 - 99.0) * 20)
        self.assertFalse(did_flat)

        # stop is 100 bps under the 99 fill → 98.01; high also tags VWAP, so stop wins.
        same = _bar("10:20", 99.5, 100.5, 97.5, 99.4)
        _, stopped, _ = on_closed_bar(same, 100.0, pos, _PARAMS, day_flattened=False)
        assert stopped is not None
        self.assertEqual(stopped.reason, "stop_samebar")
        self.assertAlmostEqual(stopped.price, 99.0 * (1 - 100 / 10000))

        only_stop = _bar("10:25", 99.5, 99.4, 97.5, 99.0)
        _, stopped2, _ = on_closed_bar(only_stop, 100.0, pos, _PARAMS, day_flattened=False)
        assert stopped2 is not None
        self.assertEqual(stopped2.reason, "stop")
        self.assertAlmostEqual(stopped2.price, 99.0 * (1 - 100 / 10000))

        eod_bar = _bar("15:50", 100.2, 100.2, 100.2, 100.2)
        _, eod, did_flat = on_closed_bar(eod_bar, 101.0, pos, _PARAMS, day_flattened=False)
        assert eod is not None
        self.assertEqual(eod.reason, "eod")
        self.assertAlmostEqual(eod.price, 100.2)
        self.assertTrue(did_flat)

        late = _bar("15:55", 90, 90, 80, 85)
        _, no_reentry, still = on_closed_bar(late, 100.0, None, _PARAMS, day_flattened=True)
        self.assertIsNone(no_reentry)
        self.assertTrue(still)

        _, no_entry, blocked = on_closed_bar(late, 100.0, None, _PARAMS, day_flattened=False)
        self.assertIsNone(no_entry)
        self.assertTrue(blocked)

        live, live_fill, _ = on_closed_bar(
            dip, 100.0, None, _PARAMS, day_flattened=False, exec_px=98.5, exec_source="mid"
        )
        assert live_fill is not None and live is not None
        self.assertAlmostEqual(live_fill.price, 98.5)
        self.assertEqual(live_fill.price_source, "mid")
        self.assertEqual(live.qty, int(2000 // 98.5))

    def test_price_above_notional_skips(self) -> None:
        pricey = SymbolParams(100.0, 5.0, 100.0, 50.0, "test")
        dip = _bar("10:05", 100, 100, 98.5, 99)
        pos, fill, _ = on_closed_bar(dip, 100.0, None, pricey, day_flattened=False)
        self.assertIsNone(pos)
        self.assertIsNone(fill)


class EnginePersistTest(unittest.TestCase):
    def setUp(self) -> None:
        db.init_db()
        with db.connect() as conn:
            conn.execute("DELETE FROM t_fills")
            conn.execute("DELETE FROM t_symbol_state")
            conn.execute("DELETE FROM t_engine")
            conn.execute("DELETE FROM t_bars")

    def _session_bars(self) -> list[Intrabar]:
        bars = []
        start = datetime(2026, 10, 30, 10, 0, tzinfo=ET)
        for i in range(8):
            ts = start + timedelta(minutes=5 * i)
            hm = ts.strftime("%H:%M")
            bars.append(_bar(hm, 100, 100.1, 99.95, 100))
        bars.append(_bar("10:40", 100, 100, 99.0, 99.4))
        bars.append(_bar("10:45", 99.8, 101.0, 99.7, 100.4))
        return bars

    def test_replay_is_idempotent_and_reset_skips_history(self) -> None:
        bars = self._session_bars()
        now = datetime(2026, 10, 30, 16, 5, tzinfo=ET)
        t_engine.process_closed_bars("QQQM", "2026-10-30", bars, now=now)
        fills = db.list_t_fills("2026-10-30")
        self.assertGreaterEqual(len(fills), 2)
        self.assertEqual(fills[0]["side"], "BUY")
        self.assertEqual(fills[0]["symbol"], "QQQM")
        self.assertTrue(fills[0]["dry_run"])
        self.assertEqual(fills[1]["side"], "SELL")
        self.assertIsNotNone(fills[1]["pnl"])
        self.assertIn(fills[0]["reason"], ("entry_vwap", "entry_gap"))

        t_engine.process_closed_bars("QQQM", "2026-10-30", bars, now=now)
        self.assertEqual(len(db.list_t_fills("2026-10-30")), len(fills))

        state = db.get_t_state("QQQM", "2026-10-30")
        assert state is not None
        self.assertFalse(state["open_qty"])

        t_engine.reset_session(now)
        self.assertEqual(db.list_t_fills("2026-10-30"), [])
        t_engine.process_closed_bars("QQQM", "2026-10-30", bars, now=now)
        self.assertEqual(db.list_t_fills("2026-10-30"), [])

    def test_snapshot_has_no_live_orders(self) -> None:
        snap = t_engine.snapshot()
        self.assertFalse(snap["t_live_orders"])
        self.assertEqual(snap["t_mode"], "dry_run")
        self.assertEqual(snap["symbols"], ["QQQM", "AAPL"])
        self.assertNotIn("token", str(snap).lower())
        src = inspect.getsource(t_engine)
        self.assertNotIn("place_order", src)

    def test_display_bars_include_moving_levels(self) -> None:
        from app.services.t_engine import _display_bars
        from app.services.t_market import MarketSnap

        bar = _bar("10:00", 350, 351, 348, 349.5, vol=1000)
        snap = MarketSnap(
            symbol="AAPL",
            session_date="2026-10-30",
            is_today=True,
            bars=[bar],
            source="test",
            quote=None,
        )
        now = datetime(2026, 10, 30, 10, 6, tzinfo=ET)
        rows = _display_bars(snap, now, 2000)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        aapl = params_for("AAPL", 2000)
        self.assertAlmostEqual(row["entry"], round(row["vwap"] * (1 - aapl.entry_bps / 10000), 4))
        self.assertAlmostEqual(row["exit"], round(row["vwap"] * (1 - aapl.exit_bps / 10000), 4))
        self.assertAlmostEqual(row["stop"], round(row["vwap"] * (1 - aapl.stop_bps / 10000), 4))
        self.assertLess(row["stop"], row["entry"])
        self.assertLess(row["entry"], row["exit"])
        self.assertLess(row["exit"], row["vwap"])

    def test_reset_symbol_clears_all_sessions_only_that_symbol(self) -> None:
        now = datetime(2026, 10, 30, 16, 5, tzinfo=ET)
        common = dict(qty=6, price=100.0, pnl=None, price_source="trigger", bar_ts=now.isoformat())
        db.insert_t_fill(session_date="2026-10-29", symbol="QQQM", side="BUY", reason="entry_vwap", **common)
        db.insert_t_fill(
            session_date="2026-10-30",
            symbol="QQQM",
            side="BUY",
            reason="entry_vwap",
            qty=6,
            price=101.0,
            pnl=None,
            price_source="trigger",
            bar_ts=now.isoformat(),
        )
        db.insert_t_fill(
            session_date="2026-10-29",
            symbol="AAPL",
            side="BUY",
            reason="entry_vwap",
            qty=5,
            price=200.0,
            pnl=None,
            price_source="trigger",
            bar_ts=now.isoformat(),
        )
        db.insert_t_fill(
            session_date="2026-10-30",
            symbol="AAPL",
            side="SELL",
            reason="mr_vwap",
            qty=5,
            price=201.0,
            pnl=5.0,
            price_source="trigger",
            bar_ts=now.isoformat(),
        )
        db.upsert_t_state(
            symbol="QQQM",
            session_date="2026-10-29",
            open_entry_ts=now.isoformat(),
            open_entry_px=100.0,
            open_qty=6,
            last_bar_ts=now.isoformat(),
            flattened=False,
        )

        t_engine.reset_symbol_all("qqqm", now)
        self.assertEqual([row["symbol"] for row in db.list_t_fills("2026-10-29")], ["AAPL"])
        today = db.list_t_fills("2026-10-30")
        self.assertEqual([row["symbol"] for row in today], ["AAPL"])
        self.assertIsNone(db.get_t_state("QQQM", "2026-10-29"))
        state = db.get_t_state("QQQM", "2026-10-30")
        assert state is not None
        self.assertFalse(state["open_qty"])
        self.assertTrue(state["last_bar_ts"])

        t_engine.process_closed_bars("QQQM", "2026-10-30", self._session_bars(), now=now)
        self.assertEqual([row["symbol"] for row in db.list_t_fills("2026-10-30")], ["AAPL"])

        with self.assertRaises(ValueError):
            t_engine._reset_symbol_all("TSLA", now)

    def test_replay_shows_stored_fills_only(self) -> None:
        from app.services.t_market import MarketSnap

        day = "2026-10-29"
        bar = _bar("10:00", 100, 101, 99, 100, day=day)
        snap = MarketSnap(
            symbol="QQQM",
            session_date=day,
            is_today=False,
            bars=[bar],
            source="test",
            quote=None,
            sessions={day: [bar]},
        )
        now = datetime(2026, 10, 30, 16, 0, tzinfo=ET)
        rows = t_engine._history_rows(snap, 2000, now)
        with t_engine._view_lock:
            t_engine._view["history"] = {"QQQM": rows}
            t_engine._view["sources"] = {"QQQM": "test"}
        db.insert_t_fill(
            session_date=day,
            symbol="QQQM",
            side="BUY",
            qty=6,
            price=100.0,
            pnl=None,
            reason="entry_vwap",
            price_source="trigger",
            bar_ts=bar.start.isoformat(),
        )
        db.insert_t_fill(
            session_date=day,
            symbol="AAPL",
            side="BUY",
            qty=5,
            price=200.0,
            pnl=None,
            reason="entry_vwap",
            price_source="trigger",
            bar_ts=bar.start.isoformat(),
        )
        before = len(db.list_t_fills(day))
        payload = t_engine.replay_payload("QQQM", day)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["view"], "replay")
        self.assertIsNone(payload["error"])
        self.assertEqual(len(payload["bars"]), 1)
        self.assertIn("vwap", payload["bars"][0])
        self.assertEqual([row["symbol"] for row in payload["trades"]], ["QQQM"])
        self.assertEqual(len(db.list_t_fills(day)), before)
        missing = t_engine.replay_payload("QQQM", "2026-10-01")
        self.assertEqual(missing["bars"], [])
        self.assertTrue(missing["error"])
        self.assertEqual(len(db.list_t_fills(day)), before)

    def test_bar_cache_prefers_schwab_and_keeps_older_days(self) -> None:
        from app.services import t_bar_cache
        from app.services.t_market import group_sessions

        old = _bar("10:00", 10, 11, 9, 10.5, day="2026-09-01")
        recent = _bar("10:00", 100, 101, 99, 100, day="2026-10-29")
        self.assertEqual(t_bar_cache.ingest("AAPL", [old, recent], t_bar_cache.SOURCE_YAHOO), 2)
        schwab_recent = _bar("10:00", 100, 102, 99, 101, day="2026-10-29")
        self.assertEqual(t_bar_cache.ingest("AAPL", [schwab_recent], t_bar_cache.SOURCE_SCHWAB), 1)
        self.assertEqual(
            t_bar_cache.ingest("AAPL", [_bar("10:00", 1, 1, 1, 1, day="2026-10-29")], t_bar_cache.SOURCE_YAHOO),
            0,
        )
        yahoo_only = _bar("10:05", 5, 6, 4, 5, day="2026-09-01")
        self.assertEqual(t_bar_cache.ingest("AAPL", [yahoo_only], t_bar_cache.SOURCE_YAHOO), 1)
        updated = _bar("10:05", 5, 7, 4, 6.5, day="2026-09-01")
        self.assertEqual(t_bar_cache.ingest("AAPL", [updated], t_bar_cache.SOURCE_YAHOO), 1)
        weekend = _bar("10:00", 3, 3, 3, 3, day="2026-10-31")
        self.assertEqual(t_bar_cache.ingest("AAPL", [weekend], t_bar_cache.SOURCE_YAHOO), 0)

        loaded = t_bar_cache.load_intrabars("AAPL")
        by_day = group_sessions(loaded)
        self.assertEqual(sorted(by_day), ["2026-09-01", "2026-10-29"])
        self.assertAlmostEqual(by_day["2026-10-29"][0].close, 101.0)
        self.assertAlmostEqual(by_day["2026-09-01"][0].close, 10.5)
        self.assertAlmostEqual(by_day["2026-09-01"][1].close, 6.5)
        self.assertEqual(t_bar_cache.session_dates("AAPL"), ["2026-10-29", "2026-09-01"])

        self.assertTrue(t_bar_cache.backfill_due("CACHEME"))
        t_bar_cache.mark_backfill("CACHEME", now_mono=1000.0)
        self.assertFalse(t_bar_cache.backfill_due("CACHEME", now_mono=1010.0))
        self.assertTrue(t_bar_cache.backfill_due("CACHEME", now_mono=1000.0 + 6 * 3600))

    def test_replay_range_filters_cached_days(self) -> None:
        from datetime import date

        from app.services import t_bar_cache

        today = date(2026, 10, 30)
        dates = [
            "2026-10-30",
            "2026-10-24",
            "2026-10-23",
            "2026-10-22",
            "2026-09-30",
            "2026-07-30",
            "2026-04-30",
            "2025-10-30",
            "2025-10-29",
        ]
        self.assertEqual(
            t_bar_cache.filter_session_dates(dates, today, "1w"),
            ["2026-10-30", "2026-10-24", "2026-10-23"],
        )
        self.assertEqual(
            t_bar_cache.filter_session_dates(dates, today, "1m"),
            ["2026-10-30", "2026-10-24", "2026-10-23", "2026-10-22", "2026-09-30"],
        )
        self.assertIn("2026-07-30", t_bar_cache.filter_session_dates(dates, today, "3m"))
        self.assertNotIn("2026-04-30", t_bar_cache.filter_session_dates(dates, today, "3m"))
        self.assertIn("2026-04-30", t_bar_cache.filter_session_dates(dates, today, "6m"))
        self.assertNotIn("2025-10-29", t_bar_cache.filter_session_dates(dates, today, "6m"))
        self.assertIn("2025-10-30", t_bar_cache.filter_session_dates(dates, today, "1y"))
        self.assertNotIn("2025-10-29", t_bar_cache.filter_session_dates(dates, today, "1y"))
        self.assertEqual(t_bar_cache.replay_range_start(date(2026, 3, 31), "1m"), date(2026, 2, 28))

    def test_range_backfill_loads_window_and_does_not_add_fills(self) -> None:
        from datetime import date, timedelta

        from app.services import t_bar_cache

        today = date(2026, 10, 30)
        calls: list[tuple[date, date]] = []

        def yahoo(_symbol, start, end):
            calls.append((start, end))
            bars = [
                _bar("10:00", 10, 11, 9, 10.5, day="2026-10-15"),
                _bar("10:00", 12, 13, 11, 12.5, day="2026-10-30"),
            ]
            return bars, None

        def schwab(_symbol):
            raise RuntimeError("schwab offline")

        before = len(db.list_t_fills("2026-10-15"))
        payload = t_engine.ensure_replay_range(
            "QQQM",
            "1m",
            today=today,
            force=True,
            yahoo_fetch=yahoo,
            schwab_fetch=schwab,
        )
        self.assertEqual(calls, [(date(2026, 9, 30), today)])
        self.assertFalse(payload["partial"])
        self.assertEqual(payload["sessions"], ["2026-10-30", "2026-10-15"])
        self.assertTrue(payload["continuous"])
        self.assertEqual(len(payload["bars"]), 2)
        self.assertLess(payload["bars"][0]["time"], payload["bars"][1]["time"])
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["trades"], [])
        self.assertEqual(len(db.list_t_fills("2026-10-15")), before)
        chart = t_engine.replay_payload("QQQM", "2026-10-15")
        self.assertIsNone(chart["error"])
        self.assertGreaterEqual(len(chart["bars"]), 1)
        self.assertEqual(chart["trades"], [])

        year_calls: list[tuple[date, date]] = []

        def yahoo_year(_symbol, start, end):
            year_calls.append((start, end))
            return [_bar("11:00", 8, 9, 7, 8.5, day=end.isoformat())], None

        year = t_engine.ensure_replay_range(
            "AAPL",
            "1y",
            today=today,
            force=True,
            yahoo_fetch=yahoo_year,
            schwab_fetch=schwab,
        )
        self.assertTrue(year["partial"])
        self.assertIn("60", year["note"])
        self.assertEqual(year_calls[0][0], today - timedelta(days=t_bar_cache.YAHOO_5M_MAX_DAYS))
        self.assertEqual(year_calls[0][1], today)

        t_bar_cache.ingest(
            "AAPL",
            [_bar("10:00", 1, 2, 1, 1.5, day="2026-08-03")],
            t_bar_cache.SOURCE_YAHOO,
        )
        picked = t_engine.ensure_replay_range(
            "AAPL",
            "day",
            today=today,
            force=True,
            yahoo_fetch=yahoo_year,
            schwab_fetch=schwab,
        )
        self.assertIn("2026-08-03", picked["sessions"])
        self.assertIn("2026-10-30", picked["sessions"])
        self.assertFalse(picked["partial"])
        self.assertFalse(picked["continuous"])
        self.assertEqual(picked["bars"], [])


class TPageTest(unittest.TestCase):
    def test_page_csrf_and_stream(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.scheduler import DUE_TICK_JOB_ID, T_TICK_JOB_ID, get_scheduler

        with (
            patch("app.services.t_engine.tick", lambda *a, **k: None),
            patch("app.services.t_engine._tick_body", lambda *a, **k: None),
        ):
            with TestClient(app) as client:
                sched = get_scheduler()
                self.assertIsNotNone(sched.get_job(DUE_TICK_JOB_ID))
                self.assertIsNotNone(sched.get_job(T_TICK_JOB_ID))

                home = client.get("/plan/")
                self.assertEqual(home.status_code, 200)
                self.assertIn("做T", home.text)

                page = client.get("/plan/t")
                self.assertEqual(page.status_code, 200)
                self.assertIn("5分钟 VWAP", page.text)
                self.assertIn("不会", page.text)
                self.assertIn("QQQM", page.text)
                self.assertIn("31.3", page.text)
                self.assertIn("/t/stream", page.text)
                self.assertIn(">实时<", page.text)
                self.assertIn(">回看<", page.text)
                self.assertIn("指定天", page.text)
                self.assertIn('id="t-replay-day-wrap" hidden', page.text)
                self.assertIn("近1周", page.text)
                self.assertIn("近1月", page.text)
                self.assertIn("近3月", page.text)
                self.assertIn("近6月", page.text)
                self.assertIn("近1年", page.text)
                self.assertIn("/t/replay", page.text)
                self.assertIn("不会补写新成交", page.text)
                self.assertIn('id="t-chart"', page.text)
                self.assertIn("min-height:420px", page.text)
                self.assertIn("lg-vwap", page.text)
                self.assertIn("lg-entry", page.text)
                self.assertIn("lg-exit", page.text)
                self.assertIn("lg-stop", page.text)
                self.assertIn("模拟买", page.text)
                self.assertIn("模拟卖", page.text)
                self.assertNotIn("lg-last", page.text)
                self.assertNotIn("三条水平线", page.text)
                self.assertIn("入场（低于 VWAP）", page.text)
                self.assertIn("/static/lightweight-charts.standalone.production.js", page.text)
                self.assertNotIn("cdn.jsdelivr.net", page.text)
                self.assertIn("csrf", page.text.lower())

                lib = client.get("/plan/static/lightweight-charts.standalone.production.js")
                self.assertEqual(lib.status_code, 200)
                self.assertIn("LightweightCharts", lib.text)
                self.assertIn("Apache License", lib.text)
                chart_js = client.get("/plan/static/t_monitor.js")
                self.assertEqual(chart_js.status_code, 200)
                self.assertIn("entryLine", chart_js.text)
                self.assertNotIn("createPriceLine", chart_js.text)
                self.assertNotIn("priceLineVisible: true", chart_js.text)
                self.assertIn("priceLineVisible: false", chart_js.text)

                denied = client.post("/plan/t/start", data={})
                self.assertEqual(denied.status_code, 403)

                token = client.cookies.get("csrf_token")
                self.assertTrue(token)
                started = client.post(
                    "/plan/t/start",
                    data={"_csrf": token},
                    follow_redirects=False,
                )
                self.assertEqual(started.status_code, 303)
                self.assertTrue(started.headers["location"].endswith("/t"))

                state = client.get("/plan/t/state")
                self.assertEqual(state.status_code, 200)
                body = state.json()
                self.assertEqual(body["t_mode"], "dry_run")
                self.assertFalse(body["t_live_orders"])
                self.assertTrue(body["engine_running"])

                stream = client.get("/plan/t/stream?once=1")
                self.assertEqual(stream.status_code, 200)
                self.assertIn("text/event-stream", stream.headers["content-type"])
                self.assertIn("event: snapshot", stream.text)
                self.assertIn("dry_run", stream.text)

                bad = client.post(
                    "/plan/t/settings",
                    data={"_csrf": token, "symbols": "not a ticker", "notional_usd": "2000"},
                    follow_redirects=False,
                )
                self.assertEqual(bad.status_code, 303)
                self.assertIn("error=", bad.headers["location"])

                stopped = client.post(
                    "/plan/t/stop",
                    data={"_csrf": token},
                    follow_redirects=False,
                )
                self.assertEqual(stopped.status_code, 303)

                one = client.post(
                    "/plan/t/settings",
                    data={"_csrf": token, "symbols": "AAPL", "notional_usd": "2000"},
                    follow_redirects=False,
                )
                self.assertEqual(one.status_code, 303)
                page_one = client.get("/plan/t")
                self.assertEqual(page_one.status_code, 200)
                self.assertEqual(page_one.text.count('data-symbol="'), 1)
                self.assertIn('data-symbol="AAPL"', page_one.text)
                self.assertIn('id="t-chart"', page_one.text)
                self.assertIn("height:440px", page_one.text)
                self.assertIn("清空今日模拟", page_one.text)
                self.assertIn("清空此标的全部模拟", page_one.text)
                self.assertIn("/t/reset-symbol", page_one.text)

                missing = client.post("/plan/t/reset-symbol", data={"symbol": "AAPL"})
                self.assertEqual(missing.status_code, 403)
                cleared = client.post(
                    "/plan/t/reset-symbol",
                    data={"_csrf": token, "symbol": "AAPL"},
                    follow_redirects=False,
                )
                self.assertEqual(cleared.status_code, 303)
                self.assertTrue(cleared.headers["location"].endswith("/t"))
                unknown = client.post(
                    "/plan/t/reset-symbol",
                    data={"_csrf": token, "symbol": "TSLA"},
                    follow_redirects=False,
                )
                self.assertEqual(unknown.status_code, 303)
                self.assertIn("error=", unknown.headers["location"])

                bad_day = client.get("/plan/t/replay?symbol=AAPL&date=yesterday")
                self.assertEqual(bad_day.status_code, 400)
                replay = client.get("/plan/t/replay?symbol=AAPL&date=2026-10-29")
                self.assertEqual(replay.status_code, 200)
                replay_body = replay.json()
                self.assertTrue(replay_body["read_only"])
                self.assertEqual(replay_body["view"], "replay")

                bare = client.post("/plan/t/replay/backfill", data={"symbol": "AAPL", "range": "1m"})
                self.assertEqual(bare.status_code, 403)
                with patch(
                    "app.services.t_engine.ensure_replay_range",
                    return_value={
                        "symbol": "AAPL",
                        "range": "1m",
                        "sessions": ["2026-10-29", "2026-10-28"],
                        "partial": False,
                        "note": "",
                        "error": None,
                        "read_only": True,
                    },
                ):
                    filled = client.post(
                        "/plan/t/replay/backfill",
                        data={"_csrf": token, "symbol": "AAPL", "range": "1m"},
                    )
                self.assertEqual(filled.status_code, 200)
                self.assertEqual(filled.json()["sessions"][0], "2026-10-29")
                self.assertTrue(filled.json()["read_only"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
