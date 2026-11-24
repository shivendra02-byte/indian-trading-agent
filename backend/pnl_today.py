"""Today's P&L: your own futures trades + the system's paper trades (asked 24 Nov 2026).

Two books, each split into realised (ended today) and unrealised (still open):

* **Yours**: (a) My futures trades (``my_trades``): closed today at your exit price; a stop or
  target hit today but not yet closed by you, at the hit price ("awaiting your exit"); open trades
  at the last close of the bars they trail on. (b) Journal trades you marked **Taken** (asked 24
  Nov 2026): your fill → your exit, with the lots the rules sized (no charges); counted on the day
  the rules' version of the trade ended, or as open at the last close until you enter your exit.
* **System paper trades**: journal records you were alerted on — AI-approved, or sent as
  "rules only" (AI review failed or over budget; ``panel.was_alerted``) — closed today with ₹
  after charges, open at the last close the tracker saw; plus approved option spreads closed
  today or open at the last evening value. Shadow strategies are never alerted.

The overall total counts a trade you took once (your numbers), not again as a paper trade.

Nothing is stored: the numbers are read from the tables the trackers keep current, so the card
changes as soon as a tracker marks a trade ended (after each bar sync for futures/MCX, the daily
close for cash swing, the evening option files for spreads).
"""

from __future__ import annotations

from datetime import date, datetime

from backend.db import get_db

SEGMENT_LABELS = {"stock_futures": "Stock futures", "index_futures": "Index futures", "mcx": "MCX",
                  "cash_swing": "Cash swing", "cup_handle": "Cup & handle", "wolfe": "Wolfe wave",
                  "nifty_options": "NIFTY options", "option_spreads": "Option spreads"}


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _r(x) -> float | None:
    return None if x is None else round(float(x), 2)


def my_trade_rows(today: date) -> list[dict]:
    from backend import my_trades
    my_trades.ensure_table()
    iso = today.isoformat()
    out = []
    for t in my_trades.list_trades(limit=1000):
        base = {"book": "mine", "id": t["id"], "name": t["contract"], "segment": "My futures",
                "direction": t["direction"], "lots": t["lots"], "entry": t["entry_price"]}
        if t["status"] == "closed" and (t["exit_time"] or "")[:10] == iso:
            out.append({**base, "state": "closed", "at": t["exit_time"], "price": t["exit_price"],
                        "r": _r(t["realised_r"]), "pnl": _r(t["realised_pnl"])})
        elif t["status"] in ("stop_hit", "target_hit") and (t["hit_time"] or "")[:10] == iso:
            out.append({**base, "state": "awaiting_exit", "at": t["hit_time"], "price": t["hit_price"],
                        "r": _r(t["open_r"]), "pnl": _r(t["open_pnl"]),
                        "note": f"{t['status'].replace('_', ' ')} — enter your exit price on My trades"})
        elif t["status"] == "open":
            out.append({**base, "state": "open", "at": t["last_bar_ts"], "price": t["last_close"],
                        "r": _r(t["open_r"]), "pnl": _r(t["open_pnl"]),
                        "note": None if t["last_close"] is not None else (t.get("data_note") or "no bars yet")})
    return out


def taken_rows(today: date) -> list[dict]:
    """Journal trades you marked Taken, with your fill and exit."""
    from backend.journal.store import list_records
    iso = today.isoformat()
    out = []
    recs = list_records("user_action = 'taken' AND user_fill_price IS NOT NULL AND (track_status = 'open' OR "
                        "(track_status = 'closed' AND substr(exit_ts, 1, 10) = ?))", (iso,), limit=1000)
    for rec in recs:
        s = 1 if rec["direction"] == "long" else -1
        risk = abs(rec["entry"] - rec["stop"])
        fill, exit_price = rec["user_fill_price"], rec.get("user_exit_price")
        qty, per_point = _qty(rec)
        base = {"book": "mine", "id": rec["id"], "name": rec["symbol"], "direction": rec["direction"],
                "segment": f"{SEGMENT_LABELS.get(rec['segment'], rec['segment'])} (journal)", "setup": rec["setup"],
                "entry": fill}
        if exit_price:
            out.append({**base, "state": "closed", "at": rec.get("exit_ts"), "price": exit_price,
                        "r": _r(s * (exit_price - fill) / risk) if risk else None,
                        "pnl": _r(s * (exit_price - fill) * qty * per_point), "note": rec.get("user_note")})
            continue
        last = ((rec.get("extra") or {}).get("stop_now") or {}).get("last_close")
        if rec["track_status"] == "closed":
            out.append({**base, "state": "awaiting_exit", "at": rec.get("exit_ts"), "price": rec.get("exit_price"),
                        "r": _r(s * (rec["exit_price"] - fill) / risk) if risk and rec.get("exit_price") else None,
                        "pnl": _r(s * (rec["exit_price"] - fill) * qty * per_point) if rec.get("exit_price") else None,
                        "note": f"rules exit ({rec.get('exit_reason') or 'closed'}) — enter your exit on the Trade Journal"})
        else:
            out.append({**base, "state": "open", "at": ((rec.get("extra") or {}).get("stop_now") or {}).get("at"),
                        "price": last, "r": _r(s * (last - fill) / risk) if risk and last is not None else None,
                        "pnl": _r(s * (last - fill) * qty * per_point) if last is not None else None,
                        "note": None if last is not None else "no bars yet"})
    return out


def _qty(rec: dict) -> tuple[float, float]:
    """(quantity, ₹ per point) the rules sized the trade with."""
    from backend.journal.tracker import _size
    fill = rec.get("fill_price") or rec["entry"]
    risk = abs(fill - rec["stop"])
    if rec.get("qty"):
        per_point = 1.0 if rec["segment"] in ("cash_swing", "cup_handle", "wolfe") else \
            float((rec.get("extra") or {}).get("value_per_point") or 1)
        return rec["qty"], per_point
    return _size(rec, fill, risk) if risk else (0, 1.0)


def _journal_open_pnl(rec: dict) -> tuple[float | None, float | None, float | None]:
    """(R, ₹, last close) for an open approved record, from the tracker's last stop_now."""
    from backend.journal.tracker import _size
    now = (rec.get("extra") or {}).get("stop_now") or {}
    last, fill = now.get("last_close"), rec.get("fill_price")
    if last is None or fill is None:
        return None, None, last
    s = 1 if rec["direction"] == "long" else -1
    risk = s * (fill - rec["stop"])
    if risk <= 0:
        return None, None, last
    qty, per_point = _size(rec, fill, risk)
    return s * (last - fill) / risk, s * (last - fill) * qty * per_point, last


def system_rows(today: date) -> list[dict]:
    from backend.journal.store import list_records
    iso = today.isoformat()
    out = []
    from backend.journal.panel import was_alerted
    recs = list_records("decision IN ('approved', 'shortlisted') AND (track_status IN ('pending', 'open') OR "
                        "(track_status = 'closed' AND substr(exit_ts, 1, 10) = ?))", (iso,), limit=1000)
    for rec in recs:
        if not was_alerted(rec):
            continue
        base = {"book": "system", "id": rec["id"], "name": rec["symbol"], "direction": rec["direction"],
                "segment": SEGMENT_LABELS.get(rec["segment"], rec["segment"]), "setup": rec["setup"],
                "entry": rec.get("fill_price") or rec["entry"],
                "alert": "AI approved" if rec["decision"] == "approved" else "rules only",
                "taken": rec.get("user_action") == "taken"}
        if rec["track_status"] == "closed":
            out.append({**base, "state": "closed", "at": rec["exit_ts"], "price": rec["exit_price"],
                        "r": _r(rec["pnl_r"]), "pnl": _r(rec["pnl_rupees"]), "note": rec.get("exit_reason")})
        elif rec["track_status"] == "open":
            r, pnl, last = _journal_open_pnl(rec)
            out.append({**base, "state": "open", "at": ((rec.get("extra") or {}).get("stop_now") or {}).get("at"),
                        "price": last, "r": _r(r), "pnl": _r(pnl)})
        else:
            out.append({**base, "state": "pending", "at": rec.get("fill_until"), "price": rec["entry"],
                        "r": None, "pnl": None, "note": "waiting for the limit entry"})
    with get_db() as conn:
        has_spreads = _table_exists(conn, "option_spreads")
    if has_spreads:
        from backend.options.spreads import pnl as spread_pnl, settings as spread_settings, spreads
        cfg = spread_settings()
        for sp in spreads():
            if sp["decision"] != "approved":
                continue
            base = {"book": "system", "id": sp["id"], "name": f"{sp['symbol']} {sp['strategy'].replace('_', ' ')}",
                    "segment": SEGMENT_LABELS["option_spreads"], "direction": "credit", "entry": sp["credit"]}
            if sp["status"] == "closed" and sp["exit_date"] == iso:
                out.append({**base, "state": "closed", "at": sp["exit_date"], "price": sp["exit_value"],
                            "r": _r(sp["pnl_r"]), "pnl": _r(sp["pnl_inr"]), "note": sp.get("exit_reason")})
            elif sp["status"] == "open":
                p = spread_pnl(sp, sp["last_value"], cfg) if sp.get("last_value") is not None else {}
                out.append({**base, "state": "open", "at": sp["signal_date"], "price": sp.get("last_value"),
                            "r": _r(p.get("pnl_r")), "pnl": _r(p.get("pnl_inr"))})
    return out


def _totals(rows: list[dict]) -> dict:
    done = [x for x in rows if x["state"] in ("closed", "awaiting_exit")]
    live = [x for x in rows if x["state"] == "open"]

    def total(xs, key):
        vals = [x[key] for x in xs if x[key] is not None]
        return round(sum(vals), 2) if vals else 0.0
    realised, unrealised = total(done, "pnl"), total(live, "pnl")
    return {"realised_pnl": realised, "realised_r": total(done, "r"), "unrealised_pnl": unrealised,
            "unrealised_r": total(live, "r"), "total_pnl": round(realised + unrealised, 2),
            "ended_today": len(done), "wins": len([x for x in done if (x["pnl"] or 0) > 0]),
            "open": len(live), "pending": len([x for x in rows if x["state"] == "pending"])}


def today_summary(today: date | None = None) -> dict:
    today = today or date.today()
    mine, system = my_trade_rows(today) + taken_rows(today), system_rows(today)
    order = {"closed": 0, "awaiting_exit": 1, "open": 2, "pending": 3}
    for rows in (mine, system):
        rows.sort(key=lambda x: (order[x["state"]], str(x.get("at") or "")))
    both = _totals(mine + [x for x in system if not x.get("taken")])     # a trade you took counts once
    return {"date": today.isoformat(), "as_of": datetime.now().isoformat(timespec="seconds"),
            "mine": {"rows": mine, **_totals(mine)}, "system": {"rows": system, **_totals(system)}, "total": both}
