"""Your own futures trades: trailing stop, alerts, close, API."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login

ROWS = [{"contract": "RELIANCE26NOVFUT", "expiry": "2026-11-24", "lot_size": 500, "instrument_token": 111,
         "active": False, "point_value": 500},
        {"contract": "RELIANCE26DECFUT", "expiry": "2026-12-29", "lot_size": 500, "instrument_token": 222,
         "active": True, "point_value": 500}]


def hourly(prices, start=datetime(2026, 11, 23, 9, 15)):
    """prices: (open, high, low, close) per bar."""
    out, t = [], start
    for o, h, l, c in prices:
        out.append({"ts": t.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 1, "oi": 0})
        t += timedelta(hours=1)
    return out


def trade(**kw):
    base = {"direction": "long", "entry_price": 100.0, "initial_stop": 98.0, "target": None, "lots": 2,
            "point_value": 500, "breakeven_at_r": 1.0, "breakeven_lock_r": 0.0, "trail_start_r": 1.0, "trail_atr": 2.0}
    return {**base, **kw}


class FollowTests(unittest.TestCase):
    def test_breakeven_then_trail_then_hit(self):
        from backend.my_trades import follow
        up = hourly([(100, 100.5, 99.5, 100.2), (100.2, 102.4, 100.1, 102.2), (102.2, 105, 102, 104.8)])
        s = follow(trade(), up, atr=1.0)
        self.assertEqual(s["status"], "open")
        self.assertEqual(s["stop_stage"], "trailing")
        self.assertAlmostEqual(s["current_stop"], 102.8)                  # best close 104.8 - 2 ATR
        self.assertEqual(s["open_pnl"], round((104.8 - 100) * 2 * 500))
        down = up + hourly([(104.8, 105, 102.5, 102.6)], start=datetime(2026, 11, 23, 12, 15))
        s2 = follow(trade(), down, atr=1.0)
        self.assertEqual((s2["status"], s2["stop_stage"]), ("stop_hit", "trail_stop"))
        self.assertAlmostEqual(s2["hit_price"], 102.8)
        self.assertGreater(s2["open_r"], 1)

    def test_breakeven_only_without_atr_and_short(self):
        from backend.my_trades import follow
        s = follow(trade(), hourly([(100, 102.5, 99.8, 102.3)]), atr=None)
        self.assertEqual((s["current_stop"], s["stop_stage"]), (100.0, "breakeven"))
        short = trade(direction="short", initial_stop=102.0)
        s = follow(short, hourly([(100, 100.2, 97.5, 97.6), (97.6, 97.8, 95, 95.2)]), atr=1.0)
        self.assertEqual(s["stop_stage"], "trailing")
        self.assertAlmostEqual(s["current_stop"], 97.2)                   # best close 95.2 + 2 ATR
        self.assertEqual(follow(short, [], atr=1.0)["current_stop"], 102.0)

    def test_target(self):
        from backend.my_trades import follow
        s = follow(trade(target=103.0), hourly([(100, 103.5, 99.9, 103.2)]), atr=1.0)
        self.assertEqual((s["status"], s["hit_price"]), ("target_hit", 103.0))

    def test_validate(self):
        from backend.my_trades import validate
        when = {"entry_time": "2026-11-23T10:05"}
        ok = validate({"symbol": "reliance", "direction": "long", "lots": 1, "entry_price": 100, "initial_stop": 98,
                       **when}, ROWS)
        self.assertEqual((ok["contract"], ok["instrument_token"], ok["interval"]), ("RELIANCE26DECFUT", 222, "60minute"))
        picked = validate({"symbol": "RELIANCE", "contract": "RELIANCE26NOVFUT", "direction": "long", "lots": 1,
                           "entry_price": 100, "initial_stop": 98, **when}, ROWS)
        self.assertEqual(picked["instrument_token"], 111)
        for bad in ({"initial_stop": 101}, {"lots": 0}, {"contract": "TCS26DECFUT"}, {"exchange": "MCX", "interval": "10minute"}):
            with self.assertRaises(ValueError):
                validate({"symbol": "RELIANCE", "direction": "long", "lots": 1, "entry_price": 100, "initial_stop": 98,
                          **when, **bad}, ROWS)


class StoreTests(IsolatedStateTestCase, unittest.TestCase):
    def test_add_update_alerts_close(self):
        from backend import my_trades
        history = hourly([(99, 100, 98, 99.5)] * 20, start=datetime(2026, 11, 20, 9, 15))   # ATR = 2 before entry
        after = hourly([(100, 100.5, 99.5, 100.2)], start=datetime(2026, 11, 23, 10, 15))
        with mock.patch("backend.marketdata.candles.get_candles", return_value=history + after):
            t = my_trades.add_trade({"symbol": "RELIANCE", "direction": "long", "lots": 2, "entry_price": 100,
                                     "initial_stop": 96, "entry_time": "2026-11-23T10:05"}, ROWS)
        self.assertEqual((t["status"], t["current_stop"], t["stop_stage"]), ("open", 96.0, "initial"))
        self.assertAlmostEqual(t["atr"], 2.0)
        self.assertEqual(my_trades.open_contracts()[0]["instrument_token"], 222)
        sent = []
        more = after + hourly([(100.2, 104.5, 100.1, 104.2), (104.2, 109, 104, 108.8)], start=datetime(2026, 11, 23, 11, 15))
        with mock.patch("backend.marketdata.candles.get_candles", return_value=history + more):
            r = my_trades.update_trades(notify=lambda tr, st, kind: sent.append((kind, st["current_stop"])) or "sent")
        self.assertEqual(r["moved"], 1)
        self.assertEqual(sent, [("moved", 104.8)])                         # 108.8 - 2 x 2 ATR
        crash = more + hourly([(108.8, 109, 103, 103.5)], start=datetime(2026, 11, 23, 13, 15))
        with mock.patch("backend.marketdata.candles.get_candles", return_value=history + crash):
            r = my_trades.update_trades(notify=lambda tr, st, kind: sent.append((kind, st["status"])) or "sent")
        self.assertEqual((r["hit"], sent[-1]), (1, ("hit", "stop_hit")))
        text = my_trades.build_alert(my_trades.get_trade(t["id"]), my_trades.follow(
            my_trades.get_trade(t["id"]), crash, 2.0), "hit")
        self.assertIn("Stop hit", text)
        closed = my_trades.close_trade(t["id"], 104.5)
        self.assertEqual((closed["status"], closed["realised_r"], closed["realised_pnl"]), ("closed", 1.12, 4500.0))
        self.assertEqual(my_trades.open_contracts(), [])

    def test_api(self):
        with fresh_test_client() as client:
            login(client)
            with mock.patch("backend.my_trades.contracts", return_value=ROWS), \
                    mock.patch("backend.marketdata.candles.get_candles", return_value=[]):
                r = client.post("/api/my-trades", json={"symbol": "RELIANCE", "direction": "short", "lots": 1,
                                                        "entry_price": 100, "initial_stop": 103,
                                                        "entry_time": "2026-11-23T10:05"},
                                headers=csrf_headers(client))
                self.assertEqual(r.status_code, 200, r.text)
                bad = client.post("/api/my-trades", json={"symbol": "RELIANCE", "direction": "short", "lots": 1,
                                                          "entry_price": 100, "initial_stop": 97},
                                  headers=csrf_headers(client))
                self.assertEqual(bad.status_code, 400)
                listed = client.get("/api/my-trades").json()
                self.assertEqual(listed["trades"][0]["contract"], "RELIANCE26DECFUT")
                self.assertEqual(listed["settings"]["trail_atr"], 2.0)
            tid = r.json()["id"]
            closed = client.post(f"/api/my-trades/{tid}/close", json={"exit_price": 98.5}, headers=csrf_headers(client))
            self.assertEqual(closed.json()["realised_r"], 0.5)
            self.assertEqual(client.delete(f"/api/my-trades/{tid}", headers=csrf_headers(client)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
