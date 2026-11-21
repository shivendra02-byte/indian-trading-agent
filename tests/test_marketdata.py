"""Market calendar, instrument master, candle store and pause-switch tests (no network)."""

import unittest
from datetime import date, datetime, time

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login
from tradingagents.utils import market_calendar as cal


class MarketCalendarTests(unittest.TestCase):
    def test_nse_2026_official_holidays(self):
        self.assertFalse(cal.is_trading_day(date(2026, 11, 24)))  # Guru Nanak Jayanti
        self.assertFalse(cal.is_trading_day(date(2026, 3, 26)))   # Ram Navami
        self.assertTrue(cal.is_trading_day(date(2026, 3, 30)))    # was a wrong placeholder
        self.assertFalse(cal.is_trading_day(date(2026, 11, 21)))  # Saturday

    def test_mcx_partial_holiday_keeps_evening_only(self):
        d = date(2026, 11, 24)
        self.assertEqual(cal.session_windows("MCX", d), [(time(17, 0), time(23, 55))])
        self.assertFalse(cal.is_exchange_open("MCX", datetime(2026, 11, 24, 11, 0)))
        self.assertTrue(cal.is_exchange_open("MCX", datetime(2026, 11, 24, 18, 0)))
        self.assertFalse(cal.is_exchange_open("NSE", datetime(2026, 11, 24, 11, 0)))

    def test_mcx_new_year_morning_only_and_full_closures(self):
        self.assertEqual(cal.session_windows("MCX", date(2026, 1, 1)), [(time(9, 0), time(17, 0))])
        self.assertEqual(cal.session_windows("MCX", date(2026, 12, 25)), [])
        self.assertTrue(cal.is_trading_day(date(2026, 1, 1)))

    def test_mcx_close_follows_us_daylight_saving(self):
        self.assertEqual(cal.mcx_evening_close(date(2026, 7, 1)), time(23, 30))
        self.assertEqual(cal.mcx_evening_close(date(2026, 11, 23)), time(23, 55))

    def test_special_weekend_sessions_are_not_trading_days(self):
        self.assertFalse(cal.is_trading_day(date(2026, 11, 8)))
        self.assertEqual(cal.holiday_name(date(2026, 11, 8)), "Muhurat trading")

    def test_status_flags_missing_next_year_calendar(self):
        status = cal.exchange_status("NSE", datetime(2026, 12, 2, 10, 0))
        self.assertTrue(status["open_now"])
        self.assertFalse(status["calendar_loaded_next_year"])
        self.assertEqual(status["next_closure"]["date"], "2026-12-25")


def nfo_dump():
    rows = []
    token = 1000
    for name in [f"STOCK{i}" for i in range(110)] + ["NIFTY", "BANKNIFTY"]:
        for expiry in (date(2026, 11, 24), date(2026, 12, 29)):
            token += 1
            rows.append({"instrument_token": token, "exchange": "NFO", "segment": "NFO-FUT",
                         "tradingsymbol": f"{name}{expiry:%y%b}FUT".upper(), "name": name,
                         "instrument_type": "FUT", "expiry": expiry, "strike": 0,
                         "lot_size": 500, "tick_size": 0.05})
    rows.append({"instrument_token": 9001, "exchange": "NFO", "segment": "NFO-OPT",
                 "tradingsymbol": "NIFTY26DEC26000CE", "name": "NIFTY", "instrument_type": "CE",
                 "expiry": date(2026, 12, 29), "strike": 26000, "lot_size": 75, "tick_size": 0.05})
    rows.append({"instrument_token": 9002, "exchange": "NFO", "segment": "NFO-OPT",
                 "tradingsymbol": "STOCK1DEC100CE", "name": "STOCK1", "instrument_type": "CE",
                 "expiry": date(2026, 12, 29), "strike": 100, "lot_size": 500, "tick_size": 0.05})
    return rows


NSE_DUMP = [
    {"instrument_token": 501, "exchange": "NSE", "segment": "NSE", "tradingsymbol": "STOCK1",
     "name": "STOCK ONE", "instrument_type": "EQ", "expiry": "", "strike": 0, "lot_size": 1, "tick_size": 0.05},
    {"instrument_token": 502, "exchange": "NSE", "segment": "NSE", "tradingsymbol": "CASHONLY",
     "name": "CASH ONLY", "instrument_type": "EQ", "expiry": "", "strike": 0, "lot_size": 1, "tick_size": 0.05},
    {"instrument_token": 256265, "exchange": "NSE", "segment": "INDICES", "tradingsymbol": "NIFTY 50",
     "name": "NIFTY 50", "instrument_type": "EQ", "expiry": "", "strike": 0, "lot_size": 0, "tick_size": 0},
]
MCX_DUMP = [
    {"instrument_token": 701, "exchange": "MCX", "segment": "MCX-FUT", "tradingsymbol": "GOLDM26DECFUT",
     "name": "GOLDM", "instrument_type": "FUT", "expiry": date(2026, 12, 4), "strike": 0, "lot_size": 1, "tick_size": 1},
    {"instrument_token": 702, "exchange": "MCX", "segment": "MCX-FUT", "tradingsymbol": "GOLD26DECFUT",
     "name": "GOLD", "instrument_type": "FUT", "expiry": date(2026, 12, 4), "strike": 0, "lot_size": 1, "tick_size": 1},
]


class FakeKite:
    def __init__(self, nfo=None):
        self.nfo = nfo if nfo is not None else nfo_dump()
        self.calls = []

    def instruments(self, exchange=None):
        return {"NFO": self.nfo, "NSE": NSE_DUMP, "MCX": MCX_DUMP}[exchange]

    def historical_data(self, token, start, stop, interval, continuous=False, oi=False):
        self.calls.append((token, start, stop, interval))
        bars = []
        for hour in (9, 10, 11, 12, 13, 14, 15):
            ts = datetime(2026, 11, 20, hour, 15)
            if start <= ts <= stop:
                bars.append({"date": ts, "open": hour, "high": hour + 1, "low": hour - 1,
                             "close": hour + 0.5, "volume": 10})
        return bars


class InstrumentAndCandleTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        ensure_marketdata_tables()

    def test_universe_is_fno_stocks_only(self):
        from backend.marketdata.instruments import fno_universe, refresh_instruments
        summary = refresh_instruments(FakeKite())
        self.assertEqual(summary["fno_stocks"], 110)
        self.assertEqual(summary["counts"]["NFO-OPT"], 1)  # index options only
        self.assertEqual(summary["counts"]["MCX-FUT"], 1)  # minis only
        universe = fno_universe(today=date(2026, 11, 2))
        symbols = {s["symbol"] for s in universe}
        self.assertNotIn("CASHONLY", symbols)
        self.assertNotIn("NIFTY", symbols)
        stock1 = next(s for s in universe if s["symbol"] == "STOCK1")
        self.assertEqual(stock1["spot_token"], 501)
        self.assertEqual(stock1["ban"], "unknown")

    def test_partial_dump_keeps_previous_master(self):
        from backend.marketdata.instruments import InstrumentMasterError, instrument_summary, refresh_instruments
        refresh_instruments(FakeKite())
        with self.assertRaises(InstrumentMasterError):
            refresh_instruments(FakeKite(nfo=nfo_dump()[:20]))
        self.assertEqual(instrument_summary()["fno_stocks"], 110)

    def test_rollover_to_next_month_near_expiry(self):
        from backend.marketdata.instruments import active_future, refresh_instruments
        refresh_instruments(FakeKite())
        early = active_future("STOCK1", today=date(2026, 11, 2))
        late = active_future("STOCK1", today=date(2026, 11, 19))
        self.assertEqual(early["expiry"], "2026-11-24")
        self.assertEqual(late["expiry"], "2026-12-29")

    def test_ban_list_parse_and_status(self):
        from backend.marketdata.instruments import ban_status, refresh_ban_list
        text = "Securities in Ban For Trade Date 23-NOV-2026:\n1,STOCK1\n2,STOCK9\n"
        result = refresh_ban_list(fetch=lambda: text)
        self.assertEqual(result["trade_date"], "2026-11-23")
        self.assertEqual(ban_status(date(2026, 11, 23))["symbols"], ["STOCK1", "STOCK9"])
        empty = refresh_ban_list(fetch=lambda: "Securities in Ban For Trade Date 25-NOV-2026:\n")
        self.assertEqual(empty["symbols"], [])
        self.assertEqual(ban_status(date(2026, 11, 25)), {"status": "ok", "trade_date": "2026-11-25", "symbols": []})
        self.assertEqual(refresh_ban_list(fetch=lambda: 1 / 0)["status"], "unknown")

    def test_candle_sync_keeps_only_completed_bars_and_catches_up(self):
        from backend.marketdata.candles import get_candles, resample_4h, sync_candles
        kite = FakeKite()
        saved = sync_candles(kite, 501, "60minute", "NSE", 5,
                             now=datetime(2026, 11, 20, 12, 30), sleep=lambda s: None)
        self.assertEqual(saved, 3)  # 09:15, 10:15, 11:15 done; 12:15 still forming
        sync_candles(kite, 501, "60minute", "NSE", 5,
                     now=datetime(2026, 11, 20, 15, 31), sleep=lambda s: None)
        rows = get_candles(501, "60minute")
        self.assertEqual([r["ts"][11:16] for r in rows],
                         ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"])
        bars = resample_4h(rows, "NSE")
        self.assertEqual([b["ts"][11:16] for b in bars], ["09:15", "13:15"])
        self.assertEqual((bars[0]["open"], bars[0]["high"], bars[0]["close"]), (9, 13, 12.5))
        self.assertEqual(bars[1]["volume"], 30)

    def test_jobs_skip_when_paused(self):
        from backend.marketdata import jobs
        from backend.system_state import set_paused
        set_paused(True, "test")
        self.assertEqual(jobs.candle_sync(client=FakeKite())["status"], "skipped")
        set_paused(False)
        result = jobs.morning_refresh(client=FakeKite())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["instruments"]["fno_stocks"], 110)


class PauseRouteTests(IsolatedStateTestCase, unittest.TestCase):
    def test_pause_toggle_and_status_routes(self):
        with fresh_test_client() as client:
            login(client)
            headers = csrf_headers(client)
            self.assertFalse(client.get("/api/system/pause").json()["paused"])
            paused = client.post("/api/system/pause", json={"paused": True, "reason": "lunch"}, headers=headers)
            self.assertEqual(paused.status_code, 200, paused.text)
            self.assertTrue(paused.json()["paused"])
            status = client.get("/api/marketdata/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertTrue(status.json()["pause"]["paused"])
            cal_status = client.get("/api/market-calendar/status")
            self.assertEqual(cal_status.status_code, 200, cal_status.text)
            self.assertEqual({e["exchange"] for e in cal_status.json()["exchanges"]}, {"NSE", "MCX"})

    def test_pause_requires_csrf(self):
        with fresh_test_client() as client:
            login(client)
            response = client.post("/api/system/pause", json={"paused": True})
            self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
