"""Cup and handle (William O'Neil) on daily bars — cash stocks, long only.

Chart pattern only (no earnings filters), with its own exits and hold, per the
rules agreed on 21 Nov 2026 (project doc ``trade-selection-rules.md`` §8).
All numbers come from ``instruments.cup_handle`` in config/decision_engine.yaml.

Pattern on the signal day (the last bar):
* prior uptrend into the left rim (≥ ``prior_uptrend_pct``),
* cup ``cup_min_weeks``–``cup_max_weeks`` long and ``cup_min_depth_pct``–
  ``cup_max_depth_pct`` deep, with a rounded bottom (enough bars spent near the low),
* right rim within ``rim_tolerance_pct`` of the left rim,
* handle ``handle_min_days``–``handle_max_days`` long, in the upper half of the
  cup and at most ``handle_max_depth_pct`` deep,
* breakout: close above the handle high (pivot), at most ``max_extended_pct``
  above it, on volume ≥ ``breakout_volume_ratio`` × the ``volume_avg_days`` average,
* stock outperforming NIFTY over ``rs_lookback_days``.

Trade: buy limit at the top of the buy zone (pivot + ``max_extended_pct``) on the
next session, so it fills at the open unless the stock gaps beyond the zone;
stop ``stop_pct`` below and target ``target_pct`` above the signal close; time
stop after ``hold_trading_days``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Sequence

from backend.decision.config import get_config
from backend.signals.indicators import swing_points
from backend.signals.selection import choose_shortlist
from backend.signals.setups import stage

SEGMENT = "cup_handle"
SETUP = "cup_handle"


def settings() -> dict:
    inst = get_config().instrument(SEGMENT)
    x = inst.model_extra or {}
    g = lambda k, d: float(x.get(k, d))  # noqa: E731
    return {
        "inst": inst, "enabled": inst.enabled, "hold_days": inst.hold_trading_days,
        "shortlist": inst.shortlist_per_scan, "min_bars": inst.min_primary_bars,
        "prior_uptrend_pct": g("prior_uptrend_pct", 30), "prior_lookback_days": int(g("prior_lookback_days", 130)),
        "cup_min_days": int(g("cup_min_weeks", 7) * 5), "cup_max_days": int(g("cup_max_weeks", 65) * 5),
        "cup_min_depth": g("cup_min_depth_pct", 12) / 100, "cup_max_depth": g("cup_max_depth_pct", 33) / 100,
        "rounded_min_fraction": g("rounded_bottom_min_fraction", 0.30),
        "rim_tolerance": g("rim_tolerance_pct", 10) / 100,
        "handle_min_days": int(g("handle_min_days", 5)), "handle_max_days": int(g("handle_max_days", 25)),
        "handle_max_depth": g("handle_max_depth_pct", 12) / 100,
        "breakout_volume_ratio": g("breakout_volume_ratio", 1.5), "volume_avg_days": int(g("volume_avg_days", 50)),
        "max_extended": g("max_extended_pct", 5) / 100, "rs_lookback_days": int(g("rs_lookback_days", 126)),
        "stop_pct": g("stop_pct", 8) / 100, "target_pct": g("target_pct", 20) / 100,
        "min_turnover": g("min_turnover_cr", 50) * 1e7, "event_block_days": int(g("event_block_days", 5)),
        "entry_window": x.get("entry_window", ["09:15", "15:15"]),
        "max_position_value": g("max_position_value", 200_000),
    }


def _avg_volume(bars: Sequence[dict], end: int, n: int) -> float:
    window = [b.get("volume") or 0 for b in bars[max(0, end - n):end]]
    return sum(window) / len(window) if window else 0.0


def _relative_strength(bars: Sequence[dict], benchmark: Sequence[dict], n: int) -> float | None:
    """Stock return minus NIFTY return over the last ``n`` sessions, in %."""
    if len(bars) <= n or not benchmark:
        return None
    bench = {b["ts"][:10]: b["close"] for b in benchmark}
    b0, b1 = bench.get(bars[-n - 1]["ts"][:10]), bench.get(bars[-1]["ts"][:10])
    if not b0 or not b1:
        return None
    return round(100 * (bars[-1]["close"] / bars[-n - 1]["close"] - b1 / b0), 2)


def detect(bars: Sequence[dict], benchmark: Sequence[dict] | None, cfg: dict | None = None,
           swing_highs: Sequence[tuple[int, float]] | None = None) -> dict:
    """Check for a cup-and-handle breakout on the last bar.

    Returns {"found": bool, "reason": str, ...pattern fields}. ``swing_highs``
    may be passed precomputed (indices into ``bars``) for speed; only swings
    confirmed by the last bar are used."""
    cfg = cfg or settings()
    n = len(bars)
    t = n - 1
    if n < cfg["cup_min_days"] + cfg["handle_min_days"] + cfg["prior_lookback_days"]:
        return {"found": False, "reason": f"only {n} daily bars"}
    if swing_highs is None:
        swing_highs, _ = swing_points(bars, 2)
    rim_index = {i for i, _ in swing_highs if i <= t - 3}   # swings confirmed before the signal bar
    signal = bars[t]
    # Quick exit: a breakout above the handle must also be a close above every
    # high since the right rim and of the minimum cup length before it.
    recent = bars[max(0, t - cfg["cup_min_days"] - cfg["handle_min_days"]):t]
    if not recent or signal["close"] <= max(b["high"] for b in recent):
        return {"found": False, "reason": "no close above the handle high"}
    best_reason = "no cup with handle"
    for h_len in range(cfg["handle_min_days"], cfg["handle_max_days"] + 1):
        c = t - h_len                      # right rim (start of the handle)
        if c < 1:
            break
        handle = bars[c:t]                 # right rim .. day before the signal
        pivot = max(b["high"] for b in handle)
        if signal["close"] <= pivot:
            break                          # longer handles only raise the pivot
        if bars[c]["high"] < pivot:        # the handle starts at its high
            continue
        rim_r = bars[c]["high"]
        handle_low = min(b["low"] for b in handle[1:]) if len(handle) > 1 else rim_r
        handle_depth = (rim_r - handle_low) / rim_r
        # Walk left from the right rim, keeping the running max high and min low
        # of the cup interior; test each confirmed swing high as the left rim.
        inner_max, low_i = float("-inf"), None
        for a in range(c - 1, max(0, c - cfg["cup_max_days"]) - 1, -1):
            length = c - a
            if length >= cfg["cup_min_days"] and a in rim_index:
                rim_l = bars[a]["high"]
                if inner_max > max(rim_l, rim_r):
                    best_reason = "price broke above the rims inside the cup"
                    break                  # further left rims would contain the same breach
                found = _check_cup(bars, a, c, t, low_i, rim_l, rim_r, handle_low, handle_depth, cfg)
                if isinstance(found, str):
                    best_reason = found
                    continue
                return _finish(bars, a, c, t, h_len, low_i, rim_l, pivot, handle_depth, found, benchmark, cfg)
            # extend the interior to include bar a before moving further left
            inner_max = max(inner_max, bars[a]["high"])
            if low_i is None or bars[a]["low"] < bars[low_i]["low"]:
                low_i = a
    return {"found": False, "reason": best_reason}


def _check_cup(bars, a, c, t, low_i, rim_l, rim_r, handle_low, handle_depth, cfg):
    """Pattern checks for left rim ``a``; returns (depth, rounded, prior_rise) or the reason."""
    if not (rim_l * (1 - cfg["rim_tolerance"]) <= rim_r <= rim_l * (1 + cfg["rim_tolerance"] / 2)):
        return "right rim not level with the left rim"
    cup_low = bars[low_i]["low"]
    if max(b["high"] for b in bars[low_i:c]) > rim_r:
        return "the handle does not start at the right-side high"
    depth = (rim_l - cup_low) / rim_l
    if not (cfg["cup_min_depth"] <= depth <= cfg["cup_max_depth"]):
        return f"cup depth {depth:.0%} outside {cfg['cup_min_depth']:.0%}–{cfg['cup_max_depth']:.0%}"
    inner = bars[a + 1:c]
    near_low = cup_low + 0.25 * (rim_l - cup_low)
    rounded = sum(1 for b in inner if b["close"] <= near_low) / len(inner)
    if rounded < cfg["rounded_min_fraction"]:
        return "V-shaped bottom (too little time near the low)"
    if handle_low < cup_low + 0.5 * (rim_r - cup_low):
        return "handle in the lower half of the cup"
    if handle_depth > cfg["handle_max_depth"]:
        return f"handle {handle_depth:.0%} deep (max {cfg['handle_max_depth']:.0%})"
    prior = bars[max(0, a - cfg["prior_lookback_days"]):a]
    prior_low = min(b["low"] for b in prior) if prior else rim_l
    prior_rise = rim_l / prior_low - 1 if prior_low > 0 else 0
    if prior_rise < cfg["prior_uptrend_pct"] / 100:
        return f"prior uptrend {prior_rise:.0%} < {cfg['prior_uptrend_pct']:.0f}%"
    return depth, rounded, prior_rise


def _finish(bars, a, c, t, h_len, low_i, rim_l, pivot, handle_depth, found, benchmark, cfg) -> dict:
    """Breakout checks (extension, volume, relative strength) once a cup is found."""
    depth, rounded, prior_rise = found
    signal = bars[t]
    extended = signal["close"] / pivot - 1
    if extended > cfg["max_extended"]:
        return {"found": False, "reason": f"close {extended:.1%} above the pivot (max {cfg['max_extended']:.0%})"}
    avg_vol = _avg_volume(bars, t, cfg["volume_avg_days"])
    vol_ratio = (signal.get("volume") or 0) / avg_vol if avg_vol else 0.0
    if vol_ratio < cfg["breakout_volume_ratio"]:
        return {"found": False, "reason": f"breakout volume {vol_ratio:.1f}x < {cfg['breakout_volume_ratio']}x"}
    rs = _relative_strength(bars, benchmark or [], cfg["rs_lookback_days"])
    if rs is None or rs <= 0:
        return {"found": False, "reason": "not outperforming NIFTY" if rs is not None
                else "relative strength not available"}
    dry_up = _avg_volume(bars, t, h_len) < _avg_volume(bars, c, cfg["volume_avg_days"])
    return {"found": True, "reason": "", "pivot": round(pivot, 2), "left_rim_ts": bars[a]["ts"][:10],
            "cup_low_ts": bars[low_i]["ts"][:10], "right_rim_ts": bars[c]["ts"][:10],
            "cup_weeks": round((c - a) / 5, 1), "cup_depth_pct": round(100 * depth, 1),
            "handle_days": h_len, "handle_depth_pct": round(100 * handle_depth, 1),
            "prior_uptrend_pct": round(100 * prior_rise, 1), "extended_pct": round(100 * extended, 2),
            "vol_ratio": round(vol_ratio, 2), "rs_vs_nifty_pct": rs, "handle_volume_dry_up": dry_up,
            "rounded_fraction": round(rounded, 2)}


def score(p: dict) -> dict:
    """0–100: breakout volume 25, relative strength 20, handle tightness 20,
    cup depth 15, handle volume dry-up 10, close near the pivot 10."""
    factors = {
        "breakout_volume": 25 if p["vol_ratio"] >= 2.5 else 18 if p["vol_ratio"] >= 2 else 12,
        "relative_strength": 20 if p["rs_vs_nifty_pct"] >= 20 else 14 if p["rs_vs_nifty_pct"] >= 10 else 8,
        "handle_tightness": 20 if p["handle_depth_pct"] <= 6 else 14 if p["handle_depth_pct"] <= 9 else 8,
        "cup_depth": 15 if 15 <= p["cup_depth_pct"] <= 25 else 8,
        "handle_dry_up": 10 if p["handle_volume_dry_up"] else 0,
        "near_pivot": 10 if p["extended_pct"] <= 2 else 5,
    }
    return {"factors": factors, "score": float(sum(factors.values()))}


def candidate(bars: Sequence[dict], benchmark: Sequence[dict] | None, cfg: dict | None = None,
              swing_highs=None, regime: str | None = None) -> dict:
    """Journal-ready candidate (setup, entry, stop, target, score, stages) or a rejection."""
    cfg = cfg or settings()
    p = detect(bars, benchmark, cfg, swing_highs)
    if not p["found"]:
        return {"setup": SETUP, "direction": "long", "valid": False, "trackable": False,
                "reject_reason": p["reason"], "stages": [stage("setup", "fail", p["reason"])]}
    close = bars[-1]["close"]
    # Buy zone: up to max_extended above the pivot (O'Neil), so a breakout that keeps
    # running the next morning still fills; stop and target are set from the signal close.
    entry = p["pivot"] * (1 + cfg["max_extended"])
    stop, target = close * (1 - cfg["stop_pct"]), close * (1 + cfg["target_pct"])
    sc = score(p)
    min_score = get_config().global_.scoring.min_shortlist_score
    return {
        "setup": SETUP, "direction": "long", "valid": True, "trackable": True, "reject_reason": None,
        "regime": regime, "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
        "risk_per_unit": round(close - stop, 2), "rr": round(cfg["target_pct"] / cfg["stop_pct"], 2),
        "signal_close": round(close, 2),
        "score": sc["score"], "factors": sc["factors"], "pattern": p, "vol_ratio": p["vol_ratio"],
        "rs_vs_nifty_pct": p["rs_vs_nifty_pct"], "soft_checks": {"handle_dry_up": p["handle_volume_dry_up"]},
        "rule_version": get_config().rule_version,
        "stages": [stage("setup", "pass", **{k: p[k] for k in ("pivot", "cup_weeks", "cup_depth_pct",
                                                                "handle_days", "handle_depth_pct")}),
                   stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2),
                         target=round(target, 2)),
                   stage("scoring", "pass" if sc["score"] >= min_score else "below_threshold", score=sc["score"])],
    }


# --- daily scan ------------------------------------------------------------------------

def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    """Scan every F&O stock for a cup-and-handle breakout at the close (after the
    cash swing scan). Stage A filters as cash swing, except events are checked
    only for the first ``event_block_days`` of the hold (an 8-week hold would
    otherwise always contain a results date)."""
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
        return {"status": "skipped", "reason": "cup and handle disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if not is_trading_day(today) and not force:
        return {"status": "skipped", "reason": "not an NSE trading day"}

    signal_date = expected_last_daily_bar(now)
    run_id = _start_run(signal_date.isoformat(), now, segment=SEGMENT)
    hold = events.trading_days_after(signal_date, cfg["hold_days"])
    first, last = hold[0], hold[-1]
    block_last = hold[min(cfg["event_block_days"], len(hold)) - 1]

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
        bars = get_candles(stock["spot_token"], "day", limit=800)
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
        c = candidate(bars, nifty_bars, cfg, regime=regime)
        c["stages"] = [stage("eligibility", "pass", turnover_cr=round(turnover / 1e7, 1)), *c["stages"]]
        rows.append({**c, "symbol": symbol, "token": stock["spot_token"], "turnover_cr": round(turnover / 1e7, 1)})

    ranked = sorted((r for r in rows if r.get("valid") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    shortlist = choose_shortlist(ranked, cfg["shortlist"], lambda r: SEGMENT)
    for r in rows:
        if r.get("trackable"):
            sel = bool(r.get("shortlisted"))
            r["stages"] = [*r["stages"], stage("ai_panel", "pending" if sel else "not_sent"),
                           stage("final_approval", "pending" if sel else "not_applicable")]
    _save(run_id, signal_date.isoformat(), rows, segment=SEGMENT)
    journal_add([journal_row(r, signal_date, first, last, cfg["entry_window"][1])
                 for r in rows if r.get("trackable")])
    rejected = {}
    for r in rows:
        if not r.get("trackable"):
            rejected[r["reject_reason"]] = rejected.get(r["reject_reason"], 0) + 1
    return _finish_run(run_id, "ok", {
        "signal_date": signal_date.isoformat(), "entry_day": first.isoformat(), "time_stop": last.isoformat(),
        "regime": regime, "stocks": len(universe), "patterns": len([r for r in rows if r.get("trackable")]),
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
        "entry": r["entry"], "stop": r["stop"], "target": r["target"], "rr": r.get("rr"), "atr": None,
        "entry_date": first.isoformat(), "fill_until": f"{first.isoformat()}T{fill_until}",
        "time_stop_date": last.isoformat(),
        "extra": {"pattern": r.get("pattern"), "turnover_cr": r.get("turnover_cr")},
        "stages": r.get("stages"), "rule_version": r.get("rule_version"),
    }
