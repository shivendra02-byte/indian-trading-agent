"""Fixes asked on 24 Nov 2026 (an NSE holiday, MCX evening only)."""

import unittest
from datetime import datetime

from tests._support import IsolatedStateTestCase

HOLIDAY = datetime(2026, 11, 24, 10, 30)     # Guru Nanak Jayanti: NSE shut, MCX evening only


class HolidayRunNowTests(IsolatedStateTestCase, unittest.TestCase):
    def test_swing_run_now_on_holiday_reuses_the_last_sessions_scan(self):
        from backend.signals import cash_swing
        cash_swing.ensure_scan_tables()
        run_id = cash_swing._start_run("2026-11-23", datetime(2026, 11, 23, 15, 50))
        cash_swing._finish_run(run_id, "ok", {})
        out = cash_swing.run_scan(now=HOLIDAY, force=True)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["run_id"], run_id)
        self.assertIn("2026-11-23 already ran", out["reason"])

    def test_swing_without_force_still_skips_holidays(self):
        from backend.signals import cash_swing
        out = cash_swing.run_scan(now=HOLIDAY)
        self.assertEqual(out["reason"], "not an NSE trading day")

    def test_futures_run_now_on_holiday_says_when_the_next_scan_is(self):
        from backend.signals.fno import run_hourly_scan
        out = run_hourly_scan("nse", now=HOLIDAY, force=True)
        self.assertEqual(out["status"], "skipped")
        self.assertIn("NSE closed today (Guru Nanak Jayanti)", out["reason"])
        self.assertIn("Wed 25 Nov 10:15", out["reason"])

    def test_mcx_before_the_evening_session_says_when_the_first_bar_closes(self):
        from backend.signals.fno import run_hourly_scan
        out = run_hourly_scan("mcx", now=HOLIDAY, force=True)
        self.assertIn("morning session closed today", out["reason"])
        self.assertIn("first bar closes at 18:00", out["reason"])


class MomentumSideSwitchTests(IsolatedStateTestCase, unittest.TestCase):
    def test_short_switch_overrides_config_and_resets(self):
        from backend.momentum import book
        self.assertFalse(book.settings()["short_enabled"])          # YAML default
        out = book.set_side("short", True)
        self.assertTrue(out["short_enabled"])
        self.assertTrue(book.status()["short_enabled"])
        book.set_side("short", None)
        self.assertFalse(book.settings()["short_enabled"])

    def test_unknown_side_rejected(self):
        from backend.momentum import book
        with self.assertRaises(ValueError):
            book.set_side("both", True)


class SensexFuturesTests(IsolatedStateTestCase, unittest.TestCase):
    def _kite(self, with_bse=True):
        from datetime import date
        from tests.test_marketdata import FakeKite

        class Kite(FakeKite):
            def instruments(self, exchange=None):
                if exchange == "BFO":
                    if not with_bse:
                        raise RuntimeError("BFO dump unavailable")
                    return [{"instrument_token": 801, "exchange": "BFO", "segment": "BFO-FUT",
                             "tradingsymbol": "SENSEX26DECFUT", "name": "SENSEX", "instrument_type": "FUT",
                             "expiry": date(2026, 12, 24), "strike": 0, "lot_size": 20, "tick_size": 0.05},
                            {"instrument_token": 802, "exchange": "BFO", "segment": "BFO-OPT",
                             "tradingsymbol": "SENSEX26DEC80000CE", "name": "SENSEX", "instrument_type": "CE",
                             "expiry": date(2026, 12, 24), "strike": 80000, "lot_size": 20, "tick_size": 0.05}]
                if exchange == "BSE":
                    return [{"instrument_token": 265, "exchange": "BSE", "segment": "INDICES",
                             "tradingsymbol": "SENSEX", "name": "SENSEX", "instrument_type": "EQ", "expiry": "",
                             "strike": 0, "lot_size": 0, "tick_size": 0}]
                return super().instruments(exchange)
        return Kite()

    def test_sensex_future_and_spot_are_kept_and_tradeable_in_my_trades(self):
        from backend import my_trades
        from backend.marketdata.instruments import refresh_instruments, spot_instrument
        summary = refresh_instruments(self._kite())
        self.assertEqual(summary["counts"].get("BFO-FUT"), 1)
        self.assertEqual(summary["counts"].get("BFO-OPT"), 1)        # SENSEX options kept for the intraday spread scan (24 Nov 2026)
        self.assertEqual(spot_instrument("SENSEX")["instrument_token"], 265)
        rows = my_trades.contracts("SENSEX", "BFO")
        self.assertEqual([r["contract"] for r in rows], ["SENSEX26DECFUT"])
        self.assertEqual(rows[0]["point_value"], 20)

    def test_missing_bse_dump_keeps_the_rest_of_the_master(self):
        from backend.marketdata.instruments import refresh_instruments
        summary = refresh_instruments(self._kite(with_bse=False))
        self.assertGreater(summary["counts"]["NFO-FUT"], 100)
        self.assertNotIn("BFO-FUT", summary["counts"])


BNF_ROWS = [{"contract": "BANKNIFTY26DECFUT", "expiry": "2026-12-29", "lot_size": 30, "instrument_token": 333,
             "active": True, "point_value": 30}]


class TenMinuteKite:
    """Kite stub: BANKNIFTY futures 10-minute bars, 20 Nov and 23 Nov, falling steadily, plus a
    bogus bar stamped 15:35 (after the close)."""
    def __init__(self):
        self.calls = 0

    def historical_data(self, token, start, stop, interval, continuous=False, oi=False):
        from datetime import date, timedelta
        self.calls += 1
        rows = []
        for day in (date(2026, 11, 20), date(2026, 11, 23)):
            t, price = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15), 64600.0
            while t.hour < 15 or (t.hour == 15 and t.minute <= 35):
                price -= 10
                rows.append({"date": t, "open": price + 10, "high": price + 40, "low": price - 30, "close": price,
                             "volume": 100})
                t += timedelta(minutes=10)
        return [r for r in rows if start <= r["date"] <= stop]


class MyTradesFetchTests(IsolatedStateTestCase, unittest.TestCase):
    def _add(self, **kw):
        from backend import my_trades
        data = {"symbol": "BANKNIFTY", "direction": "short", "lots": 1, "entry_price": 64500,
                "initial_stop": 65000, "entry_time": "2026-11-23T09:40", "interval": "10minute", **kw}
        return my_trades.add_trade(data, BNF_ROWS, client=kw.pop("client", None) or TenMinuteKite())

    def test_adding_a_past_trade_fetches_its_bars_and_follows_it(self):
        t = self._add()
        self.assertIsNotNone(t["last_close"])
        self.assertEqual(t["last_bar_ts"][11:16], "15:25")          # the 15:35 bar is ignored
        self.assertIsNotNone(t["atr"])
        self.assertLess(t["best_close"], 64500)
        self.assertIsNone(t["data_note"])

    def test_not_logged_in_says_so(self):
        from backend import my_trades
        data = {"symbol": "BANKNIFTY", "direction": "short", "lots": 1, "entry_price": 64500,
                "initial_stop": 65000, "entry_time": "2026-11-23T09:40", "interval": "10minute"}
        t = my_trades.add_trade(data, BNF_ROWS)                     # no Kite login in tests
        self.assertIsNone(t["last_close"])
        self.assertEqual(t["data_note"], "Log in to Kite to fetch bars")

    def test_recalculate_fetches_then_follows(self):
        from backend import my_trades
        data = {"symbol": "BANKNIFTY", "direction": "short", "lots": 1, "entry_price": 64500,
                "initial_stop": 65000, "entry_time": "2026-11-23T09:40", "interval": "10minute"}
        t = my_trades.add_trade(data, BNF_ROWS, fetch=False)
        self.assertIsNone(t["last_close"])
        kite = TenMinuteKite()
        out = my_trades.recalculate(client=kite)
        self.assertGreater(kite.calls, 0)
        self.assertEqual(out["fetch_problems"], {})
        self.assertIsNotNone(my_trades.get_trade(t["id"])["last_close"])

    def test_entry_time_must_be_in_a_session(self):
        from backend.my_trades import check_entry_time
        now = datetime(2026, 11, 24, 19, 0)
        with self.assertRaisesRegex(ValueError, "MCX was not trading at 09:26"):
            check_entry_time("MCX", datetime(2026, 11, 24, 9, 26), now)
        check_entry_time("MCX", datetime(2026, 11, 24, 18, 30), now)
        with self.assertRaisesRegex(ValueError, "NSE was closed on Tue 24 Nov"):
            check_entry_time("NFO", datetime(2026, 11, 24, 10, 0), now)
        with self.assertRaisesRegex(ValueError, "in the future"):
            check_entry_time("MCX", datetime(2026, 11, 24, 21, 0), now)
        check_entry_time("BFO", datetime(2026, 11, 23, 15, 30), now)


class TodayPnlTests(IsolatedStateTestCase, unittest.TestCase):
    DAY = "2026-11-25"

    def _journal(self, symbol, decision, status, **kw):
        from backend.journal.store import add_records, list_records, update_record
        add_records([{"segment": "stock_futures", "symbol": symbol, "instrument_token": 1, "exchange": "NFO",
                      "signal_date": self.DAY, "signal_ts": f"{self.DAY}T10:15:00", "setup": "pullback",
                      "direction": "long", "regime": "STRONG_UP", "score": 70, "decision": decision,
                      "entry": 100.0, "stop": 98.0, "target": 104.0, "rr": 2.0, "atr": 1.0,
                      "entry_date": self.DAY, "fill_until": f"{self.DAY}T13:15:00", "time_stop_date": "2026-12-29",
                      "extra": {"value_per_point": 500}}])
        rec = list_records("symbol = ?", (symbol,))[0]
        update_record(rec["id"], {"track_status": status, **kw})

    def test_both_books_realised_and_open(self):
        from backend import my_trades
        from backend.db import get_db
        from backend.pnl_today import today_summary
        rows = [{"contract": "RELIANCE26DECFUT", "expiry": "2026-12-29", "lot_size": 500, "instrument_token": 222,
                 "active": True, "point_value": 500}]
        base = {"symbol": "RELIANCE", "direction": "long", "lots": 1, "entry_price": 100, "initial_stop": 98,
                "entry_time": "2026-11-23T10:05"}
        closed = my_trades.add_trade(base, rows, fetch=False)
        my_trades.close_trade(closed["id"], 103, f"{self.DAY}T11:00")          # +1.5R, +₹1,500
        hit = my_trades.add_trade(base, rows, fetch=False)
        with get_db() as conn:
            conn.execute("UPDATE my_trades SET status = 'stop_hit', hit_time = ?, hit_price = 98, open_r = -1, "
                         "open_pnl = -1000 WHERE id = ?", (f"{self.DAY}T12:15:00", hit["id"]))
        live = my_trades.add_trade(base, rows, fetch=False)
        with get_db() as conn:
            conn.execute("UPDATE my_trades SET last_close = 101, open_r = 0.5, open_pnl = 500 WHERE id = ?", (live["id"],))

        self._journal("TCS", "approved", "closed", exit_ts=f"{self.DAY}T14:15:00", exit_price=104,
                      pnl_r=2.0, pnl_rupees=9_800.0, fill_price=100.0)
        self._journal("INFY", "approved", "open", fill_price=100.0,
                      extra={"value_per_point": 500, "stop_now": {"last_close": 101.0, "at": f"{self.DAY}T14:15:00"}})
        self._journal("WIPRO", "not_selected", "closed", exit_ts=f"{self.DAY}T14:15:00", pnl_r=2.0, pnl_rupees=1.0)

        out = today_summary(datetime.fromisoformat(self.DAY).date())
        self.assertEqual(out["mine"]["realised_pnl"], 500.0)                   # +1,500 closed, −1,000 stop hit
        self.assertEqual(out["mine"]["unrealised_pnl"], 500.0)
        states = {r["state"] for r in out["mine"]["rows"]}
        self.assertEqual(states, {"closed", "awaiting_exit", "open"})
        names = [r["name"] for r in out["system"]["rows"]]
        self.assertNotIn("WIPRO", names)                                       # only approved paper trades
        self.assertEqual(out["system"]["realised_pnl"], 9_800.0)
        infy = next(r for r in out["system"]["rows"] if r["name"] == "INFY")
        self.assertEqual(infy["r"], 0.5)
        self.assertGreater(infy["pnl"], 0)
        self.assertEqual(out["total"]["realised_pnl"], 10_300.0)
        self.assertEqual(out["total"]["ended_today"], 3)

    def test_taken_and_rules_only_trades(self):
        """Asked 24 Nov 2026: Monday's ORB trades were alerted "rules only" (AI error) and taken."""
        from backend.journal.store import list_records, set_user_action
        from backend.pnl_today import today_summary
        self._journal("RELIANCE", "shortlisted", "closed", exit_ts=f"{self.DAY}T15:15:00", exit_price=101.5,
                      pnl_r=0.75, pnl_rupees=3_700.0, fill_price=100.0, qty=5,
                      panel={"alert": "sent", "status": "error"})
        self._journal("LT", "shortlisted", "closed", exit_ts=f"{self.DAY}T15:15:00", exit_price=101.0,
                      pnl_r=0.5, pnl_rupees=2_400.0, fill_price=100.0, qty=5, panel={"alert": "not sent"})
        rel = list_records("symbol = 'RELIANCE'")[0]
        set_user_action(rel["id"], "taken", fill_price=100.5, exit_price=102.5, note="Reached a support")
        out = today_summary(datetime.fromisoformat(self.DAY).date())
        mine = out["mine"]["rows"]
        self.assertEqual([r["name"] for r in mine], ["RELIANCE"])
        self.assertEqual(mine[0]["r"], 1.0)                               # (102.5 - 100.5) / 2
        self.assertEqual(mine[0]["pnl"], 2.0 * 5 * 500)                   # 5 lots x 500 per point
        system = out["system"]["rows"]
        self.assertEqual([r["name"] for r in system], ["RELIANCE"])       # LT was never alerted
        self.assertEqual(system[0]["alert"], "rules only")
        self.assertTrue(system[0]["taken"])
        self.assertEqual(out["total"]["realised_pnl"], 5_000.0)           # counted once, as yours

    def test_api(self):
        from tests._support import fresh_test_client, login
        with fresh_test_client() as client:
            login(client)
            r = client.get("/api/pnl/today?day=2026-11-25")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total"]["total_pnl"], 0.0)


class PostmortemSoFarTests(IsolatedStateTestCase, unittest.TestCase):
    def test_mid_week_run_does_not_block_saturdays_report(self):
        from datetime import date
        from backend.db import get_db
        from backend.journal import postmortem
        week = date(2026, 11, 23)
        postmortem.run_weekly_postmortem(week, with_ai=False)
        with get_db() as conn:
            conn.execute("UPDATE weekly_postmortems SET created_at = '2026-11-24 11:50:00'")
        self.assertFalse(postmortem.exists(week))
        with get_db() as conn:
            conn.execute("UPDATE weekly_postmortems SET created_at = '2026-11-28 08:01:00'")
        self.assertTrue(postmortem.exists(week))


if __name__ == "__main__":
    unittest.main()
