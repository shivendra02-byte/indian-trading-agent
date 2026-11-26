"""Cash universe: F&O stocks + the NIFTY 500, ASM/GSM skip (agreed 26 Nov 2026)."""

import unittest
from datetime import date

from tests._support import IsolatedStateTestCase


def nifty500_csv(n=450, extra=("NEWCO", "OTHERCO")):
    lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    lines += [f"Company {i},Industry,SYM{i},EQ,INE{i:09d}" for i in range(n)]
    lines += [f"{s} Ltd,Industry,{s},EQ,INE000000XX" for s in extra]
    return "\n".join(lines) + "\n"


SURV = ("ScripCode,Symbol,NSE Exclusive,Status,Series,GSM,Long_Term_Additional_Surveillance_Measure (Long Term ASM),"
        "Short_Term_Additional_Surveillance_Measure (Short Term ASM)\n"
        "1,NEWCO,N,A,EQ,100,100,100\n"
        "2,OTHERCO,N,A,EQ,100,2,100\n"
        "3,FNOCO,N,A,EQ,0,100,100\n")


class ParseTests(unittest.TestCase):
    def test_parse_index_csv(self):
        from backend.marketdata.cash_universe import parse_index_csv
        rows = parse_index_csv("﻿" + nifty500_csv(3))
        self.assertEqual([r["symbol"] for r in rows], ["SYM0", "SYM1", "SYM2", "NEWCO", "OTHERCO"])

    def test_parse_surveillance(self):
        from backend.marketdata.cash_universe import parse_surveillance
        flagged = parse_surveillance(SURV)
        self.assertEqual(set(flagged), {"OTHERCO", "FNOCO"})                # 100 = not under the measure
        self.assertIn("Long Term ASM", flagged["OTHERCO"][0])
        with self.assertRaises(ValueError):
            parse_surveillance("Symbol,Price\nX,1\n")

    def test_select_rows_keeps_extra_equities(self):
        from backend.marketdata.instruments import select_rows
        nfo = [{"segment": "NFO-FUT", "name": f"S{i}", "instrument_token": i} for i in range(120)]
        nse = [{"segment": "NSE", "instrument_type": "EQ", "tradingsymbol": s, "instrument_token": 1000 + i}
               for i, s in enumerate(["S1", "NEWCO", "JUNK"])]
        rows = select_rows(nfo, nse, [], extra_equities=["newco"])
        kept = {r["tradingsymbol"] for r in rows if r.get("segment") == "NSE"}
        self.assertEqual(kept, {"S1", "NEWCO"})


class UniverseTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.marketdata import ensure_marketdata_tables
        ensure_marketdata_tables()
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (501, "NSE", "NSE", "FNOCO", "FNOCO", "EQ", None, 0, 1, 0.05, "2026-11-26"),
                (601, "NFO", "NFO-FUT", "FNOCO26DECFUT", "FNOCO", "FUT", "2099-12-31", 0, 250, 0.05, "2026-11-26"),
                (502, "NSE", "NSE", "NEWCO", "NEWCO", "EQ", None, 0, 1, 0.05, "2026-11-26"),
                (503, "NSE", "NSE", "OTHERCO", "OTHERCO", "EQ", None, 0, 1, 0.05, "2026-11-26")])

    def test_refresh_list_monthly_and_keeps_old_on_bad_file(self):
        from backend.marketdata.cash_universe import list_status, refresh_nifty500
        today = date(2026, 11, 26)
        res = refresh_nifty500(fetch=lambda url: nifty500_csv(), today=today)
        self.assertEqual(res["status"], "ok", res)
        self.assertEqual(list_status()["symbols"], 452)
        self.assertEqual(refresh_nifty500(fetch=lambda url: 1 / 0, today=date(2026, 12, 10))["status"], "fresh")
        bad = refresh_nifty500(fetch=lambda url: nifty500_csv(10), today=date(2027, 1, 5))   # too short
        self.assertEqual(bad["status"], "error")
        self.assertEqual(list_status()["symbols"], 452)                     # old list kept

    def test_universe_flags_and_floor(self):
        from backend.marketdata.cash_universe import (cash_universe, import_surveillance, min_turnover_for,
                                                      refresh_nifty500)
        today = date(2026, 11, 26)
        refresh_nifty500(fetch=lambda url: nifty500_csv(400, extra=("NEWCO", "OTHERCO", "FNOCO")), today=today)
        u = {s["symbol"]: s for s in cash_universe(today)}
        self.assertTrue(u["FNOCO"]["fno"])
        self.assertFalse(u["NEWCO"]["fno"])
        self.assertNotIn("SYM1", u)                                         # no NSE instrument → left out
        self.assertEqual(u["NEWCO"]["surveillance_status"], "unknown")
        import_surveillance(SURV, trade_date=today)
        u = {s["symbol"]: s for s in cash_universe(today)}
        self.assertIsNone(u["NEWCO"]["surveillance"])
        self.assertTrue(u["OTHERCO"]["surveillance"])
        self.assertTrue(u["FNOCO"]["surveillance"])                         # GSM stage 0 = in GSM
        self.assertEqual(min_turnover_for(u["FNOCO"], 50e7), 50e7)
        self.assertEqual(min_turnover_for(u["NEWCO"], 50e7), 20e7)

    def test_surveillance_download_tries_recent_days(self):
        from backend.marketdata.cash_universe import refresh_surveillance, surveillance_status
        seen = []

        def fetch(url):
            seen.append(url)
            if "261126" in url:
                raise OSError("not published yet")
            return SURV
        res = refresh_surveillance(fetch=fetch, today=date(2026, 11, 26))
        self.assertEqual((res["status"], res["trade_date"]), ("ok", "2026-11-25"))
        self.assertIn("REG1_IND261126.csv", seen[0])
        self.assertEqual(surveillance_status(date(2026, 11, 26))["status"], "ok")
        self.assertEqual(surveillance_status(date(2026, 12, 5))["status"], "unknown")   # too old


if __name__ == "__main__":
    unittest.main()
