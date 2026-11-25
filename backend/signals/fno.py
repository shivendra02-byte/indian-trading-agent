"""Hourly scans: NSE futures (stock + index) and MCX minis.

Runs after each completed 1h candle (scheduler jobs ``scan:nse_1h`` and
``scan:mcx_1h``). Entry TF 1h on the traded contract; context 4H (resampled
from 1h) and daily. Same rule engine, scoring and journal as cash swing, plus
the futures-specific hard filters:

* F&O ban (stock futures) — banned or unknown → no trade
* Liquidity — live bid-ask spread ≤ 0.1% from a Kite quote; unverifiable → no trade
* Daily trend must not be against the trade (4H is the scored higher timeframe)
* Sizing — if 1 lot risks more than 1% (₹10,000) → no trade
* Events — RBI/Budget/results within 3 trading days of entry (NSE); US CPI / FOMC
  (gold, silver) and US inventory windows (crude, gas) at entry (MCX)
* Timing — no signal whose entry window would start in the last 30 minutes

Entries: limit at the signal close, valid for the next 2 hourly bars.
Exits: target, stop, or the managed stop from the instrument config (breakeven at 1R, then a
2 ATR trailing stop); no time stop (agreed 23 Nov 2026). Never held past the session before expiry.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from backend.decision.config import get_config
from backend.journal.store import add_records as journal_add
from backend.journal.tracker import track_open_records
from backend.marketdata.candles import bars_for_timeframe, get_candles
from backend.marketdata.instruments import active_future, ban_status, fno_universe, spot_instrument
from backend.marketdata.store import MCX_UNDERLYINGS, SCANNED_INDICES
from backend.signals import events, mcx_trend
from backend.signals.cash_swing import (
    _finish_run, _save, _start_run, data_quality, ensure_scan_tables, expected_last_daily_bar,
)
from backend.signals.regime import current_regime
from backend.signals.selection import choose_shortlist
from backend.signals.setups import _relative_strength_points, evaluate, htf_trend, min_score, stage
from backend.system_state import is_paused
from tradingagents.utils.market_calendar import IST, MCX, NSE, session_windows

GROUPS = {"nse": "fno_1h", "mcx": "mcx_1h"}
SEGMENTS = {"nse": ("stock_futures", "index_futures"), "mcx": ("mcx",)}


def _inst(segment: str):
    return get_config().instrument(segment)


def _x(segment: str, key: str, default):
    return (_inst(segment).model_extra or {}).get(key, default)


def risk_per_trade() -> float:
    g = get_config().global_
    return g.capital * g.risk.risk_per_trade_pct / 100


def breakdown_shorts(segment: str) -> dict | None:
    """MCX breakdown shorts (agreed 25 Nov 2026): ``instruments.<segment>.breakdown_shorts``
    in the YAML, or None when absent/disabled."""
    cfg = _x(segment, "breakdown_shorts", None)
    return cfg if cfg and cfg.get("enabled") else None


def mcx_value_per_point() -> dict[str, float]:
    """₹ value of a 1-point move per lot (MCX quotes per 10 g / kg / barrel / mmBtu)."""
    return _x("mcx", "value_per_point", {})


def _now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def exchange_days_after(exchange: str, start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if session_windows(exchange, d):
            out.append(d)
    return out


def last_session_before(exchange: str, expiry: date, today: date) -> date:
    """Last trading session before ``expiry`` (futures are never held on/after expiry)."""
    d = expiry - timedelta(days=1)
    while d > today and not session_windows(exchange, d):
        d -= timedelta(days=1)
    return max(d, today)


def _last_scan_note(segment_key: str) -> str:
    from backend.signals.cash_swing import latest_run
    run = latest_run(segment=segment_key)
    return f" Last scan: {run['started_at'][:16].replace('T', ' ')} ({run['status']})." if run else ""


def closed_reason(exchange: str, now: datetime, segment_key: str) -> str:
    """Why "Run now" can't scan on a closed day (asked 24 Nov 2026). An hourly signal from the
    last session's final bar would have its entry window already over, so nothing is re-scanned."""
    from tradingagents.utils.market_calendar import holiday_name
    d, nxt = now.date(), now.date() + timedelta(days=1)
    while not session_windows(exchange, nxt):
        nxt += timedelta(days=1)
    first = datetime.combine(nxt, session_windows(exchange, nxt)[0][0]) + timedelta(hours=1)
    why = holiday_name(d) if d.weekday() < 5 else "weekend"
    return (f"{exchange} closed today ({why or 'holiday'}). An hourly scan needs a completed bar of today's "
            f"session; the next one is after the bar ending {first:%a %d %b %H:%M}.{_last_scan_note(segment_key)}")


def waiting_reason(exchange: str, now: datetime, segment_key: str) -> str:
    """No completed hourly bar yet today: say when the first one closes."""
    from backend.scheduler import CANDLE_BUFFER
    windows = session_windows(exchange, now.date())
    start, close = windows[0]
    first = min(datetime.combine(now.date(), start) + timedelta(hours=1), datetime.combine(now.date(), close))
    partial = " (morning session closed today)" if exchange == MCX and start >= time(17, 0) else ""
    return (f"no completed hourly bar yet{partial}; the first bar closes at {first:%H:%M}, "
            f"scan from about {first + CANDLE_BUFFER:%H:%M}.{_last_scan_note(segment_key)}")


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


def hourly_quality(bars: list[dict], expected_start: datetime, exchange: str, need_oi: bool,
                   min_bars: int = 60) -> str | None:
    if len(bars) < min_bars:
        return f"only {len(bars)} hourly bars (need {min_bars})"
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
                            "value_per_point": mcx_value_per_point()[name], "ban": False})
    return out


def _min_daily_bars() -> int:
    from backend.marketdata.jobs import _min_daily_bars as need
    return need()


def run_hourly_scan(group: str, now: datetime | None = None, client=None, force: bool = False) -> dict:
    if group not in GROUPS:
        raise ValueError(group)
    segment_key = GROUPS[group]
    exchange = NSE if group == "nse" else MCX
    ensure_scan_tables()
    now = now or _now()
    tracked = track_open_records()
    lead = SEGMENTS[group][0]
    if not any(_inst(seg).enabled for seg in SEGMENTS[group]):
        return {"status": "skipped", "reason": f"{group} scans disabled in config", "tracking": tracked}
    if is_paused():
        return {"status": "skipped", "reason": "paused", "tracking": tracked}
    windows = session_windows(exchange, now.date())
    if not windows:
        return {"status": "skipped", "reason": closed_reason(exchange, now, segment_key), "tracking": tracked}
    bar_start = latest_hourly_bar_start(exchange, now)
    if bar_start is None:
        return {"status": "skipped", "reason": waiting_reason(exchange, now, segment_key), "tracking": tracked}
    signal_end = min(bar_start + timedelta(hours=1), datetime.combine(now.date(), windows[-1][1]))
    last_min = int(_x(lead, "no_entry_last_minutes", 30))
    first_min = int(_x(lead, "no_entry_first_minutes", 15))
    cutoff = datetime.combine(now.date(), windows[-1][1]) - timedelta(minutes=last_min)
    if signal_end >= cutoff and not force:
        return {"status": "skipped", "reason": f"entry window would start in the last {last_min} minutes",
                "tracking": tracked}
    if signal_end < datetime.combine(now.date(), windows[0][0]) + timedelta(minutes=first_min):
        return {"status": "skipped", "reason": f"inside the first {first_min} minutes", "tracking": tracked}

    run_id = _start_run(now.date().isoformat(), now, segment=segment_key)
    hold = exchange_days_after(exchange, now.date() - timedelta(days=1), _inst(lead).hold_trading_days)
    daily_expected = expected_last_daily_bar(now) if exchange == NSE else None

    regime_nse = None
    nifty_daily: list[dict] = []
    if exchange == NSE:
        nifty = spot_instrument("NIFTY")
        nifty_daily = get_candles(nifty["instrument_token"], "day", limit=300) if nifty else []
        problem = data_quality(nifty_daily, daily_expected, need_volume=False) if nifty else "NIFTY instrument missing"
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

    instruments = [i for i in _instruments(group, now.date()) if _inst(i["segment"]).enabled]
    if not instruments:
        return _finish_run(run_id, "blocked", {"reason": "no active contracts (instrument master missing)"})
    repaired = None
    if exchange == MCX and any(len(get_candles(i["contract"]["instrument_token"], "day", limit=300))
                               < _min_daily_bars() for i in instruments):
        # 24 Nov 2026: short continuous daily history blocked every MCX contract — download it again
        try:
            from backend.marketdata.jobs import repair_continuous_job
            repaired = repair_continuous_job(client, exchanges=(MCX,))
        except Exception as exc:
            repaired = {"status": "error", "reason": type(exc).__name__}

    rows: list[dict] = []
    for inst in instruments:
        sym, contract, seg = inst["symbol"], inst["contract"], inst["segment"]
        icfg = _inst(seg)
        base = {"symbol": sym, "segment": seg, "token": contract["instrument_token"],
                "tradingsymbol": contract["tradingsymbol"]}

        def reject(reason: str) -> None:
            rows.append({**base, "valid": False, "reject_reason": reason,
                         "stages": [stage("eligibility", "fail", reason)]})

        if inst["ban"] is True:
            reject("in F&O ban")
            continue
        if inst["ban"] == "unknown":
            reject("F&O ban list not verified today")
            continue
        hourly = get_candles(contract["instrument_token"], "60minute", limit=400)
        problem = hourly_quality(hourly, bar_start, exchange, need_oi=True, min_bars=icfg.min_primary_bars)
        if problem:
            reject(problem)
            continue
        if exchange == NSE:
            daily = get_candles(inst["spot"]["instrument_token"], "day", limit=300) if inst["spot"] else []
            # Index spot has no volume on Kite; stocks' spot volume is still checked.
            problem = (data_quality(daily, daily_expected, need_volume=seg != "index_futures")
                       if inst["spot"] else "no spot instrument")
            regime = regime_nse
        else:
            daily = get_candles(contract["instrument_token"], "day", limit=300)
            need = _min_daily_bars()
            problem = None if len(daily) >= need else \
                f"only {len(daily)} daily bars of {contract.get('tradingsymbol') or sym} (need {need}; " \
                f"Kite's continuous daily history came back short)"
            if not problem and (now.date() - date.fromisoformat(daily[-1]["ts"][:10])).days > 4:
                problem = f"stale daily bars (last {daily[-1]['ts'][:10]})"
            regime = current_regime(daily, high_vol_trend=bool(_x(seg, "regime_high_vol_trend", False))) \
                if not problem else None
        if problem:
            reject(f"daily data: {problem}")
            continue
        if seg == "stock_futures":
            ok, why = events.results_clear(sym, hold[0], hold[-1], now.date())
            if not ok:
                reject(why)
                continue
        if seg == "mcx":
            blocked, why = events.mcx_event_block(sym, signal_end)
            if blocked:
                reject(why)
                continue
        eligibility = stage("eligibility", "pass")

        context = {"day": daily}
        token = contract["instrument_token"]
        htf_bars = bars_for_timeframe(token, icfg.scored_htf or "4h", exchange) if icfg.scored_htf not in context \
            else context[icfg.scored_htf]
        confirmations = {tf: bars_for_timeframe(token, tf, exchange) for tf in icfg.confirmation_tfs
                         if tf != icfg.scored_htf}
        rs = None
        if seg == "stock_futures":
            rs = {d: _relative_strength_points(daily, nifty_daily, 1 if d == "long" else -1)
                  for d in ("long", "short")}
        oi = oi_signal(hourly)
        context_bars = {tf: context.get(tf) or bars_for_timeframe(token, tf, exchange)
                        for tf in icfg.context_must_not_oppose}
        must_not_oppose = {tf: htf_trend(b) or 0.0 for tf, b in context_bars.items()}
        candidates = evaluate(hourly, htf_bars, regime["regime"], directions=tuple(icfg.directions),
                              is_stock=seg == "stock_futures", rs_override=rs, oi=oi,
                              confirmations=confirmations, context=context_bars,
                              min_primary_bars=icfg.min_primary_bars,
                              stock_regime=current_regime(daily)["regime"] if daily else None,
                              breakdown_shorts=breakdown_shorts(seg))
        trend_cfg = mcx_trend.config_for(icfg.model_extra) if seg == "mcx" else None
        if trend_cfg:   # 4H trend → 2H pullback → 1H trigger (agreed 25 Nov 2026)
            candidates += mcx_trend.candidates(
                hourly, confirmations.get("2h") or bars_for_timeframe(token, "2h", exchange), htf_bars, regime,
                must_not_oppose.get("day"), directions=tuple(icfg.directions), oi=oi, cfg=trend_cfg,
                min_primary_bars=icfg.min_primary_bars)
        trackable = []
        for c in candidates:
            c["stages"] = [eligibility, *c.get("stages", [])]
            if not c.get("trackable"):
                continue
            lots = int(risk_per_trade() // (c["risk_per_unit"] * inst["value_per_point"])) if c["risk_per_unit"] else 0
            if lots < 1:
                lot_risk = c["risk_per_unit"] * inst["value_per_point"]
                c.update(trackable=False, valid=False,
                         reject_reason=f"1 lot risks ₹{lot_risk:,.0f}, more than the per-trade limit "
                                       f"(₹{risk_per_trade():,.0f})")
                c["stages"].append(stage("construction", "fail", c["reject_reason"]))
                continue
            c["lots"] = lots
            c["stages"].append(stage("sizing", "pass", lots=lots))
            trackable.append(c)
        if trackable:
            for c in trackable:
                rows.append({**base, **c, "oi_change_pct": oi["change_pct"],
                             "value_per_point": inst["value_per_point"], "expiry": contract["expiry"],
                             "context": {tf: t for tf, t in must_not_oppose.items()}})
        else:
            reasons = "; ".join(sorted({c["reject_reason"] for c in candidates if c.get("reject_reason")}))
            rows.append({**base, "valid": False, "regime": regime["regime"], "reject_reason": reasons,
                         "stages": [eligibility]})

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
            max_spread = float(_x(r["segment"], "max_spread_pct", 0.1))
            if spread is None or spread > max_spread:
                r.update(trackable=False, valid=False,
                         reject_reason="liquidity not verified" if spread is None
                         else f"bid-ask spread {spread:.2f}% > {max_spread}%")
                r["stages"].append(stage("eligibility", "fail", r["reject_reason"]))
            else:
                r["stages"].append(stage("liquidity", "pass", spread_pct=spread))

    ranked = sorted((r for r in rows if r.get("valid") and r.get("trackable") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    shortlist_n = _inst(lead).shortlist_per_scan
    shortlist = choose_shortlist(ranked, shortlist_n, lambda r: r["segment"])
    for r in rows:
        if r.get("trackable"):
            selected = bool(r.get("shortlisted"))
            r["stages"].append(stage("ai_panel", "pending" if selected else "not_sent"))
            r["stages"].append(stage("final_approval", "pending" if selected else "not_applicable"))
    _save(run_id, now.date().isoformat(), rows, segment=segment_key)

    fill_until = min(signal_end + timedelta(hours=_inst(lead).model_extra.get("entry_valid_bars", 2)), cutoff)
    journal_rows = []
    for r in rows:
        if not r.get("trackable"):
            continue
        expiry = date.fromisoformat(r["expiry"])
        if _inst(r["segment"]).time_stop:
            days = [d for d in hold if d < expiry] or [hold[0]]
            exit_by = days[-1]
        else:   # no time stop: target / stop / trailing stop, at the latest the session before expiry
            exit_by = last_session_before(exchange, expiry, now.date())
        journal_rows.append(_journal_row(r, now.date(), bar_start, fill_until, exit_by, exchange))
    journal_add(journal_rows)
    from backend.journal.panel import review_after_scan
    panel = review_after_scan(now)    # AI review + Telegram alerts for the new shortlist
    return _finish_run(run_id, "ok", {
        "ai_panel": panel, "daily_repair": repaired,
        "signal_bar": bar_start.isoformat(timespec="minutes"), "regime": regime_nse,
        "instruments": len(instruments), "valid_setups": sum(1 for r in rows if r.get("valid")),
        "above_60": len(ranked), "shortlisted": [r["symbol"] for r in shortlist],
        "skipped_open_trade": [r["symbol"] for r in ranked if r.get("selection_skip")],
        "journal_records": len(journal_rows), "tracking": tracked, "rule_version": get_config().rule_version,
    })


def _journal_row(r: dict, day: date, bar_start: datetime, fill_until: datetime, time_stop: date,
                 exchange: str) -> dict:
    if not r.get("valid"):
        decision = "soft_rejected"
    elif r.get("shortlisted"):
        decision = "shortlisted"
    elif r["score"] >= min_score():
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
                                         "spread_pct", "stop_atr", "stop_type", "pullback_touch", "next_level", "vol_ratio", "htf_trend",
                                         "rs_vs_nifty_pct", "risk_mult", "confirmations", "context")},
        "stages": r.get("stages"), "rule_version": r.get("rule_version"),
    }
