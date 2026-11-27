"""Hourly research strategies (shadow) — run in the live market after every completed 1H bar
(asked 27 Nov 2026). Both trade with an initial stop, a fixed target and the same managed exit as
the live futures books: stop to breakeven at ``breakeven_at_r``, then a trailing stop
``trail_atr`` × ATR behind the best close from ``trail_start_r``.

``htf_pullback`` — the papers' "slow trend picks the direction, the hourly chart only times the
  entry" (Sepp & Lucic; bittrends found no edge in pure intraday trend following):
  direction = daily EWMA 20 vs 250 beyond a daily-ATR buffer (days before the bar's date only);
  entry = a 1H bar touched the 1H EMA20 (± ``touch_atr`` ATR) within ``pullback_bars`` and the
  bar closes beyond the last ``trigger_lookback`` bars' extreme and the EMA, once per pullback;
  stop = pullback extreme ∓ ``stop_buffer_atr`` ATR (between ``min_stop_atr`` and ``max_stop_atr``).
``trend_4h`` — Sepp & Lucic's EWMA crossover on 4H bars (session-anchored, resampled from 1H):
  a fresh cross beyond ``buffer_atr`` × 4H ATR; stop ``stop_atr`` × 4H ATR.

Signals use completed bars only and fill at the next 1H bar's open; stop and target are checked
on each 1H bar's high/low (gap → the open, both touched → the stop). Markets: MCX minis (their
own stitched hourly series, see ``record_mcx_hourly``), NIFTY/BANKNIFTY and the most liquid F&O
stocks (NSE spot hourly bars — no contract rolls; ₹ from today's futures lot).
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db
from backend.research import config as rcfg
from backend.research.data import daily_bars, ensure_tables as ensure_md_tables, group_of, kind_of, point_value
from backend.research.trend import _close_trade, _excursion, _mark_open, _open_trade, ewma, ewma_signal
from backend.signals.indicators import atr as wilder_atr, ema
from tradingagents.utils.market_calendar import MCX, NSE, session_windows

HOURLY_KEYS = ("htf_pullback", "trend_4h")


# --- hourly bars --------------------------------------------------------------------------------

def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS research_hourly_bars (
                            symbol TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
                            volume INTEGER, contract TEXT, PRIMARY KEY (symbol, ts))""")


def record_mcx_hourly() -> dict:
    """Append the active MCX mini contract's completed 1H bars to one continuous series per
    symbol (the contract changes at each roll; the series keeps going)."""
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import active_future
    from backend.marketdata.store import MCX_UNDERLYINGS
    ensure_md_tables()
    ensure_table()
    added = {}
    for name in MCX_UNDERLYINGS:
        fut = active_future(name, segment="MCX-FUT")
        if not fut:
            continue
        with get_db() as conn:
            last = conn.execute("SELECT MAX(ts) FROM research_hourly_bars WHERE symbol = ?", (name,)).fetchone()[0]
        rows = [r for r in get_candles(fut["instrument_token"], "60minute", limit=3000) if not last or r["ts"] > last]
        with get_db() as conn:
            conn.executemany("INSERT OR IGNORE INTO research_hourly_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             [(name, r["ts"], r["open"], r["high"], r["low"], r["close"], r.get("volume") or 0,
                               fut["tradingsymbol"]) for r in rows])
        added[name] = len(rows)
    return added


def hourly_bars(symbol: str, limit: int = 6000) -> list[dict]:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.marketdata.store import MCX_UNDERLYINGS
    ensure_md_tables()
    if symbol in MCX_UNDERLYINGS:
        ensure_table()
        with get_db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT ts, open, high, low, close, volume FROM research_hourly_bars WHERE symbol = ? "
                "ORDER BY ts DESC LIMIT ?", (symbol, limit))]
        rows.reverse()
        return rows
    spot = spot_instrument(symbol)
    return get_candles(spot["instrument_token"], "60minute", limit=limit) if spot else []


def partial_today(symbol: str, last_daily: str | None) -> dict | None:
    """Today's bar so far from completed hourly bars after the last stored daily bar (live runs);
    ``partial: True`` so it can hit a stop/target but never start a trade."""
    rows = [r for r in hourly_bars(symbol, limit=40) if not last_daily or r["ts"][:10] > last_daily]
    if not rows:
        return None
    day = rows[-1]["ts"][:10]
    rows = [r for r in rows if r["ts"][:10] == day]
    return {"ts": day + "T00:00:00", "open": rows[0]["open"], "high": max(r["high"] for r in rows),
            "low": min(r["low"] for r in rows), "close": rows[-1]["close"],
            "volume": sum(r.get("volume") or 0 for r in rows), "partial": True}


# --- universe -----------------------------------------------------------------------------------

def universe(p: dict, group: str | None = None) -> list[str]:
    out = []
    if group in (None, "mcx"):
        out += list(p.get("mcx") or [])
    if group in (None, "nse"):
        out += list(p.get("indices") or [])
        n = int(p.get("stocks_top_n") or 0)
        if n:
            out += liquid_stocks(n, float(p.get("min_turnover_cr", 100)))
    return out


def liquid_stocks(n: int, min_cr: float) -> list[str]:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe
    ranked = []
    for s in fno_universe():
        if not s["spot_token"]:
            continue
        bars = get_candles(s["spot_token"], "day", limit=20)
        if len(bars) < 15:
            continue
        tv = sum(b["close"] * (b.get("volume") or 0) for b in bars) / len(bars)
        if tv >= min_cr * 1e7:
            ranked.append((tv, s["symbol"]))
    return [sym for _, sym in sorted(ranked, reverse=True)[:n]]


# --- engine -------------------------------------------------------------------------------------

def _ctx(symbol: str) -> dict:
    kind = kind_of(symbol)
    pv, contract = point_value(symbol)
    return {"kind": kind, "pv": pv, "contract": contract, "cost_bps": rcfg.cost_bps(kind),
            "roll_bps": rcfg.cost_bps("roll"), "one_r": rcfg.one_r_rupees()}


def _stop_reason(t: dict, stop: float) -> str:
    sign = 1 if t["direction"] == "long" else -1
    if (stop - t["stop"]) * sign <= 1e-9:
        return "stop"
    return "breakeven_stop" if abs(stop - t["_be_level"]) <= 1e-6 * max(1.0, abs(stop)) else "trailing_stop"


def _hit(t: dict, bar: dict, stop: float) -> tuple[float, str] | None:
    sign = 1 if t["direction"] == "long" else -1
    o, h, l, target = bar["open"], bar["high"], bar["low"], t["target"]
    if (o - stop) * sign <= 0:
        return o, _stop_reason(t, stop)
    if (o - target) * sign >= 0:
        return o, "target"
    if ((l if sign > 0 else h) - stop) * sign <= 0:
        return stop, _stop_reason(t, stop)
    if ((h if sign > 0 else l) - target) * sign >= 0:
        return target, "target"
    return None


def run_1h(bars: list[dict], signal_fn: Callable[[int], dict | None], p: dict, symbol: str, ctx: dict,
           timeframe: str, start: str | None = None, end: str | None = None) -> list[dict]:
    """Bar-by-bar on 1H bars: ``signal_fn(i)`` decides on bar i's close; fills at bar i+1's open."""
    trades: list[dict] = []
    pos: dict | None = None
    stop = 0.0
    be_at, lock = float(p.get("breakeven_at_r", 1.0)), float(p.get("breakeven_lock_r", 0.0))
    trail_from, trail_k = float(p.get("trail_start_r", 1.0)), float(p.get("trail_atr", 2.0))
    max_hold = int(p.get("max_hold_bars", 70))
    for i, bar in enumerate(bars):
        if pos is not None:
            sign = 1 if pos["direction"] == "long" else -1
            hit = _hit(pos, bar, stop)
            if hit:
                t = _close_trade(pos, hit[0], bar["ts"], hit[1], ctx)
                t["exit_ts"] = bar["ts"]
                trades.append(t)
                pos = None
                continue
            _excursion(pos, bar["close"])
            pos["_bars"] += 1
            risk = pos["_risk_pts"]
            if pos["mfe_r"] >= be_at:
                stop = max(stop, pos["_be_level"]) if sign > 0 else min(stop, pos["_be_level"])
            if pos["mfe_r"] >= trail_from:
                trail = bar["close"] - sign * trail_k * pos["_trail_atr"]
                stop = max(stop, trail) if sign > 0 else min(stop, trail)
            if pos["_bars"] >= max_hold and i + 1 < len(bars):
                t = _close_trade(pos, bar["close"], bar["ts"], "time", ctx)
                t["exit_ts"] = bar["ts"]
                trades.append(t)
                pos = None
            continue
        if i + 1 >= len(bars):
            break
        nxt = bars[i + 1]
        if (start and nxt["ts"][:10] < start) or (end and nxt["ts"][:10] > end):
            continue
        sig = signal_fn(i)
        if not sig:
            continue
        sign = 1 if sig["direction"] == "long" else -1
        entry = nxt["open"]
        risk = (entry - sig["stop"]) * sign
        if risk <= 0 or risk > float(p.get("max_stop_atr", 3.0)) * 1.5 * sig["atr"]:
            continue                                       # opened through / far beyond the planned stop
        pos = _open_trade(symbol, sig["direction"], nxt["ts"], entry, risk, ctx,
                          {"signal_ts": bar["ts"], "atr": round(sig["atr"], 2), "contract": ctx.get("contract"),
                           **sig.get("meta", {})})
        pos.update(key=f"{symbol}|{sig['direction']}|{nxt['ts']}", entry_ts=nxt["ts"], timeframe=timeframe,
                   target=round(entry + sign * float(p.get("target_r", 3.0)) * risk, 2),
                   _bars=0, _trail_atr=sig.get("trail_atr", sig["atr"]),
                   _be_level=entry + sign * lock * risk)
        stop = pos["stop"]
    if pos is not None:
        t = _mark_open(pos, bars[-1]["close"], bars[-1]["ts"], stop, ctx)
        t["extra"]["last_ts"] = bars[-1]["ts"]
        trades.append(t)
    return trades


# --- signals ------------------------------------------------------------------------------------

def daily_direction(daily: list[dict], p: dict) -> tuple[list[str], list[int]]:
    """(dates, direction after each daily close: 1 / -1 / 0) for the 20/250 EWMA filter."""
    closes = [b["close"] for b in daily]
    f, s = ewma(closes, int(p["daily_fast"])), ewma(closes, int(p["daily_slow"]))
    a = wilder_atr(daily, 14)
    dirs = [0 if (k < int(p["daily_slow"]) or a[k] is None)
            else ewma_signal(f[k], s[k], a[k], float(p["daily_buffer_atr"])) for k in range(len(daily))]
    return [b["ts"][:10] for b in daily], dirs


def pullback_signals(bars: list[dict], daily: list[dict], p: dict) -> Callable[[int], dict | None]:
    closes = [b["close"] for b in bars]
    e = ema(closes, int(p["ema"]))
    a = wilder_atr(bars, int(p["atr_period"]))
    ddates, ddirs = daily_direction(daily, p)
    pb, look = int(p["pullback_bars"]), int(p["trigger_lookback"])
    touch, buf = float(p["touch_atr"]), float(p["stop_buffer_atr"])
    lo_atr, hi_atr = float(p["min_stop_atr"]), float(p["max_stop_atr"])
    used: dict[int, int] = {}                     # last pullback start used per direction

    def fn(i: int) -> dict | None:
        if i < max(pb, look) + 1 or e[i] is None or a[i] is None:
            return None
        k = bisect.bisect_left(ddates, bars[i]["ts"][:10]) - 1
        if k < 0 or ddirs[k] == 0:
            return None
        sign = ddirs[k]
        if (sign > 0 and not p.get("long_enabled", True)) or (sign < 0 and not p.get("short_enabled", True)):
            return None
        touches = [j for j in range(i - pb + 1, i) if e[j] is not None and a[j] is not None and (
            (bars[j]["low"] <= e[j] + touch * a[j]) if sign > 0 else (bars[j]["high"] >= e[j] - touch * a[j]))]
        if not touches:
            return None
        prior = bars[i - look:i]
        trig = (closes[i] > max(b["high"] for b in prior) and closes[i] > e[i] and closes[i] > bars[i]["open"]) \
            if sign > 0 else \
            (closes[i] < min(b["low"] for b in prior) and closes[i] < e[i] and closes[i] < bars[i]["open"])
        if not trig or used.get(sign, -1) >= touches[0]:
            return None
        used[sign] = i
        window = bars[i - pb + 1:i + 1]
        anchor = (min(b["low"] for b in window) - buf * a[i]) if sign > 0 else (max(b["high"] for b in window) + buf * a[i])
        dist = (closes[i] - anchor) * sign
        if dist < lo_atr * a[i]:
            anchor = closes[i] - sign * lo_atr * a[i]
        elif dist > hi_atr * a[i]:
            return None
        return {"direction": "long" if sign > 0 else "short", "stop": anchor, "atr": a[i],
                "meta": {"daily_trend": "up" if sign > 0 else "down", "ema": round(e[i], 2)}}
    return fn


def _bucket_index(bars: list[dict], exchange: str, hours: int = 4) -> tuple[list[dict], list[int], list[bool]]:
    """4H bars resampled from 1H, the 4H index of each 1H bar, and whether that 1H bar completes
    its 4H bar."""
    from backend.marketdata.candles import SESSION_ANCHOR, candle_end, resample_hours
    four = resample_hours(bars, exchange, hours)
    pos = {b["ts"]: k for k, b in enumerate(four)}
    ah, am = SESSION_ANCHOR[exchange]
    starts, ends = [], []
    idx, done = [], []
    for b in bars:
        ts = datetime.fromisoformat(b["ts"])
        anchor = ts.replace(hour=ah, minute=am, second=0)
        k = int((ts - anchor).total_seconds() // (hours * 3600))
        start = anchor + timedelta(hours=hours * k)
        windows = session_windows(exchange, ts.date())
        close = datetime.combine(ts.date(), windows[-1][1]) if windows else start + timedelta(hours=hours)
        starts.append(start)
        ends.append(min(start + timedelta(hours=hours), close))
        idx.append(pos.get(start.isoformat(timespec="seconds"), -1))
    for j, b in enumerate(bars):
        last_of_bucket = j + 1 < len(bars) and starts[j + 1] != starts[j]
        done.append(last_of_bucket or candle_end(datetime.fromisoformat(b["ts"]), "60minute", exchange) >= ends[j])
    return four, idx, done


def trend4h_signals(bars: list[dict], exchange: str, p: dict) -> Callable[[int], dict | None]:
    four, idx, done = _bucket_index(bars, exchange)
    closes = [b["close"] for b in four]
    f, s = ewma(closes, int(p["fast"])), ewma(closes, int(p["slow"]))
    a = wilder_atr(four, int(p["atr_period"]))
    state = [0 if (k < int(p["slow"]) or a[k] is None) else ewma_signal(f[k], s[k], a[k], float(p["buffer_atr"]))
             for k in range(len(four))]

    def fn(i: int) -> dict | None:
        b = idx[i]
        if b <= 0 or not done[i] or state[b] == 0 or state[b] == state[b - 1]:
            return None
        sign = state[b]
        if (sign > 0 and not p.get("long_enabled", True)) or (sign < 0 and not p.get("short_enabled", True)):
            return None
        return {"direction": "long" if sign > 0 else "short",
                "stop": bars[i]["close"] - sign * float(p["stop_atr"]) * a[b], "atr": a[b], "trail_atr": a[b],
                "meta": {"fast": round(f[b], 2), "slow": round(s[b], 2), "bar_4h": four[b]["ts"]}}
    return fn


# --- strategy entry points ----------------------------------------------------------------------

def run_symbol(key: str, symbol: str, p: dict, start: str | None = None, end: str | None = None) -> tuple[list[dict], dict]:
    bars = hourly_bars(symbol)
    exchange = MCX if group_of(symbol) == "mcx" else NSE
    state: dict = {"hourly_bars": len(bars), "last_bar": bars[-1]["ts"] if bars else None}
    if len(bars) < 60:
        state["note"] = "not enough hourly bars yet"
        return [], state
    if key == "htf_pullback":
        daily = daily_bars(symbol)
        if len(daily) < int(p["daily_slow"]) + 5:
            state["note"] = f"{len(daily)} daily bars — the 250-day trend needs more (download research history)"
            return [], state
        dates, dirs = daily_direction(daily, p)
        state["daily_trend"] = {1: "up", -1: "down", 0: "flat"}[dirs[-1]]
        fn = pullback_signals(bars, daily, p)
    else:
        fn = trend4h_signals(bars, exchange, p)
    trades = run_1h(bars, fn, p, symbol, _ctx(symbol), "1h" if key == "htf_pullback" else "4h", start, end)
    open_t = next((t for t in trades if t["status"] == "open"), None)
    state["position"] = open_t["direction"] if open_t else "flat"
    if open_t:
        state.update(stop_now=open_t["extra"].get("stop_now"), target=open_t.get("target"), r_now=open_t["pnl_r"])
    return trades, state


def live(key: str, today: date | None = None, group: str | None = None) -> dict:
    from backend.research import journal
    p = rcfg.strategy(key)
    if group in (None, "mcx"):
        record_mcx_hourly()
    symbols = universe(p, group)
    warm = (date.fromisoformat(rcfg.live_start()) - timedelta(days=5)).isoformat()
    trades, states = [], {}
    for sym in symbols:
        t, st = run_symbol(key, sym, p, start=warm)
        trades += t
        states[sym] = st
    synced = journal.sync(key, trades, rcfg.live_start(), symbols=symbols)
    return {"status": "ok", **synced, "symbols": len(symbols), "states": states}


def status(key: str) -> dict:
    p = rcfg.strategy(key)
    warm = (date.fromisoformat(rcfg.live_start()) - timedelta(days=5)).isoformat()
    states = {}
    for sym in universe(p):
        _, st = run_symbol(key, sym, p, start=warm)
        states[sym] = st
    return {"settings": p, "states": states}


def backtest(key: str, params: dict, progress=None) -> dict:
    p = {**rcfg.strategy(key), **(params.get("overrides") or {})}
    symbols = universe(p)
    trades = []
    for n, sym in enumerate(symbols, 1):
        t, _ = run_symbol(key, sym, p, params.get("start_date"), params.get("end_date"))
        trades += t
        if progress:
            progress(f"{sym} ({n}/{len(symbols)})")
    return {"trades": trades, "settings": p}


LIMITS = [
    "NSE: signals and fills on the spot (index/stock) hourly bars; futures basis not modelled, a monthly roll "
    "cost is charged; ₹ uses today's lot size.",
    "MCX: a continuous hourly series is stitched from the active mini contract from the day the research "
    "section started; older backtests only reach as far as the current contract's stored hourly bars.",
    "Hourly history: NSE about 12 months after the Rule Backtest history download; MCX only a few months.",
    "Fills at the next 1H open; stop/target on 1H highs and lows (stop first when both are touched).",
]


def entry(key: str) -> dict:
    return {"key": key, "page": "hourly", "kind": "trades", "live_after": "both", "hourly": True,
            "live": lambda today=None, group=None: live(key, today, group),
            "backtest": lambda params, progress=None: backtest(key, params, progress),
            "status": lambda: status(key), "limitations": LIMITS}
