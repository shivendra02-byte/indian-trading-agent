"""Wolfe Wave (bullish) on daily bars — cash stocks, long only, its own book.

Agreed 23 Nov 2026: a full strategy (journal → AI panel → Telegram), cash only,
within the overall daily AI limit. All numbers come from ``instruments.wolfe``.

Five alternating confirmed swing points (5-bar fractals, consecutive same-side
swings collapsed to the more extreme one): lows 1, 3, 5 and highs 2, 4.

* 3 below 1 and 5 below 3; 4 below 2 (a falling wedge),
* 4 above 1 (the waves overlap),
* the 2–4 line falls faster than the 1–3 line (the lines converge),
* time symmetry: bars 3→5 are ``min_time_ratio``–``max_time_ratio`` × bars 1→3,
* the whole pattern 1→5 spans ``min_span``–``max_span`` bars,
* point 5 at or below the 1–3 line, by at most ``max_overshoot_atr`` ATR (the "sweet zone").

The signal is the day point 5 is confirmed (two bars after it). Trade: buy limit
at the signal close for the next session; stop ``stop_atr`` ATR under point 5
(at most ``max_stop_pct`` below the entry); target = the 1–4 line at the signal
day (conservative: the line keeps rising), at least ``min_rr`` R away; time stop
after ``hold_trading_days``; managed exit from ``exit_management`` (breakeven,
then trail). Point-in-time: only swings confirmed by the signal bar are used.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Sequence

from backend.decision.config import get_config
from backend.signals.indicators import atr as atr_series
from backend.signals.indicators import rsi as rsi_series
from backend.signals.indicators import swing_points
from backend.signals.selection import choose_shortlist, open_positions
from backend.signals.setups import stage

SEGMENT = "wolfe"
SETUP = "wolfe_wave"
CASH_BOOKS = ("cash_swing", "cup_handle", "wolfe")   # one stock is never held in two cash books at once


def settings() -> dict:
    inst = get_config().instrument(SEGMENT)
    x = inst.model_extra or {}
    g = lambda k, d: float(x.get(k, d))  # noqa: E731
    return {
        "inst": inst, "enabled": inst.enabled, "hold_days": inst.hold_trading_days or 20,
        "shortlist": inst.shortlist_per_scan, "min_bars": inst.min_primary_bars,
        "swing_bars": int(g("swing_bars", 2)), "min_span": int(g("min_span_bars", 15)),
        "max_span": int(g("max_span_bars", 150)), "min_time_ratio": g("min_time_ratio", 0.5),
        "max_time_ratio": g("max_time_ratio", 2.0), "max_overshoot_atr": g("max_overshoot_atr", 1.0),
        "line_tolerance_atr": g("line_tolerance_atr", 0.25), "require_convergence": bool(x.get("require_convergence", True)),
        "stop_atr": g("stop_atr", 0.25), "max_stop_pct": g("max_stop_pct", 8) / 100, "min_rr": g("min_rr", 2.0),
        "min_turnover": g("min_turnover_cr", 50) * 1e7, "event_block_days": int(g("event_block_days", 5)),
        "entry_window": x.get("entry_window", ["09:15", "15:15"]),
    }


def alternating_swings(bars: Sequence[dict], k: int, upto: int | None = None) -> list[tuple[int, float, str]]:
    """Confirmed swings (index, price, 'H'/'L') in time order, alternating: of two
    same-side swings in a row the more extreme is kept. ``upto``: only swings
    confirmed by bar ``upto`` (index ≤ upto - k)."""
    pts = raw_swings(bars, k)
    if upto is not None:
        pts = [p for p in pts if p[0] <= upto - k]
    return collapse(pts)


def raw_swings(bars: Sequence[dict], k: int) -> list[tuple[int, float, str]]:
    highs, lows = swing_points(bars, k)
    return sorted([(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows])


def collapse(pts: list[tuple[int, float, str]]) -> list[tuple[int, float, str]]:
    """Alternate highs and lows: of two same-side swings in a row keep the more extreme."""
    out: list[tuple[int, float, str]] = []
    for p in pts:
        if out and out[-1][2] == p[2]:
            if (p[2] == "H" and p[1] >= out[-1][1]) or (p[2] == "L" and p[1] <= out[-1][1]):
                out[-1] = p
            continue
        if out and out[-1][0] == p[0]:          # a bar that is both: keep the earlier side
            continue
        out.append(p)
    return out


def _line(a: tuple[int, float], b: tuple[int, float]):
    slope = (b[1] - a[1]) / (b[0] - a[0])
    return (lambda i: a[1] + slope * (i - a[0])), slope


def detect(bars: Sequence[dict], cfg: dict | None = None, swings: list | None = None,
           atr_values: list | None = None, rsi_values: list | None = None) -> dict:
    """A bullish Wolfe Wave whose point 5 is confirmed on the last bar, or a reason why not."""
    cfg = cfg or settings()
    k = cfg["swing_bars"]
    t = len(bars) - 1
    if t < 30:
        return {"found": False, "reason": "not enough bars"}
    swings = swings if swings is not None else alternating_swings(bars, k, upto=t)
    if len(swings) < 5:
        return {"found": False, "reason": "fewer than 5 swing points"}
    p1, p2, p3, p4, p5 = swings[-5:]
    if p5[2] != "L" or p5[0] != t - k:
        return {"found": False, "reason": "no swing low confirmed today"}
    (i1, v1, _), (i2, v2, _), (i3, v3, _), (i4, v4, _), (i5, v5, _) = p1, p2, p3, p4, p5
    if not (v3 < v1 and v5 < v3):
        return {"found": False, "reason": "lows not falling (1 > 3 > 5)"}
    if not v4 < v2:
        return {"found": False, "reason": "highs not falling (2 > 4)"}
    if not v4 > v1:
        return {"found": False, "reason": "point 4 not above point 1"}
    span = i5 - i1
    if not cfg["min_span"] <= span <= cfg["max_span"]:
        return {"found": False, "reason": f"pattern spans {span} bars"}
    ratio = (i5 - i3) / max(i3 - i1, 1)
    if not cfg["min_time_ratio"] <= ratio <= cfg["max_time_ratio"]:
        return {"found": False, "reason": f"waves not symmetric (3→5 is {ratio:.2f}× 1→3)"}
    line13, slope13 = _line((i1, v1), (i3, v3))
    line24, slope24 = _line((i2, v2), (i4, v4))
    converging = slope24 < slope13
    if cfg["require_convergence"] and not converging:
        return {"found": False, "reason": "lines 1–3 and 2–4 do not converge"}
    atrs = atr_values if atr_values is not None else atr_series(bars, 14)
    a = atrs[t]
    if not a:
        return {"found": False, "reason": "no ATR"}
    l13_at5 = line13(i5)
    overshoot = (l13_at5 - v5) / a           # + = below the line
    if overshoot < -cfg["line_tolerance_atr"]:
        return {"found": False, "reason": "point 5 did not reach the 1–3 line"}
    if overshoot > cfg["max_overshoot_atr"]:
        return {"found": False, "reason": f"point 5 overshot the 1–3 line by {overshoot:.1f} ATR"}
    line14, _ = _line((i1, v1), (i4, v4))
    rsis = rsi_values if rsi_values is not None else rsi_series([b["close"] for b in bars], 14)
    rsi_div = bool(rsis[i5] and rsis[i3] and rsis[i5] > rsis[i3])
    day = lambda i: str(bars[i]["ts"])[:10]  # noqa: E731
    return {"found": True, "points": [{"n": n, "date": day(i), "index": i, "price": round(v, 2)}
                                      for n, (i, v, _) in enumerate((p1, p2, p3, p4, p5), 1)],
            "line13_at5": round(l13_at5, 2), "overshoot_atr": round(overshoot, 2),
            "target_line14": round(line14(t), 2), "time_ratio": round(ratio, 2), "span_bars": span,
            "converging": converging, "rsi_divergence": rsi_div, "atr": a,
            "slope13": round(slope13, 4), "slope24": round(slope24, 4),
            "line14": {"from": [day(i1), round(v1, 2)], "slope_per_bar": round((v4 - v1) / (i4 - i1), 4)}}


def score(p: dict, rr: float) -> dict:
    """0–100: time symmetry 25, R:R 25, point 5 in the sweet zone 15, converging lines 15,
    RSI higher at point 5 than at point 3 (bullish divergence) 20."""
    sym = abs(1 - p["time_ratio"])
    factors = {
        "symmetry": 25 if sym <= 0.2 else 18 if sym <= 0.5 else 10,
        "reward_risk": 25 if rr >= 4 else 18 if rr >= 3 else 12,
        "sweet_zone": 15 if 0 <= p["overshoot_atr"] <= 0.6 else 8,
        "convergence": 15 if p["converging"] else 0,
        "rsi_divergence": 20 if p["rsi_divergence"] else 0,
    }
    return {"factors": factors, "score": float(sum(factors.values()))}


def candidate(bars: Sequence[dict], cfg: dict | None = None, regime: str | None = None, **kw) -> dict:
    """Journal-ready candidate or a rejection."""
    cfg = cfg or settings()
    p = detect(bars, cfg, **kw)
    if not p["found"]:
        return {"setup": SETUP, "direction": "long", "valid": False, "trackable": False,
                "reject_reason": p["reason"], "stages": [stage("setup", "fail", p["reason"])]}
    close = bars[-1]["close"]
    entry = close
    stop = p["points"][4]["price"] - cfg["stop_atr"] * p["atr"]
    risk = entry - stop
    target = p["target_line14"]
    if risk <= 0 or risk / entry > cfg["max_stop_pct"]:
        why = f"stop {100 * risk / entry:.1f}% away (max {100 * cfg['max_stop_pct']:.0f}%)"
        return {"setup": SETUP, "direction": "long", "valid": False, "trackable": False, "reject_reason": why,
                "stages": [stage("setup", "pass"), stage("construction", "fail", why)]}
    rr = (target - entry) / risk
    if rr < cfg["min_rr"]:
        why = f"1–4 line only {rr:.1f}R away (min {cfg['min_rr']}R)"
        return {"setup": SETUP, "direction": "long", "valid": False, "trackable": False, "reject_reason": why,
                "stages": [stage("setup", "pass"), stage("construction", "fail", why)]}
    sc = score(p, rr)
    min_score = get_config().global_.scoring.min_shortlist_score
    return {
        "setup": SETUP, "direction": "long", "valid": True, "trackable": True, "reject_reason": None,
        "regime": regime, "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
        "risk_per_unit": round(risk, 2), "rr": round(rr, 2), "atr": round(p["atr"], 4),
        "signal_close": round(close, 2), "score": sc["score"], "factors": sc["factors"], "pattern": p,
        "soft_checks": {"rsi_divergence": p["rsi_divergence"]}, "rule_version": get_config().rule_version,
        "stages": [stage("setup", "pass", points=[pt["price"] for pt in p["points"]], time_ratio=p["time_ratio"],
                         overshoot_atr=p["overshoot_atr"]),
                   stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
                         rr=round(rr, 2)),
                   stage("scoring", "pass" if sc["score"] >= min_score else "below_threshold", score=sc["score"])],
    }


def find_all(bars: Sequence[dict], cfg: dict | None = None, start: int = 0) -> list[dict]:
    """Every Wolfe candidate (trackable or not) by signal bar, point in time — for studies/backtests."""
    cfg = cfg or settings()
    k = cfg["swing_bars"]
    atrs = atr_series(bars, 14)
    rsis = rsi_series([b["close"] for b in bars], 14)
    raw = raw_swings(bars, k)          # a 5-bar fractal at i needs only bars up to i + k: same as on any prefix
    lows_at = {i for i, _, side in raw if side == "L"}
    out = []
    for t in range(max(start, 40), len(bars)):
        if (t - k) not in lows_at:     # a signal needs a swing low confirmed exactly today
            continue
        sw = collapse([p for p in raw if p[0] <= t - k])
        c = candidate(bars[:t + 1], cfg, swings=sw, atr_values=atrs[:t + 1], rsi_values=rsis[:t + 1])
        if c.get("trackable"):
            c["s"] = t
            out.append(c)
    return out


# --- daily scan ------------------------------------------------------------------------

def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    """Scan every F&O stock for a Wolfe Wave whose point 5 is confirmed today
    (after the cup & handle scan); shortlist → AI panel → Telegram."""
    from backend.journal.store import add_records as journal_add
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe, spot_instrument
    from backend.signals import events
    from backend.signals.cash_swing import (IST, _finish_run, _save, _start_run, avg_turnover, data_quality,
                                            ensure_scan_tables, expected_last_daily_bar, min_score)
    from backend.signals.regime import current_regime
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import is_trading_day

    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    today = now.date()
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "Wolfe Wave disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if not is_trading_day(today) and not force:
        return {"status": "skipped", "reason": "not an NSE trading day"}

    signal_date = expected_last_daily_bar(now)
    run_id = _start_run(signal_date.isoformat(), now, segment=SEGMENT)
    hold = events.trading_days_after(signal_date, cfg["hold_days"])
    first, last = hold[0], hold[-1]
    block_last = hold[min(cfg["event_block_days"], len(hold)) - 1]
    if not cfg["inst"].time_stop:   # no time stop (23 Nov 2026): target / stop / trailing stop, safety exit later
        last = events.trading_days_after(signal_date, cfg["inst"].max_hold_trading_days)[-1]
    nifty = spot_instrument("NIFTY")
    nifty_bars = get_candles(nifty["instrument_token"], "day", limit=800) if nifty else []
    problem = data_quality(nifty_bars, signal_date) if nifty else "NIFTY instrument missing"
    if problem:
        return _finish_run(run_id, "blocked", {"reason": f"NIFTY data: {problem}"})
    market = events.market_events_between(first, block_last, kinds=("RBI", "BUDGET"))
    if not market["verified"]:
        return _finish_run(run_id, "blocked", {"reason": "event calendar not verified for the entry window"})
    if market["events"]:
        names = ", ".join(f"{e['event']} {e['date']}" for e in market["events"])
        return _finish_run(run_id, "blocked", {"reason": f"event in the first {cfg['event_block_days']} days: {names}"})
    regime = current_regime(nifty_bars[-300:])["regime"]
    universe = fno_universe(today)
    if not universe:
        return _finish_run(run_id, "blocked", {"reason": "F&O universe is empty (instrument master missing)"})
    events.refresh_results([u["symbol"] for u in universe], fetch=results_fetch, today=today)

    rows = []
    for stock in universe:
        symbol = stock["symbol"]
        if not stock["spot_token"]:
            continue
        bars = get_candles(stock["spot_token"], "day", limit=400)
        problem = data_quality(bars, signal_date, min_bars=cfg["min_bars"])
        if problem:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": problem,
                         "stages": [stage("eligibility", "fail", problem)]})
            continue
        turnover = avg_turnover(bars)
        if turnover < cfg["min_turnover"]:
            why = f"turnover ₹{turnover / 1e7:.0f} cr < ₹{cfg['min_turnover'] / 1e7:.0f} cr"
            rows.append({"symbol": symbol, "valid": False, "reject_reason": why,
                         "stages": [stage("eligibility", "fail", why)]})
            continue
        ok, why = events.results_clear(symbol, first, block_last, today)
        if not ok:
            rows.append({"symbol": symbol, "valid": False, "reject_reason": why,
                         "stages": [stage("eligibility", "fail", why)]})
            continue
        c = candidate(bars, cfg, regime=regime)
        c["stages"] = [stage("eligibility", "pass", turnover_cr=round(turnover / 1e7, 1)), *c["stages"]]
        rows.append({**c, "symbol": symbol, "token": stock["spot_token"], "turnover_cr": round(turnover / 1e7, 1)})

    ranked = sorted((r for r in rows if r.get("valid") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    held = {(SEGMENT, sym) for _, sym in open_positions(CASH_BOOKS)}
    shortlist = choose_shortlist(ranked, cfg["shortlist"], lambda r: SEGMENT, busy=held)
    for r in rows:
        if r.get("trackable"):
            sel = bool(r.get("shortlisted"))
            r["stages"] = [*r["stages"], stage("ai_panel", "pending" if sel else "not_sent"),
                           stage("final_approval", "pending" if sel else "not_applicable")]
    _save(run_id, signal_date.isoformat(), rows, segment=SEGMENT)
    journal_add([journal_row(r, signal_date, first, last, cfg["entry_window"][1])
                 for r in rows if r.get("trackable")])
    rejected: dict[str, int] = {}
    for r in rows:
        if not r.get("trackable"):
            key = r["reject_reason"].split(" (")[0]
            rejected[key] = rejected.get(key, 0) + 1
    from backend.journal.panel import review_after_scan
    panel = review_after_scan(now)
    return _finish_run(run_id, "ok", {
        "ai_panel": panel, "signal_date": signal_date.isoformat(), "entry_day": first.isoformat(),
        "time_stop": last.isoformat(), "regime": regime, "stocks": len(universe),
        "patterns": len([r for r in rows if r.get("trackable")]),
        "shortlisted": [r["symbol"] for r in shortlist],
        "skipped_open_trade": [r["symbol"] for r in ranked if r.get("selection_skip")],
        "top_reject_reasons": dict(sorted(rejected.items(), key=lambda kv: -kv[1])[:5]),
        "rule_version": get_config().rule_version,
    })


def journal_row(r: dict, signal_date: date, first: date, last: date, fill_until: str) -> dict:
    from backend.signals.cash_swing import min_score
    decision = "shortlisted" if r.get("shortlisted") else "not_selected" if r["score"] >= min_score() \
        else "below_score"
    return {
        "segment": SEGMENT, "symbol": r["symbol"], "instrument_token": r["token"], "exchange": "NSE",
        "signal_date": signal_date.isoformat(), "setup": SETUP, "direction": "long",
        "regime": r.get("regime"), "score": r.get("score"), "factors": r.get("factors"),
        "soft_checks": r.get("soft_checks"), "decision": decision, "reject_reason": r.get("selection_skip"),
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": r.get("atr"),
        "entry_date": first.isoformat(), "fill_until": f"{first.isoformat()}T{fill_until}",
        "time_stop_date": last.isoformat(),
        "extra": {"pattern": r.get("pattern"), "turnover_cr": r.get("turnover_cr")},
        "stages": r.get("stages"), "rule_version": r.get("rule_version"),
    }
