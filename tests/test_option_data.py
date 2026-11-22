"""Option data layer: Black-76, UDiFF bhavcopy parsing, IV/Greeks, storage,
backfill, scheduler timing and API (synthetic files, no network)."""

from __future__ import annotations

import io
import math
import os
import unittest
import zipfile
from datetime import date, datetime

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login

HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
          "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
          "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
          "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def _line(trade, tp, symbol, expiry, strike, opt, close, spot, volume=1000, lot=65):
    strike_text = f"{strike:.2f}" if strike else ""
    return (f"{trade},{trade},FO,NSE,{tp},1,,{symbol},,{expiry},{expiry},{strike_text},{opt},X,"
            f"{close},{close},{close},{close:.2f},{close},{close},{spot},{close:.2f},5000,100,{volume},1,1,F1,{lot},,,,,")


def synthetic_file(trade="2026-11-20", expiry="2026-11-24", forward=26000.0, spot=25990.0, sigma=0.12,
                   r=0.065, symbol="NIFTY", extra_symbol=True, zipped=True) -> bytes:
    from backend.options import black76
    T = (date.fromisoformat(expiry) - date.fromisoformat(trade)).days / 365
    lines = [HEADER]
    for strike in range(25500, 26550, 50):
        for opt in ("CE", "PE"):
            premium = round(black76.price(forward, strike, T, r, sigma, opt), 2)
            if premium >= 0.05:
                lines.append(_line(trade, "IDO", symbol, expiry, strike, opt, premium, spot))
    lines.append(_line(trade, "IDF", symbol, "2026-11-24", 0, "", forward + 5, spot))
    if extra_symbol:   # filtered out: not a configured symbol
        lines.append(_line(trade, "STO", "RELIANCE", expiry, 1500, "CE", 20.0, 1490.0))
    text = "\n".join(lines) + "\n"
    if not zipped:
        return text.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"BhavCopy_NSE_FO_0_0_0_{trade.replace('-', '')}_F_0000.csv", text)
    return buf.getvalue()


class Black76Tests(unittest.TestCase):
    def test_put_call_parity_and_iv_round_trip(self):
        from backend.options import black76
        F, K, T, r, sigma = 26000.0, 25800.0, 7 / 365, 0.065, 0.14
        c = black76.price(F, K, T, r, sigma, "CE")
        p = black76.price(F, K, T, r, sigma, "PE")
        self.assertAlmostEqual(c - p, math.exp(-r * T) * (F - K), places=6)
        self.assertAlmostEqual(black76.implied_vol(c, F, K, T, r, "CE"), sigma, places=4)
        self.assertAlmostEqual(black76.implied_vol(p, F, K, T, r, "PE"), sigma, places=4)
        self.assertAlmostEqual(black76.forward_from_parity(c, p, K, T, r), F, places=4)

    def test_greeks_signs_and_bounds(self):
        from backend.options import black76
        g_call = black76.greeks(26000, 26200, 5 / 365, 0.065, 0.12, "CE")
        g_put = black76.greeks(26000, 25800, 5 / 365, 0.065, 0.12, "PE")
        self.assertTrue(0 < g_call["delta"] < 0.5)
        self.assertTrue(-0.5 < g_put["delta"] < 0)
        self.assertGreater(g_call["gamma"], 0)
        self.assertGreater(g_call["vega"], 0)
        self.assertLess(g_call["theta"], 0)     # options lose value each day

    def test_iv_none_when_impossible(self):
        from backend.options import black76
        self.assertIsNone(black76.implied_vol(10, 26000, 25000, 5 / 365, 0.065, "CE"))   # below intrinsic
        self.assertIsNone(black76.implied_vol(10, 26000, 26000, 0, 0.065, "CE"))        # expiry day
        self.assertIsNone(black76.implied_vol(0, 26000, 26000, 5 / 365, 0.065, "CE"))


class ParseTests(unittest.TestCase):
    def test_parse_filters_symbols_and_types(self):
        from backend.options import bhavcopy
        text = bhavcopy.to_text(synthetic_file())
        trade, rows = bhavcopy.parse(text, ["NIFTY"])
        self.assertEqual(trade, date(2026, 11, 20))
        self.assertTrue(all(r["symbol"] == "NIFTY" for r in rows))
        kinds = {r["opt_type"] for r in rows}
        self.assertEqual(kinds, {"CE", "PE", "FUT"})
        fut = next(r for r in rows if r["opt_type"] == "FUT")
        self.assertEqual(fut["strike"], 0.0)
        self.assertEqual(rows[0]["lot_size"], 65)

    def test_plain_csv_and_bad_header(self):
        from backend.options import bhavcopy
        _, rows = bhavcopy.parse(bhavcopy.to_text(synthetic_file(zipped=False)), ["NIFTY"])
        self.assertTrue(rows)
        with self.assertRaises(bhavcopy.BhavcopyFormatError):
            bhavcopy.parse("a,b,c\n1,2,3\n", ["NIFTY"])

    def test_enrich_recovers_forward_and_iv(self):
        from backend.options import bhavcopy
        trade, rows = bhavcopy.parse(bhavcopy.to_text(synthetic_file(sigma=0.12)), ["NIFTY"])
        bhavcopy.enrich(rows, trade, 0.065)
        opts = [r for r in rows if r["opt_type"] != "FUT"]
        self.assertEqual(opts[0]["forward_source"], "parity")
        self.assertAlmostEqual(opts[0]["forward"], 26000.0, delta=2.0)
        near = [r for r in opts if abs(r["strike"] - 26000) <= 200 and r["iv"] is not None]
        self.assertTrue(near)
        for r in near:   # premiums are rounded to 0.01, so IV is close, not exact
            self.assertAlmostEqual(r["iv"], 0.12, delta=0.005)
        self.assertEqual(opts[0]["days_to_expiry"], 4)


class StoreAndJobTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.options.store import ensure_option_tables
        ensure_option_tables()

    def test_import_is_idempotent_and_chain_query(self):
        from backend.options import jobs, store
        first = jobs.import_file(synthetic_file(), "NSE")
        second = jobs.import_file(synthetic_file(), "NSE")
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["rows"], second["rows"])
        self.assertEqual(store.expiries("NIFTY", "2026-11-20"), ["2026-11-24"])
        rows = store.chain("NIFTY", "2026-11-20", "2026-11-24")
        self.assertTrue(all(r["opt_type"] in ("CE", "PE") for r in rows))
        self.assertEqual(store.latest_date("NIFTY"), "2026-11-20")
        hist = store.contract_history("NIFTY", "2026-11-24", 26000.0, "CE")
        self.assertEqual(len(hist), 1)

    def test_wrong_date_file_is_rejected(self):
        from backend.options import jobs, store
        result = jobs.load_day(date(2026, 11, 19), "NSE", fetch=lambda url: synthetic_file(trade="2026-11-20"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(store.file_status(date(2026, 11, 19), "NSE"), "error")

    def test_backfill_counts_missing_and_skips_existing(self):
        from backend.options import jobs, store
        seen = []

        def fetch(url):
            seen.append(url)
            if "BSE" in url:
                return None              # e.g. BSE not published yet
            day = url.split("_0_0_0_")[1][:8]
            return synthetic_file(trade=f"{day[:4]}-{day[4:6]}-{day[6:]}", expiry="2026-11-24")

        no_vix = lambda start, end: None   # noqa: E731
        result = jobs.backfill(date(2026, 11, 16), date(2026, 11, 20), fetch=fetch, vix_download=no_vix,
                               pause_seconds=0)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["counts"]["ok"], 5)          # Mon-Fri on NSE
        self.assertEqual(result["counts"]["missing"], 5)     # BSE
        self.assertIn("nsearchives.nseindia.com", seen[0])
        self.assertIn("20261116", seen[0])
        seen.clear()
        again = jobs.backfill(date(2026, 11, 16), date(2026, 11, 20), fetch=fetch, vix_download=no_vix,
                              pause_seconds=0)
        self.assertEqual(again["counts"]["exists"], 5)
        self.assertEqual(len(seen), 5)                        # only the missing BSE days are retried
        summary = store.summary()
        self.assertEqual(summary["exchanges"]["NSE"]["ok"], 5)

    def test_vix_saved_from_frame(self):
        import pandas as pd
        from backend.options import jobs, store
        frame = pd.DataFrame({"Open": [14.0], "High": [15.0], "Low": [13.5], "Close": [14.6]},
                             index=pd.to_datetime(["2026-11-20"]))
        result = jobs.update_vix(date(2026, 11, 20), date(2026, 11, 20), download=lambda s, e: frame)
        self.assertEqual(result, {"status": "ok", "days": 1})
        self.assertEqual(store.vix_on("2026-11-23"), 14.6)

    def test_daily_update_complete_only_with_today(self):
        from backend.options import jobs

        def fetch(url):
            day = url.split("_0_0_0_")[1][:8]
            text_day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            data = synthetic_file(trade=text_day, zipped=False)
            return data if "NSE" in url else data.replace(b"NIFTY", b"SENSEX")

        no_vix = lambda start, end: None   # noqa: E731
        result = jobs.daily_update(now=datetime(2026, 11, 20, 19, 5), fetch=fetch, vix_download=no_vix,
                                   pause_seconds=0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["days"], 7)


class SchedulerTimingTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("TRADINGAGENTS_OPTION_DATA")
        os.environ["TRADINGAGENTS_OPTION_DATA"] = "on"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TRADINGAGENTS_OPTION_DATA", None)
        else:
            os.environ["TRADINGAGENTS_OPTION_DATA"] = self._old

    def test_eod_due_window(self):
        from backend.options.jobs import eod_due
        friday = date(2026, 11, 20)
        self.assertFalse(eod_due(datetime(2026, 11, 20, 18, 59), None, None))
        self.assertTrue(eod_due(datetime(2026, 11, 20, 19, 0), None, None))
        self.assertFalse(eod_due(datetime(2026, 11, 20, 19, 10), friday, None))          # done today
        self.assertFalse(eod_due(datetime(2026, 11, 20, 19, 10), None, datetime(2026, 11, 20, 19, 0)))
        self.assertTrue(eod_due(datetime(2026, 11, 20, 19, 31), None, datetime(2026, 11, 20, 19, 0)))
        self.assertFalse(eod_due(datetime(2026, 11, 20, 23, 45), None, None))           # after the window
        self.assertFalse(eod_due(datetime(2026, 11, 21, 19, 30), None, None))           # Saturday

    def test_off_switch(self):
        from backend.options.jobs import eod_due
        os.environ["TRADINGAGENTS_OPTION_DATA"] = "off"
        self.assertFalse(eod_due(datetime(2026, 11, 20, 19, 30), None, None))


class ApiTests(IsolatedStateTestCase, unittest.TestCase):
    def test_upload_status_and_chain(self):
        with fresh_test_client() as client:
            login(client)
            response = client.post("/api/options/upload?exchange=NSE", content=synthetic_file(),
                                   headers=csrf_headers(client))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["trade_date"], "2026-11-20")
            status = client.get("/api/options/status").json()
            self.assertEqual(status["exchanges"]["NSE"]["ok"], 1)
            chain = client.get("/api/options/chain?symbol=NIFTY").json()
            self.assertEqual(chain["date"], "2026-11-20")
            self.assertEqual(chain["expiry"], "2026-11-24")
            self.assertEqual(chain["atm_strike"], 26000.0)
            row = next(r for r in chain["rows"] if r["strike"] == 26000.0)
            self.assertIn("CE", row)
            self.assertIn("PE", row)
            bad = client.post("/api/options/upload?exchange=NSE", content=b"x,y\n1,2\n",
                              headers=csrf_headers(client))
            self.assertEqual(bad.status_code, 400)

    def test_backfill_validation(self):
        with fresh_test_client() as client:
            login(client)
            response = client.post("/api/options/backfill", json={"start_date": "2026-11-20", "end_date": "2026-11-10"},
                                   headers=csrf_headers(client))
            self.assertEqual(response.status_code, 400)
            response = client.post("/api/options/backfill", json={"start_date": "2020-01-01", "end_date": "2026-11-10"},
                                   headers=csrf_headers(client))
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
