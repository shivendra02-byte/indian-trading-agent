"""Kite WebSocket live prices for the Today P&L (asked 24 Nov 2026)."""

import asyncio
import json
import struct
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

from tests._support import IsolatedStateTestCase


def frame(*pairs):
    """Binary frame of ltp packets: (token, price in rupees)."""
    body = b"".join(struct.pack(">H", 8) + struct.pack(">Ii", t, round(p * 100)) for t, p in pairs)
    return struct.pack(">H", len(pairs)) + body


class ParseTests(unittest.TestCase):
    def test_ltp_packets(self):
        from backend.marketdata.live import parse_frame
        self.assertEqual(parse_frame(frame((12345, 2450.55), (256265, 23987.1))),
                         [(12345, 2450.55), (256265, 23987.1)])

    def test_heartbeat_and_short_frames(self):
        from backend.marketdata.live import parse_frame
        self.assertEqual(parse_frame(b"\x00"), [])
        self.assertEqual(parse_frame(struct.pack(">H", 2) + struct.pack(">H", 8)), [])   # truncated

    def test_currency_segment_divisor(self):
        from backend.marketdata.live import parse_frame
        token = (1 << 8) | 3                                   # segment 3 = CDS
        data = struct.pack(">H", 1) + struct.pack(">H", 8) + struct.pack(">Ii", token, 835_000_000)
        self.assertAlmostEqual(parse_frame(data)[0][1], 83.5)

    def test_quote_packets_read_the_first_eight_bytes(self):
        from backend.marketdata.live import parse_frame
        pkt = struct.pack(">Ii", 7, 10_050) + b"\x00" * 36            # 44-byte quote packet
        self.assertEqual(parse_frame(struct.pack(">H", 1) + struct.pack(">H", 44) + pkt), [(7, 100.5)])

    def test_only_open_exchanges_followed(self):
        from backend.marketdata.live import tokens_to_follow
        contracts = {1: "NFO", 2: "MCX", 3: "BFO", 4: "NSE"}
        self.assertEqual(tokens_to_follow(contracts, {"MCX"}, 10), [2])
        self.assertEqual(tokens_to_follow(contracts, {"NSE", "MCX"}, 2), [1, 2])

    def test_stale_prices_are_ignored(self):
        from backend.marketdata.live import LiveFeed
        f = LiveFeed()
        f.update([(5, 101.0)], at=time.time() - 500)
        self.assertIsNone(f.price(5, max_age=120))
        f.update([(5, 102.0)])
        self.assertEqual(f.price(5, max_age=120)[0], 102.0)


class LivePnlTests(IsolatedStateTestCase, unittest.TestCase):
    DAY = "2026-11-25"

    def setUp(self):
        super().setUp()
        from backend.marketdata.live import feed
        feed._prices.clear()
        self.addCleanup(feed._prices.clear)

    def test_open_trades_use_the_live_price(self):
        from backend import my_trades
        from backend.db import get_db
        from backend.journal.store import add_records, list_records, update_record
        from backend.marketdata.live import feed, wanted_contracts
        from backend.pnl_today import today_summary
        rows = [{"contract": "RELIANCE26DECFUT", "expiry": "2026-12-29", "lot_size": 500, "instrument_token": 222,
                 "active": True, "point_value": 500}]
        t = my_trades.add_trade({"symbol": "RELIANCE", "direction": "long", "lots": 1, "entry_price": 100,
                                 "initial_stop": 98, "entry_time": "2026-11-23T10:05"}, rows, fetch=False)
        with get_db() as conn:
            conn.execute("UPDATE my_trades SET last_close = 101, open_r = 0.5, open_pnl = 500 WHERE id = ?", (t["id"],))
        add_records([{"segment": "stock_futures", "symbol": "INFY", "instrument_token": 333, "exchange": "NFO",
                      "signal_date": self.DAY, "signal_ts": f"{self.DAY}T10:15:00", "setup": "pullback",
                      "direction": "short", "regime": "STRONG_DOWN", "score": 70, "decision": "approved",
                      "entry": 100.0, "stop": 102.0, "target": 96.0, "rr": 2.0, "atr": 1.0,
                      "entry_date": self.DAY, "fill_until": f"{self.DAY}T13:15:00", "time_stop_date": "2026-12-29",
                      "extra": {"value_per_point": 500}}])
        rec = list_records("symbol = 'INFY'")[0]
        update_record(rec["id"], {"track_status": "open", "fill_price": 100.0,
                                  "extra": {"value_per_point": 500, "stop_now": {"last_close": 99.0}}})

        self.assertEqual(wanted_contracts(), {222: "NFO", 333: "NFO"})
        day = datetime.fromisoformat(self.DAY).date()
        before = today_summary(day)
        self.assertEqual(before["mine"]["rows"][0]["pnl"], 500.0)              # last bar close
        self.assertFalse(before["mine"]["rows"][0].get("live"))

        feed.update([(222, 103.0), (333, 98.0)])
        out = today_summary(day)
        mine = out["mine"]["rows"][0]
        self.assertTrue(mine["live"])
        self.assertEqual((mine["price"], mine["r"], mine["pnl"]), (103.0, 1.5, 1500.0))
        infy = out["system"]["rows"][0]
        self.assertTrue(infy["live"])
        self.assertEqual((infy["price"], infy["r"]), (98.0, 1.0))             # short: +2 points on a 2-point risk
        self.assertIn("live_feed", out)
        # the live price never touches the stored trade
        self.assertEqual(my_trades.list_trades()[0]["last_close"], 101)


class SessionTests(IsolatedStateTestCase, unittest.TestCase):
    """The client against a local WebSocket server speaking Kite's protocol."""

    def test_subscribes_in_ltp_mode_and_reads_ticks(self):
        from websockets.asyncio.server import serve
        from backend.marketdata import live

        got: list[dict] = []
        port_box: dict = {}
        ready = threading.Event()
        done = threading.Event()

        async def handler(ws):
            for _ in range(2):
                got.append(json.loads(await ws.recv()))
            await ws.send(b"\x00")                                        # heartbeat
            await ws.send(frame((222, 2451.25)))
            await asyncio.sleep(0.3)
            await ws.close()

        async def server():
            async with serve(handler, "127.0.0.1", 0) as srv:
                port_box["port"] = srv.sockets[0].getsockname()[1]
                ready.set()
                while not done.is_set():
                    await asyncio.sleep(0.05)

        th = threading.Thread(target=lambda: asyncio.run(server()), daemon=True)
        th.start()
        ready.wait(5)
        f = live.LiveFeed()
        with mock.patch.object(live, "URL", f"ws://127.0.0.1:{port_box['port']}"), \
                mock.patch.object(f, "_wanted", return_value=([222], "")):
            try:
                asyncio.run(f._session([222]))
            except Exception:
                pass                                                      # server closes the socket
        done.set()
        self.assertEqual(got[0], {"a": "subscribe", "v": [222]})
        self.assertEqual(got[1], {"a": "mode", "v": ["ltp", [222]]})
        self.assertEqual(f.price(222, max_age=60)[0], 2451.25)

    def test_idle_without_kite_login(self):
        from backend.marketdata.live import LiveFeed
        tokens, why = LiveFeed()._wanted()
        self.assertEqual(tokens, [])
        self.assertIn("Kite", why)


if __name__ == "__main__":
    unittest.main()
