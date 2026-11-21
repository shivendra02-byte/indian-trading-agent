"""Hourly scans: NSE futures (stock + index) and MCX minis.

Runs after each completed 1h candle (scheduler jobs ``scan:nse_1h`` and
``scan:mcx_1h``). Entry TF 1h on the traded contract; context 4H (resampled
from 1h) and daily. Same rule engine, scoring and journal as cash swing, plus
the futures-specific hard filters:

* F&O ban (stock futures) — banned or unknown → no trade
* Liquidity — live bid-ask spread ≤ 0.1% from a Kite quote; unverifiable → no trade
* Daily trend must not be against the trade (4H is the scored higher timeframe)
* Sizing — if 1 lot risks more than 1% (₹10,000) → no trade
* Events — RBI/Budget/results inside the 3-day hold (NSE); US CPI / FOMC
  (gold, silver) and US inventory windows (crude, gas) at entry (MCX)
* Timing — no signal whose entry window would start in the last 30 minutes

Entries: limit at the signal close, valid for the next 2 hourly bars.
Time stop: close of the 3rd trading day, and never past the day before expiry.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from backend.journal.store import add_records as journal_add
from backend.journal.tracker import track_open_records
from backend.marketdata.candles import get_candles, resample_4h
from backend.marketdata.instruments import active_future, ban_status, fno_universe, spot_instrument
from backend.marketdata.store import MCX_UNDERLYINGS, SCANNED_INDICES
from backend.signals import events
from backend.signals.cash_swing import (
    _finish_run, _save, _start_run, data_quality, ensure_scan_tables, expected_last_daily_bar,
)
from backend.signals.regime import current_regime
from backend.signals.setups import SHORTLIST_SCORE, _relative_strength_points, evaluate, htf_trend
from backend.system_state import is_paused
from tradingagents.utils.market_calendar import IST, MCX, NSE, session_windows

HOLD_DAYS = 3
ENTRY_BARS = 2
LAST_ENTRY_BEFORE_CLOSE = timedelta(minutes=30)
MAX_SPREAD_PCT = 0.1
RISK_PER_TRADE = 10_000
SHORTLIST_SIZE = 5
MIN_HOURLY_BARS = 60
# MCX quote is per 10 g / per kg / per barrel / per mmBtu; ₹ value of a 1-point move per lot.
MCX_VALUE_PER_POINT = {"GOLDM": 10, "SILVERM": 5, "CRUDEOILM": 10, "NATGASMINI": 250}
GROUPS = {"nse": "fno_1h", "mcx": "mcx_1h"}


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def exchange_days_after(exchange: str, start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if session_windows(exchange, d):
            out.append(d)
    return out


def latest_hourly_bar_start(exchange: str, now: datetime) -> datetime | None:
    """Start time of the last completed 1h bar today (Kite stamps bars by start)."""
    from backend.scheduler import CANDLE_BUFFER, bar_ends
    ends = [e for e in bar_ends(exchange, "60minute", now.date()) if e + CANDLE_BUFFER <= now]
    if not ends:
        return None
    end = ends[-1]
    for open_t, close_t in session_windows(exchange, now.date()):
        start = datetime.combine(now.date(), open_t)
        while start + timedelta(hours=1) < end:
            start += timedelta(hours=1)
        if start < end <= datetime.combine(now.date(), close_t):
            return start
    return None


def hourly_quality(bars: list[dict], expected_start: datetime, exchange: str, need_oi: bool) -> str | None:
    if len(bars) < MIN_HOURLY_BARS:
        return f"only {len(bars)} hourly bars (need {MIN_HOURLY_BARS})"
    last = datetime.fromisoformat(bars[-1]["ts"])
    if last != expected_start:
        return f"stale data: last hourly bar {last:%Y-%m-%d %H:%M}, expected {expected_start:%H:%M}"
    if not bars[-1].get("volume"):
        return "no volume on the last bar"
    if need_oi and not bars[-1].get("oi"):
        return "no open interest on the last bar"
    # No missing bars over the last 20: consecutive bars within a day are 1h apart.
    recent = [datetime.fromisoformat(b["ts"]) for b in bars[-20:]]
    for a, b in zip(recent, recent[1:]):
        if a.date() == b.date() and b - a != timedelta(hours=1):
            return f"missing hourly bar after {a:%Y-%m-%d %H:%M}"
    return None


def oi_signal(bars: list[dict], n: int = 3) -> dict:
    first, last = bars[-n - 1], bars[-1]
    oi_change = 100 * (last["oi"] / first["oi"] - 1) if first.get("oi") else None
    return {"change_pct": round(oi_change, 2) if oi_change is not None else None,
            "price_change": last["close"] - first["close"]}


def check_spreads(client, tradingsymbols: list[str]) -> dict[str, float | None]:
    """Bid-ask spread % per 'EXCHANGE:SYMBOL' from one Kite quote call; None if unavailable."""
    if not tradingsymbols:
        return {}
    try:
        quotes = client.quote(tradingsymbols)
    except Exception:
        return {s: None for s in tradingsymbols}
    out = {}
    for sym in tradingsymbols:
        depth = (quotes.get(sym) or {}).get("depth") or {}
        try:
            bid = depth["buy"][0]["price"]
            ask = depth["sell"][0]["price"]
            mid = (bid + ask) / 2
            out[sym] = round(100 * (ask - bid) / mid, 4) if bid > 0 and ask > 0 else None
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            out[sym] = None
    return out


def _instruments(group: str, today: date) -> list[dict]:
    """(segment, symbol, contract row, spot row or None, value per point, ban)."""
    out = []
    if group == "nse":
        ban = ban_status(today)
        banned = set(ban["symbols"])
        for stock in fno_universe(today):
            fut = active_future(stock["symbol"], today=today)
            if not fut:
                continue
            out.append({"segment": "stock_futures", "symbol": stock["symbol"], "contract": fut,
                        "spot": spot_instrument(stock["symbol"]), "value_per_point": fut["lot_size"],
                        "ban": "unknown" if ban["status"] != "ok" else stock["symbol"] in banned})
        for name in SCANNED_INDICES:
            fut = active_future(name, today=today)
            if fut:
                out.append({"segment": "index_futures", "symbol": name, "contract": fut,
                            "spot": spot_instrument(name), "value_per_point": fut["lot_size"], "ban": False})
    else:
        for name in MCX_UNDERLYINGS:
            fut = active_future(name, segment="MCX-FUT", today=today)
            if fut:
                out.append({"segment": "mcx", "symbol": name, "contract": fut, "spot": None,
                            "value_per_point": MCX_VALUE_PER_POINT[name], "ban": False})
    return out


def run_hourly_scan(group: str, now: datetime | None = None, client=None, force: bool = False) -> dict:
    if group not in GROUPS:
        raise ValueError(group)
    segment_key = GROUPS[group]
    exchange = NSE if group == "nse" else MCX
    ensure_scan_tables()
    now = now or _now()
    tracked = track_open_records()
    if is_paused():
        return {"status": "skipped", "reason": "paused", "tracking": tracked}
    windows = session_windows(exchange, now.date())
    if not windows:
        return {"status": "skipped", "reason": f"{exchange} closed today", "tracking": tracked}
    bar_start = latest_hourly_bar_start(exchange, now)
    if bar_start is None:
        return {"status": "skipped", "reason": "no completed hourly bar yet", "tracking": tracked}
    signal_end = min(bar_start + timedelta(hours=1), datetime.combine(now.date(), windows[-1][1]))
    cutoff = datetime.combine(now.date(), windows[-1][1]) - LAST_ENTRY_BEFORE_CLOSE
    if signal_end >= cutoff and not force:
        return {"status": "skipped", "reason": "entry window would start in the last 30 minutes",
                "tracking": tracked}

    run_id = _start_run(now.date().isoformat(), now, segment=segment_key)
    hold = exchange_days_after(exchange, now.date() - timedelta(days=1), HOLD_DAYS)  # today + next 2
    daily_expected = expected_last_daily_bar(now) if exchange == NSE else None

    regime_nse = None
    nifty_daily: list[dict] = []
    if exchange == NSE:
        nifty = spot_instrument("NIFTY")
        nifty_daily = get_candles(nifty["instrument_token"], "day", limit=300) if nifty else []
        problem = data_quality(nifty_daily, daily_expected) if nifty else "NIFTY instrument missing"
        if problem:
            return _finish_run(run_id, "blocked", {"reason": f"NIFTY data: {problem}", "tracking": tracked})
        market = events.market_events_between(hold[0], hold[-1], kinds=("RBI", "BUDGET"))
        if not market["verified"]:
            return _finish_run(run_id, "blocked", {"reason": "event calendar not verified for the holding window"})
        if market["events"]:
            names = ", ".join(f"{e['event']} {e['date']}" for e in market["events"])
            return _finish_run(run_id, "blocked", {"reason": f"event inside the holding window: {names}",
                                                   "tracking": tracked})
        regime_nse = current_regime(nifty_daily)

    instruments = _instruments(group, now.date())
    if not instruments:
        return _finish_run(run_id, "blocked", {"reason": "no active contracts (instrument master missing)"})

    rows: list[dict] = []
    for inst in instruments:
        sym, contract, seg = inst["symbol"], inst["contract"], inst["segment"]
        base = {"symbol": sym, "segment": seg, "token": contract["instrument_token"],
                "tradingsymbol": contract["tradingsymbol"]}
        if inst["ban"] is True:
            rows.append({**base, "valid": False, "reject_reason": "in F&O ban"})
            continue
        if inst["ban"] == "unknown":
            rows.append({**base, "valid": False, "reject_reason": "F&O ban list not verified today"})
            continue
        hourly = get_candles(contract["instrument_token"], "60minute", limit=400)
        problem = hourly_quality(hourly, bar_start, exchange, need_oi=True)
        if problem:
            rows.append({**base, "valid": False, "reject_reason": problem})
            continue
        if exchange == NSE:
            daily = get_candles(inst["spot"]["instrument_token"], "day", limit=300) if inst["spot"] else []
            problem = data_quality(daily, daily_expected) if inst["spot"] else "no spot instrument"
            regime = regime_nse
        else:
            daily = get_candles(contract["instrument_token"], "day", limit=300)
            problem = None if len(daily) >= 200 else f"only {len(daily)} daily bars"
            if not problem and (now.date() - date.fromisoformat(daily[-1]["ts"][:10])).days > 4:
                problem = f"stale daily bars (last {daily[-1]['ts'][:10]})"
            regime = current_regime(daily) if not problem else None
        if problem:
            rows.append({**base, "valid": False, "reject_reason": f"daily data: {problem}"})
            continue
        if seg == "stock_futures":
            ok, why = events.results_clear(sym, hold[0], hold[-1], now.date())
            if not ok:
                rows.append({**base, "valid": False, "reject_reason": why})
                continue
        if seg == "mcx":
            blocked, why = events.mcx_event_block(sym, signal_end)
            if blocked:
                rows.append({**base, "valid": False, "reject_reason": why})
                continue

        daily_trend = htf_trend(daily) or 0.0
        rs = None
        if seg == "stock_futures":
            rs = {d: _relative_strength_points(daily, nifty_daily, 1 if d == "long" else -1)
                  for d in ("long", "short")}
        oi = oi_signal(hourly)
        candidates = evaluate(hourly, resample_4h(hourly, exchange), regime["regime"],
                              is_stock=seg == "stock_futures", rs_override=rs, oi=oi)
        trackable = []
        for c in candidates:
            if not c.get("trackable"):
                continue
            s = 1 if c["direction"] == "long" else -1
            if daily_trend * s < 0:
                c.update(trackable=False, valid=False, reject_reason="daily trend against the trade")
                continue
            lots = int(RISK_PER_TRADE // (c["risk_per_unit"] * inst["value_per_point"])) if c["risk_per_unit"] else 0
            if lots < 1:
                c.update(trackable=False, valid=False, reject_reason="1 lot risks more than 1%")
                continue
            c["lots"] = lots
            trackable.append(c)
        if trackable:
            for c in trackable:
                rows.append({**base, **c, "oi_change_pct": oi["change_pct"],
                             "value_per_point": inst["value_per_point"], "expiry": contract["expiry"]})
        else:
            reasons = "; ".join(sorted({c["reject_reason"] for c in candidates if c.get("reject_reason")}))
            rows.append({**base, "valid": False, "regime": regime["regime"], "reject_reason": reasons})

    # Liquidity: one quote call for every trackable contract; unverifiable spread → reject.
    tracked_rows = [r for r in rows if r.get("trackable")]
    if tracked_rows:
        from backend.brokers.kite import get_authenticated_client
        prefix = "MCX" if exchange == MCX else "NFO"
        try:
            client = client or get_authenticated_client()
            spreads = check_spreads(client, sorted({f"{prefix}:{r['tradingsymbol']}" for r in tracked_rows}))
        except Exception:
            spreads = {}
        for r in tracked_rows:
            spread = spreads.get(f"{prefix}:{r['tradingsymbol']}")
            r["spread_pct"] = spread
            if spread is None or spread > MAX_SPREAD_PCT:
                r.update(trackable=False, valid=False,
                         reject_reason="liquidity not verified" if spread is None
                         else f"bid-ask spread {spread:.2f}% > {MAX_SPREAD_PCT}%")

    ranked = sorted((r for r in rows if r.get("valid") and r.get("trackable") and r["score"] >= SHORTLIST_SCORE),
                    key=lambda r: r["score"], reverse=True)
    for r in ranked[:SHORTLIST_SIZE]:
        r["shortlisted"] = True
    _save(run_id, now.date().isoformat(), rows, segment=segment_key)

    fill_until = min(signal_end + timedelta(hours=ENTRY_BARS), cutoff)
    journal_rows = []
    for r in rows:
        if not r.get("trackable"):
            continue
        expiry = date.fromisoformat(r["expiry"])
        days = [d for d in hold if d < expiry] or [hold[0]]
        journal_rows.append(_journal_row(r, now.date(), bar_start, fill_until, days[-1], exchange))
    journal_add(journal_rows)
    return _finish_run(run_id, "ok", {
        "signal_bar": bar_start.isoformat(timespec="minutes"), "regime": regime_nse,
        "instruments": len(instruments), "valid_setups": sum(1 for r in rows if r.get("valid")),
        "above_60": len(ranked), "shortlisted": [r["symbol"] for r in ranked[:SHORTLIST_SIZE]],
        "journal_records": len(journal_rows), "tracking": tracked,
    })


def _journal_row(r: dict, day: date, bar_start: datetime, fill_until: datetime, time_stop: date,
                 exchange: str) -> dict:
    if not r.get("valid"):
        decision = "soft_rejected"
    elif r.get("shortlisted"):
        decision = "shortlisted"
    elif r["score"] >= SHORTLIST_SCORE:
        decision = "not_selected"
    else:
        decision = "below_score"
    return {
        "segment": r["segment"], "symbol": r["symbol"], "instrument_token": r["token"],
        "exchange": "MCX" if exchange == MCX else "NFO", "signal_date": day.isoformat(),
        "signal_ts": bar_start.isoformat(timespec="seconds"), "setup": r["setup"], "direction": r["direction"],
        "regime": r.get("regime"), "score": r.get("score"), "factors": r.get("factors"),
        "soft_checks": r.get("soft_checks"), "decision": decision, "reject_reason": r.get("reject_reason"),
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": r.get("atr"),
        "entry_date": day.isoformat(), "fill_until": fill_until.isoformat(timespec="seconds"),
        "time_stop_date": time_stop.isoformat(),
        "extra": {k: r.get(k) for k in ("tradingsymbol", "expiry", "lots", "value_per_point", "oi_change_pct",
                                         "spread_pct", "stop_atr", "next_level", "vol_ratio", "htf_trend",
                                         "rs_vs_nifty_pct", "risk_mult")},
    }
