"""Opening range breakout (ORB) — an intraday strategy for stock futures.

Agreed 21 Nov 2026: one of the stock-futures strategies (journal segment
``stock_futures``, setup ``orb``), sharing the F&O book limits; universe = the
most liquid F&O stocks (``instruments.stock_futures.orb_universe_size``).
All numbers come from ``strategies.orb`` in config/decision_engine.yaml.

* Opening range = the first ``range_minutes`` (30) of the session, from
  ``bar_minutes`` (10) bars of the traded futures contract.
* Signal: the first 10-minute close above the range high (long) or below the
  range low (short) on that day. No entry-time limit beyond the general rule
  (no entry in the last 30 minutes).
* Stop: the other side of the range; the range midpoint when the range is wide
  (> ``wide_range_atr`` × daily ATR). Target ``min_rr`` (2R). Exit at
  ``exit_time`` (15:15) the same day. One ORB trade per stock per day.
* Hard (Stage A): range width within [``min_range_atr``, ``max_range_atr``] ×
  daily ATR, opening gap ≤ ``max_gap_pct``, a valid stop, plus the usual F&O
  ban / events today / data / spread / lot-size checks in the scan.
* Scored: breakout volume vs the same time slot on previous days, NIFTY moving
  the same way today, strength vs NIFTY today, range quality, close strength.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import atr
from backend.signals.setups import stage

SETUP = "orb"
SCAN_SEGMENT = "fno_orb"


def settings() -> dict:
    cfg = get_config()
    st = cfg.strategy(SETUP)
    x = st.model_extra or {}
    inst = cfg.instrument("stock_futures").model_extra or {}
    return {
        "enabled": bool(inst.get("orb_enabled", True)) and cfg.instrument("stock_futures").enabled,
        "universe_size": int(inst.get("orb_universe_size", 25)),
        "range_minutes": int(x.get("range_minutes", 30)), "bar_minutes": int(x.get("bar_minutes", 10)),
        "wide_range_atr": float(x.get("wide_range_atr", 0.6)),
        "min_range_atr": float(x.get("min_range_atr", 0.15)), "max_range_atr": float(x.get("max_range_atr", 1.2)),
        "max_gap_pct": float(x.get("max_gap_pct", 3.0)), "entry_valid_bars": int(x.get("entry_valid_bars", 2)),
        "exit_time": x.get("exit_time", "15:15"), "volume_multiplier": st.volume_multiplier,
        "volume_days": st.lookback, "atr_period": st.atr_period, "min_rr": st.min_rr,
        "weights": x.get("score_weights", {"volume": 30, "nifty_alignment": 25, "relative_strength": 15,
                                           "range_quality": 15, "close_strength": 15}),
        "mode": cfg.global_.selection.mode,
    }


def _slot_volume_ratio(signal: dict, prev_bars: Sequence[dict], days: int) -> float:
    slot = signal["ts"][11:16]
    same = [b.get("volume") or 0 for b in prev_bars if b["ts"][11:16] == slot][-days:]
    avg = sum(same) / len(same) if same else 0
    return (signal.get("volume") or 0) / avg if avg > 0 else 0.0


def _day_change(day_bars: Sequence[dict]) -> float | None:
    if not day_bars or not day_bars[0]["open"]:
        return None
    return day_bars[-1]["close"] / day_bars[0]["open"] - 1


def evaluate_orb(day_bars: Sequence[dict], daily: Sequence[dict], *, prev_bars: Sequence[dict] = (),
                 nifty_day_bars: Sequence[dict] | None = None, is_stock: bool = True,
                 volume_available: bool = True, cfg: dict | None = None) -> dict:
    """``day_bars``: today's 10-minute bars from the open up to and including the
    signal bar. ``daily``: completed daily bars before today. ``prev_bars``:
    10-minute bars of earlier days (volume reference). Returns one candidate."""
    cfg = cfg or settings()

    def reject(reason: str, **extra) -> dict:
        return {"setup": SETUP, "direction": extra.pop("direction", None), "valid": False, "trackable": False,
                "reject_reason": reason, "stages": [stage("setup", "fail", reason)], **extra}

    n_or = max(1, cfg["range_minutes"] // cfg["bar_minutes"])
    if len(day_bars) <= n_or:
        return reject("opening range not complete")
    if day_bars[0]["ts"][11:16] != "09:15":
        return reject("today's bars don't start at 09:15")
    opening, after = day_bars[:n_or], day_bars[n_or:]
    hi, lo = max(b["high"] for b in opening), min(b["low"] for b in opening)
    signal = after[-1]
    if signal["close"] > hi:
        s, direction = 1, "long"
    elif signal["close"] < lo:
        s, direction = -1, "short"
    else:
        return reject("no close outside the opening range")
    if any(s * (b["close"] - (hi if s > 0 else lo)) > 0 for b in after[:-1]):
        return reject("not the first breakout of the day", direction=direction)
    a = atr(list(daily), cfg["atr_period"])[-1] if len(daily) > cfg["atr_period"] else None
    if not a:
        return reject("daily ATR not available", direction=direction)
    width = hi - lo
    if width < cfg["min_range_atr"] * a or width > cfg["max_range_atr"] * a:
        return reject(f"opening range {width / a:.2f} ATR outside {cfg['min_range_atr']}–{cfg['max_range_atr']} ATR",
                      direction=direction)
    gap = abs(day_bars[0]["open"] / daily[-1]["close"] - 1) * 100
    if gap > cfg["max_gap_pct"]:
        return reject(f"opening gap {gap:.1f}% > {cfg['max_gap_pct']}%", direction=direction)
    wide = width > cfg["wide_range_atr"] * a
    stop = (hi + lo) / 2 if wide else (lo if s > 0 else hi)
    entry = signal["close"]
    risk = s * (entry - stop)
    if risk <= 0:
        return reject("no identifiable stop", direction=direction)
    target = entry + s * cfg["min_rr"] * risk

    vol_ratio = _slot_volume_ratio(signal, prev_bars, cfg["volume_days"]) if volume_available else None
    nifty_change = _day_change(nifty_day_bars) if nifty_day_bars else None
    stock_change = _day_change(day_bars)
    aligned = None if nifty_change is None else s * nifty_change > 0
    stronger = None if (nifty_change is None or stock_change is None or not is_stock) \
        else s * (stock_change - nifty_change) > 0
    rng = signal["high"] - signal["low"]
    close_pos = ((signal["close"] - signal["low"]) / rng if s > 0 else (signal["high"] - signal["close"]) / rng) \
        if rng > 0 else 0.0
    w = cfg["weights"]
    raw = {
        "volume": None if vol_ratio is None else
        (1.0 if vol_ratio >= 2 else 0.75 if vol_ratio >= cfg["volume_multiplier"] else 0.4 if vol_ratio >= 1 else 0),
        "nifty_alignment": None if aligned is None else (1.0 if aligned else 0.0),
        "relative_strength": None if stronger is None else (1.0 if stronger else 0.0),
        "range_quality": 1.0 if 0.25 * a <= width <= cfg["wide_range_atr"] * a else 0.5,
        "close_strength": 1.0 if close_pos >= 0.75 else 0.5 if close_pos >= 0.6 else 0.0,
    }
    factors = {k: (None if v is None else round(v * w[k], 1)) for k, v in raw.items()}
    possible = sum(w[k] for k, v in factors.items() if v is not None)
    score = round(100 * sum(v for v in factors.values() if v is not None) / possible, 1) if possible else 0.0
    soft = {"volume": None if vol_ratio is None else vol_ratio >= cfg["volume_multiplier"],
            "nifty_alignment": aligned}
    failed = []
    if cfg["mode"] == "strict" and soft["volume"] is False:
        failed.append(f"volume {vol_ratio:.1f}x < {cfg['volume_multiplier']}x")
    min_score = get_config().global_.scoring.min_shortlist_score
    return {
        "setup": SETUP, "direction": direction, "valid": not failed, "trackable": True,
        "reject_reason": "; ".join(failed) or None, "entry": round(entry, 2), "stop": round(stop, 2),
        "target": round(target, 2), "rr": cfg["min_rr"], "risk_per_unit": round(risk, 4), "atr": round(a, 4),
        "or_high": round(hi, 2), "or_low": round(lo, 2), "stop_type": "midpoint" if wide else "range",
        "range_atr": round(width / a, 2), "gap_pct": round(gap, 2),
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2), "soft_checks": soft,
        "factors": factors, "score": score, "rule_version": get_config().rule_version,
        "stages": [stage("setup", "pass", or_high=round(hi, 2), or_low=round(lo, 2)),
                   stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2),
                         target=round(target, 2), stop_type="midpoint" if wide else "range"),
                   stage("scoring", "pass" if score >= min_score else "below_threshold", score=score)],
    }


def mark_exit(bars: list[dict], exit_time: str, bar_minutes: int) -> list[dict]:
    """Keep bars up to the one that ends at/after ``exit_time`` each day and flag it ``eod``."""
    out, done_day = [], None
    for b in bars:
        day = b["ts"][:10]
        if day == done_day:
            continue
        start = datetime.fromisoformat(b["ts"])
        end = (start + timedelta(minutes=bar_minutes)).strftime("%H:%M")
        eod = end >= exit_time
        out.append({**b, "eod": eod})
        if eod:
            done_day = day
    return out


# --- live scan ---------------------------------------------------------------------------

def orb_universe(today: date, size: int | None = None) -> list[dict]:
    """The most liquid F&O stocks by 20-day spot turnover, with their active future."""
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import active_future, fno_universe
    from backend.signals.cash_swing import avg_turnover

    size = size or settings()["universe_size"]
    ranked = []
    for stock in fno_universe(today):
        if not stock["spot_token"]:
            continue
        daily = get_candles(stock["spot_token"], "day", limit=25)
        if len(daily) < 20:
            continue
        ranked.append((avg_turnover(daily), stock))
    out = []
    for _, stock in sorted(ranked, key=lambda x: -x[0])[:size]:
        fut = active_future(stock["symbol"], today=today)
        if fut:
            out.append({**stock, "contract": fut})
    return out


def latest_bar_start(now: datetime, bar_minutes: int) -> datetime | None:
    from backend.scheduler import latest_closed_bar
    end = latest_closed_bar("NSE", "10minute", now)
    return end - timedelta(minutes=bar_minutes) if end else None


def run_orb_scan(now: datetime | None = None, client=None, force: bool = False) -> dict:
    from backend.journal.store import add_records as journal_add, list_records
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import ban_status, spot_instrument
    from backend.signals import events
    from backend.signals.cash_swing import (IST, _finish_run, _save, _start_run, data_quality,
                                            ensure_scan_tables, min_score)
    from backend.signals.fno import _journal_row, check_spreads, risk_per_trade
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import NSE, previous_trading_day, session_windows

    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "ORB disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    windows = session_windows(NSE, now.date())
    if not windows:
        return {"status": "skipped", "reason": "NSE closed today"}
    bar_start = latest_bar_start(now, cfg["bar_minutes"])
    if bar_start is None:
        return {"status": "skipped", "reason": "no completed 10-minute bar yet"}
    signal_end = bar_start + timedelta(minutes=cfg["bar_minutes"])
    open_dt = datetime.combine(now.date(), windows[0][0])
    close_dt = datetime.combine(now.date(), windows[-1][1])
    last_min = int((get_config().instrument("stock_futures").model_extra or {}).get("no_entry_last_minutes", 30))
    cutoff = close_dt - timedelta(minutes=last_min)
    if signal_end <= open_dt + timedelta(minutes=cfg["range_minutes"]):
        return {"status": "skipped", "reason": "opening range not complete"}
    if signal_end >= cutoff and not force:
        return {"status": "skipped", "reason": f"entry would start in the last {last_min} minutes"}

    run_id = _start_run(now.date().isoformat(), now, segment=SCAN_SEGMENT)
    today = now.date()
    market = events.market_events_between(today, today, kinds=("RBI", "BUDGET"))
    if not market["verified"] or market["events"]:
        why = "event calendar not verified" if not market["verified"] else \
            ", ".join(f"{e['event']} today" for e in market["events"])
        return _finish_run(run_id, "blocked", {"reason": why})
    nifty = spot_instrument("NIFTY")
    nifty_bars = [b for b in get_candles(nifty["instrument_token"], "10minute", limit=60)
                  if b["ts"][:10] == today.isoformat() and b["ts"] <= bar_start.isoformat()] if nifty else []
    ban = ban_status(today)
    banned = set(ban["symbols"])
    already = {r["symbol"] for r in list_records("setup = ? AND signal_date = ?", (SETUP, today.isoformat()))}
    expected_daily = previous_trading_day(today)
    rows = []
    for stock in orb_universe(today, cfg["universe_size"]):
        sym, contract = stock["symbol"], stock["contract"]
        base = {"symbol": sym, "segment": "stock_futures", "token": contract["instrument_token"],
                "tradingsymbol": contract["tradingsymbol"], "expiry": contract["expiry"]}

        def reject(reason: str) -> None:
            rows.append({**base, "valid": False, "reject_reason": reason,
                         "stages": [stage("eligibility", "fail", reason)]})

        if sym in already:
            reject("ORB already taken for this stock today")
            continue
        if ban["status"] != "ok":
            reject("F&O ban list not verified today")
            continue
        if sym in banned:
            reject("in F&O ban")
            continue
        ok, why = events.results_clear(sym, today, today, today)
        if not ok:
            reject(why)
            continue
        if date.fromisoformat(contract["expiry"]) <= today:
            reject("contract expires today")
            continue
        bars = get_candles(contract["instrument_token"], "10minute", limit=600)
        day_bars = [b for b in bars if b["ts"][:10] == today.isoformat() and b["ts"] <= bar_start.isoformat()]
        prev_bars = [b for b in bars if b["ts"][:10] < today.isoformat()]
        if not day_bars or day_bars[-1]["ts"] != bar_start.isoformat(timespec="seconds"):
            reject("10-minute data not up to date")
            continue
        expected = int((signal_end - open_dt).total_seconds() // (60 * cfg["bar_minutes"]))
        if len(day_bars) != expected:
            reject(f"missing 10-minute bars today ({len(day_bars)} of {expected})")
            continue
        daily = get_candles(stock["spot_token"], "day", limit=60)
        daily = [b for b in daily if b["ts"][:10] < today.isoformat()]
        problem = data_quality(daily, expected_daily, min_bars=30)
        if problem:
            reject(f"daily data: {problem}")
            continue
        c = evaluate_orb(day_bars, daily, prev_bars=prev_bars, nifty_day_bars=nifty_bars, cfg=cfg)
        c["stages"] = [stage("eligibility", "pass"), *c["stages"]]
        if c.get("trackable"):
            lots = int(risk_per_trade() // (c["risk_per_unit"] * contract["lot_size"])) if c["risk_per_unit"] else 0
            if lots < 1:
                c.update(trackable=False, valid=False, reject_reason="1 lot risks more than the per-trade limit")
            else:
                c["lots"] = lots
                c["value_per_point"] = contract["lot_size"]
        rows.append({**base, **c})

    tracked = [r for r in rows if r.get("trackable")]
    if tracked:
        from backend.brokers.kite import get_authenticated_client
        try:
            client = client or get_authenticated_client()
            spreads = check_spreads(client, sorted({f"NFO:{r['tradingsymbol']}" for r in tracked}))
        except Exception:
            spreads = {}
        max_spread = float((get_config().instrument("stock_futures").model_extra or {}).get("max_spread_pct", 0.1))
        for r in tracked:
            spread = spreads.get(f"NFO:{r['tradingsymbol']}")
            r["spread_pct"] = spread
            if spread is None or spread > max_spread:
                r.update(trackable=False, valid=False, reject_reason="liquidity not verified" if spread is None
                         else f"bid-ask spread {spread:.2f}% > {max_spread}%")
                r["stages"].append(stage("eligibility", "fail", r["reject_reason"]))
    ranked = sorted((r for r in rows if r.get("valid") and r.get("trackable") and r["score"] >= min_score()),
                    key=lambda r: r["score"], reverse=True)
    for r in rows:
        if r.get("trackable"):
            r["shortlisted"] = r in ranked
            r["stages"] += [stage("ai_panel", "pending" if r["shortlisted"] else "not_sent"),
                            stage("final_approval", "pending" if r["shortlisted"] else "not_applicable")]
            r["regime"] = None
    _save(run_id, today.isoformat(), rows, segment=SCAN_SEGMENT)
    fill_until = min(signal_end + timedelta(minutes=cfg["entry_valid_bars"] * cfg["bar_minutes"]), cutoff)
    records = []
    for r in rows:
        if r.get("trackable"):
            rec = _journal_row(r, today, bar_start, fill_until, today, NSE)
            rec["extra"].update({"bar_interval": "10minute", "bar_minutes": cfg["bar_minutes"],
                                 "exit_time": cfg["exit_time"], "or_high": r["or_high"], "or_low": r["or_low"],
                                 "stop_type": r["stop_type"]})
            records.append(rec)
    journal_add(records)
    return _finish_run(run_id, "ok", {
        "signal_bar": bar_start.isoformat(timespec="minutes"), "instruments": len(rows),
        "breakouts": len([r for r in rows if r.get("trackable")]), "shortlisted": [r["symbol"] for r in ranked],
        "journal_records": len(records), "rule_version": get_config().rule_version,
    })
