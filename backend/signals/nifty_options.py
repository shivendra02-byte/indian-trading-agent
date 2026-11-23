"""NIFTY option buying on 5-minute bars (asked 23 Nov 2026) — shadow first.

Signals are read on the NIFTY index's own 5-minute bars; a bullish signal buys
the ATM call of the nearest weekly expiry, a bearish one the ATM put. Three
setups run side by side so the backtest and the shadow record can pick the best
(``instruments.nifty_options.setups``):

* ``opt_vwap_ema`` — close on the right side of the session average price and
  EMA fast beyond EMA slow, then the first close beyond the last
  ``structure_bars`` bars' extreme. The index has no volume, so the "VWAP" is a
  time-weighted session average of the typical price (``twap``).
* ``opt_orb`` — the first 5-minute close beyond the opening range
  (``orb_minutes``), once per direction per day.
* ``opt_supertrend`` — Supertrend(``supertrend_period``, ``supertrend_mult``)
  flips on the signal bar.

All three need the 15-minute trend (completed 15-minute bars, close vs their
EMA slow) to agree. Signals only from ``first_signal`` to ``last_signal`` (bar
end); none on the weekly expiry day from ``expiry_day_cutoff``; none on
RBI/Budget days. Stop beyond the extreme of the last ``stop_lookback`` bars plus
``stop_buffer_atr`` × ATR (5-minute ATR14), at least ``min_stop_atr`` and at most
``max_stop_atr`` ATR; target ``target_r`` R; the book's ``exit_management``
(breakeven at 1R, then trail) and everything out with the bar ending at
``exit_time``. Entry: limit at the signal close for ``entry_valid_bars`` bars.
At most ``max_signals_per_setup_day`` per setup per day.

Results are in R on the index. Option P&L is estimated with
``value_per_point`` = lot size × ``assumed_delta`` (ATM ≈ 0.5); the option's own
5-minute bars are stored from the signal on, so the real premium move can be
read too (``option_premium_move()``).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import atr as atr_series
from backend.signals.indicators import ema
from backend.signals.setups import stage

SEGMENT = "nifty_options"
SCAN_SEGMENT = "nifty_opt_5m"
SETUPS = ("opt_vwap_ema", "opt_orb", "opt_supertrend")
BAR = timedelta(minutes=5)
SESSION_OPEN = time(9, 15)


def settings() -> dict:
    cfg = get_config()
    base = {
        "enabled": False, "setups": list(SETUPS), "first_signal": "09:30", "last_signal": "14:30",
        "exit_time": "15:15", "expiry_day_cutoff": "13:00", "max_signals_per_setup_day": 2,
        "ema_fast": 9, "ema_slow": 21, "structure_bars": 3, "orb_minutes": 15,
        "supertrend_period": 10, "supertrend_mult": 3.0, "stop_lookback": 3, "stop_buffer_atr": 0.1,
        "min_stop_atr": 0.5, "max_stop_atr": 2.0, "target_r": 2.0, "entry_valid_bars": 2,
        "strike_step": 50, "lot_size": 65, "assumed_delta": 0.5, "underlying": "NIFTY",
    }
    try:
        inst = cfg.instrument(SEGMENT)
    except KeyError:
        return base
    return {**base, **(inst.model_extra or {}), "enabled": inst.enabled}


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _end(bar: dict) -> datetime:
    return datetime.fromisoformat(bar["ts"]) + BAR


# --- indicators ------------------------------------------------------------------------

def twap(day_bars: Sequence[dict]) -> float:
    """Session average of the typical price (the index has no volume for a real VWAP)."""
    tp = [(b["high"] + b["low"] + b["close"]) / 3 for b in day_bars]
    return sum(tp) / len(tp)


def supertrend_dirs(bars: Sequence[dict], period: int, mult: float) -> list[int | None]:
    """+1 / -1 per bar (None while ATR warms up), standard final-band Supertrend."""
    a = atr_series(bars, period)
    dirs: list[int | None] = []
    upper = lower = None
    d = None
    for i, b in enumerate(bars):
        if a[i] is None:
            dirs.append(None)
            continue
        mid = (b["high"] + b["low"]) / 2
        bu, bl = mid + mult * a[i], mid - mult * a[i]
        prev_close = bars[i - 1]["close"] if i else b["close"]
        upper = bu if upper is None or bu < upper or prev_close > upper else upper
        lower = bl if lower is None or bl > lower or prev_close < lower else lower
        if d is None:
            d = 1 if b["close"] >= mid else -1
        elif d == 1 and b["close"] < lower:
            d = -1
        elif d == -1 and b["close"] > upper:
            d = 1
        dirs.append(d)
    return dirs


def bars_15m(bars: Sequence[dict], until: datetime) -> list[dict]:
    """Completed 15-minute bars (session-anchored at 09:15) ending by ``until``."""
    groups: dict[tuple[str, int], list[dict]] = {}
    for b in bars:
        t = datetime.fromisoformat(b["ts"])
        slot = ((t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)) // 15
        groups.setdefault((b["ts"][:10], slot), []).append(b)
    out = []
    for (_, _), g in sorted(groups.items()):
        if len(g) < 3 or _end(g[-1]) > until:
            continue
        out.append({"ts": g[0]["ts"], "open": g[0]["open"], "high": max(x["high"] for x in g),
                    "low": min(x["low"] for x in g), "close": g[-1]["close"]})
    return out


def trend_15m(bars: Sequence[dict], until: datetime, slow: int) -> int:
    b15 = bars_15m(bars, until)
    if len(b15) < slow + 1:
        return 0
    e = ema([b["close"] for b in b15], slow)[-1]
    if e is None:
        return 0
    c = b15[-1]["close"]
    return 1 if c > e else -1 if c < e else 0


# --- setups ----------------------------------------------------------------------------

def _setup_direction(name: str, bars: Sequence[dict], day_bars: Sequence[dict], cfg: dict,
                     st_dirs: list | None = None) -> tuple[int, str]:
    """Direction (+1 / -1) the setup signals on the last bar, or (0, why not)."""
    last = bars[-1]
    if name == "opt_vwap_ema":
        k = cfg["structure_bars"]
        if len(day_bars) < k + 2:
            return 0, "not enough bars today"
        closes = [b["close"] for b in bars]
        fast, slow = ema(closes, cfg["ema_fast"])[-1], ema(closes, cfg["ema_slow"])[-1]
        if fast is None or slow is None:
            return 0, "EMAs not ready"
        avg = twap(day_bars)
        for s in (1, -1):
            if s * (last["close"] - avg) <= 0 or s * (fast - slow) <= 0:
                continue
            prior = day_bars[-k - 1:-1]
            level = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
            before = day_bars[-k - 2:-2]
            prev_level = max(b["high"] for b in before) if s > 0 else min(b["low"] for b in before)
            if s * (last["close"] - level) > 0 and s * (day_bars[-2]["close"] - prev_level) <= 0:
                return s, ""
        return 0, "no break with VWAP and EMAs aligned"
    if name == "opt_orb":
        n = cfg["orb_minutes"] // 5
        if len(day_bars) <= n:
            return 0, "opening range not complete"
        rng = day_bars[:n]
        hi, lo = max(b["high"] for b in rng), min(b["low"] for b in rng)
        after = day_bars[n:]
        for s, level in ((1, hi), (-1, lo)):
            beyond = [b for b in after if s * (b["close"] - level) > 0]
            if beyond and beyond[0] is after[-1]:
                return s, ""
        return 0, "no first close beyond the opening range"
    if name == "opt_supertrend":
        dirs = st_dirs if st_dirs is not None else supertrend_dirs(bars, cfg["supertrend_period"],
                                                                   cfg["supertrend_mult"])
        if len(dirs) < 2 or dirs[-1] is None or dirs[-2] is None:
            return 0, "Supertrend not ready"
        if dirs[-1] != dirs[-2]:
            return dirs[-1], ""
        return 0, "no Supertrend flip"
    return 0, f"unknown setup {name}"


def candidates(bars: Sequence[dict], cfg: dict | None = None, st_dirs: list | None = None,
               atr_values: list | None = None) -> list[dict]:
    """One row per configured setup for the last bar of ``bars`` (5-minute index bars,
    a few days for warm-up). Point in time: only bars up to the signal bar."""
    cfg = cfg or settings()
    last = bars[-1]
    day = last["ts"][:10]
    day_bars = [b for b in bars if b["ts"][:10] == day]
    end = _end(last)
    a = (atr_values or atr_series(bars, 14))[-1]
    rows = []
    trend = trend_15m(bars, end, cfg["ema_slow"])
    for name in cfg["setups"]:
        s, why = _setup_direction(name, bars, day_bars, cfg, st_dirs)
        base = {"setup": name, "signal_ts": last["ts"], "signal_end": end.isoformat(timespec="minutes")}
        if not s:
            rows.append({**base, "valid": False, "trackable": False, "reject_reason": why})
            continue
        direction = "long" if s > 0 else "short"
        if trend != s:
            rows.append({**base, "direction": direction, "valid": False, "trackable": False,
                         "reject_reason": "15-minute trend does not agree"})
            continue
        if not a:
            rows.append({**base, "direction": direction, "valid": False, "trackable": False,
                         "reject_reason": "no ATR"})
            continue
        look = day_bars[-cfg["stop_lookback"]:]
        extreme = min(b["low"] for b in look) if s > 0 else max(b["high"] for b in look)
        entry = last["close"]
        stop = extreme - s * cfg["stop_buffer_atr"] * a
        risk = abs(entry - stop)
        if risk < cfg["min_stop_atr"] * a:
            risk = cfg["min_stop_atr"] * a
            stop = entry - s * risk
        if risk > cfg["max_stop_atr"] * a:
            rows.append({**base, "direction": direction, "valid": False, "trackable": False,
                         "reject_reason": f"stop {risk / a:.1f} ATR away (max {cfg['max_stop_atr']:g})"})
            continue
        target = entry + s * cfg["target_r"] * risk
        rows.append({**base, "direction": direction, "valid": True, "trackable": True, "reject_reason": None,
                     "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
                     "risk_per_unit": round(risk, 2), "rr": cfg["target_r"], "atr": round(a, 4),
                     "score": 60.0, "factors": {"trend_15m": 1},
                     "stages": [stage("setup", "pass", setup=name),
                                stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2),
                                      target=round(target, 2))]})
    return rows


# --- option contract ---------------------------------------------------------------------

def atm_option(underlying: str, spot: float, direction: str, today: date, step: int) -> dict | None:
    """ATM call (long) / put (short) of the nearest expiry on or after ``today``."""
    from backend.db import get_db
    from backend.marketdata.store import ensure_marketdata_tables

    ensure_marketdata_tables()
    strike = round(spot / step) * step
    kind = "CE" if direction == "long" else "PE"
    with get_db() as conn:
        row = conn.execute(
            "SELECT instrument_token, tradingsymbol, expiry, strike, lot_size FROM instruments "
            "WHERE segment = 'NFO-OPT' AND name = ? AND instrument_type = ? AND strike = ? AND expiry >= ? "
            "ORDER BY expiry LIMIT 1", (underlying, kind, float(strike), today.isoformat())).fetchone()
    return dict(row) if row else None


def option_premium_move(rec: dict) -> dict | None:
    """Real premium at the fill and at the exit from the option's stored 5-minute bars."""
    from backend.marketdata.candles import get_candles
    extra = rec.get("extra") or {}
    token = extra.get("option_token")
    if not token or not rec.get("fill_ts"):
        return None
    bars = get_candles(token, "5minute", since=datetime.fromisoformat(rec["signal_ts"]))
    if not bars:
        return None
    at = lambda ts: next((b["close"] for b in bars if b["ts"] >= ts[:19]), None)  # noqa: E731
    entry = at(rec["fill_ts"])
    exit_ = at(rec["exit_ts"]) if rec.get("exit_ts") else bars[-1]["close"]
    if entry is None or exit_ is None:
        return None
    return {"entry_premium": entry, "exit_premium": exit_, "move_pct": round(100 * (exit_ / entry - 1), 1)}


# --- live scan ---------------------------------------------------------------------------

def open_option_contracts() -> list[dict]:
    """Option contracts of pending/open records, so their 5-minute bars stay stored."""
    from backend.journal.store import list_records
    out = {}
    for r in list_records(f"segment = '{SEGMENT}' AND track_status IN ('pending', 'open')", limit=200):
        extra = r.get("extra") or {}
        if extra.get("option_token"):
            out[extra["option_token"]] = {"instrument_token": extra["option_token"],
                                          "tradingsymbol": extra.get("tradingsymbol")}
    return list(out.values())


def run_scan(now: datetime | None = None, force: bool = False) -> dict:
    """After each synced NIFTY 5-minute bar: every setup on the latest completed bar."""
    from backend.journal.store import add_records as journal_add, list_records
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.signals import events
    from backend.signals.cash_swing import IST, _finish_run, _save, _start_run, ensure_scan_tables
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import NSE, session_windows

    ensure_scan_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "NIFTY option buying disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    today = now.date()
    if not session_windows(NSE, today):
        return {"status": "skipped", "reason": "NSE closed today"}
    spot = spot_instrument(cfg["underlying"])
    if not spot:
        return {"status": "skipped", "reason": f"{cfg['underlying']} instrument missing"}
    bars = [b for b in get_candles(spot["instrument_token"], "5minute", limit=400) if _end(b) <= now]
    if not bars or bars[-1]["ts"][:10] != today.isoformat():
        return {"status": "skipped", "reason": "no 5-minute bar today yet"}
    end = _end(bars[-1])
    if not force and (end.time() < _hm(cfg["first_signal"]) or end.time() > _hm(cfg["last_signal"])):
        return {"status": "skipped", "reason": f"outside {cfg['first_signal']}–{cfg['last_signal']}"}

    run_id = _start_run(today.isoformat(), now, segment=SCAN_SEGMENT)
    if now - end > timedelta(minutes=12) and not force:
        return _finish_run(run_id, "blocked", {"reason": f"5-minute data stale (last bar ended {end:%H:%M})"})
    market = events.market_events_between(today, today, kinds=("RBI", "BUDGET"))
    if not market["verified"] or market["events"]:
        why = "event calendar not verified" if not market["verified"] else \
            ", ".join(f"{e['event']} today" for e in market["events"])
        return _finish_run(run_id, "blocked", {"reason": why})
    taken: dict[str, int] = {}
    for r in list_records("segment = ? AND signal_date = ?", (SEGMENT, today.isoformat()), limit=100):
        taken[r["setup"]] = taken.get(r["setup"], 0) + 1
    rows, records = [], []
    for c in candidates(bars, cfg):
        row = {"symbol": cfg["underlying"], "segment": SEGMENT, "token": spot["instrument_token"], **c}
        if c.get("trackable"):
            if taken.get(c["setup"], 0) >= cfg["max_signals_per_setup_day"]:
                row.update(trackable=False, valid=False,
                           reject_reason=f"{cfg['max_signals_per_setup_day']} signals already today")
            else:
                opt = atm_option(cfg["underlying"], c["entry"], c["direction"], today, cfg["strike_step"])
                if not opt:
                    row.update(trackable=False, valid=False, reject_reason="ATM option not in the instrument master")
                elif opt["expiry"] == today.isoformat() and end.time() >= _hm(cfg["expiry_day_cutoff"]):
                    row.update(trackable=False, valid=False,
                               reject_reason=f"expiry day after {cfg['expiry_day_cutoff']}")
                else:
                    row["option"] = opt
        rows.append(row)
        if row.get("trackable"):
            opt = row["option"]
            lot = int(opt.get("lot_size") or cfg["lot_size"])
            fill_until = end + cfg["entry_valid_bars"] * BAR
            records.append({
                "segment": SEGMENT, "symbol": cfg["underlying"], "instrument_token": spot["instrument_token"],
                "exchange": "NFO", "signal_date": today.isoformat(), "signal_ts": c["signal_ts"],
                "setup": c["setup"], "direction": c["direction"], "regime": None, "score": c["score"],
                "factors": c["factors"], "soft_checks": None, "decision": "shortlisted", "reject_reason": None,
                "entry": c["entry"], "stop": c["stop"], "target": c["target"], "rr": c["rr"], "atr": c["atr"],
                "entry_date": today.isoformat(), "fill_until": fill_until.isoformat(timespec="seconds"),
                "time_stop_date": today.isoformat(),
                "extra": {"bar_interval": "5minute", "bar_minutes": 5, "exit_time": cfg["exit_time"],
                          "tradingsymbol": opt["tradingsymbol"], "option_token": opt["instrument_token"],
                          "strike": opt["strike"], "option_type": "CE" if c["direction"] == "long" else "PE",
                          "expiry": opt["expiry"], "lots": 1, "lot_size": lot,
                          "value_per_point": round(lot * float(cfg["assumed_delta"]), 2)},
                "stages": [*c["stages"], stage("ai_panel", "not_sent", "shadow strategy"),
                           stage("final_approval", "not_applicable")],
                "rule_version": get_config().rule_version,
            })
            taken[c["setup"]] = taken.get(c["setup"], 0) + 1
    _save(run_id, today.isoformat(), [{k: v for k, v in r.items() if k != "option"} for r in rows],
          segment=SCAN_SEGMENT)
    journal_add(records)
    from backend.journal.panel import review_after_scan
    panel = review_after_scan(now)    # shadow strategy: the panel skips these unless shadow is switched off
    return _finish_run(run_id, "ok", {
        "signal_bar": bars[-1]["ts"][11:16], "signals": [f"{r['setup']} {r['direction']}" for r in records],
        "rejected": {r["setup"]: r["reject_reason"] for r in rows if not r.get("trackable")},
        "options": [r["extra"]["tradingsymbol"] for r in records], "ai_panel": panel,
        "rule_version": get_config().rule_version,
    })
