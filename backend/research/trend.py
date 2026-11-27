"""Slow trend books on daily bars (research, shadow).

``trend_ewma`` — Sepp & Lucic's "American" system (Definition A.3):
  * long when EWMA(fast) > EWMA(slow) + buffer × ATR, short mirrored;
  * size fixed at entry so the initial stop (``stop_atr`` × ATR) is 1R;
  * the stop trails ``stop_atr`` × ATR behind each close and never loosens;
  * exit only when the close breaches the previous stop AND the entry signal is off
    (``exit_rule: stop_only`` exits on the breach alone).
``tsmom`` — time-series momentum: the average sign of the last M blocks of L days of
  volatility-scaled returns; hold long/short while |signal| ≥ threshold, re-decided once a
  week; a ``stop_atr`` trailing stop is the safety exit and defines 1R.

Decisions use the close of day t; orders fill at the open of day t+1 (no same-bar fills).
All results are in R after a cost model (research.yaml ``cost_bps``) including a roll per
calendar month held for futures.
"""

from __future__ import annotations

import math
from datetime import date
from itertools import product
from typing import Callable

from backend.research import config as rcfg
from backend.research.data import daily_bars, kind_of, point_value
from backend.signals.indicators import atr as wilder_atr


# --- indicators -------------------------------------------------------------------------------

def ewma(values: list[float], span: int) -> list[float]:
    a = 2.0 / (span + 1)
    out, cur = [], None
    for v in values:
        cur = v if cur is None else a * v + (1 - a) * cur
        out.append(cur)
    return out


def ewma_vol(returns: list[float | None], span: int) -> list[float | None]:
    """EWMA standard deviation of daily returns (zero mean), point in time."""
    a = 2.0 / (span + 1)
    out, var, n = [], None, 0
    for r in returns:
        if r is None:
            out.append(None)
            continue
        var = r * r if var is None else a * r * r + (1 - a) * var
        n += 1
        out.append(math.sqrt(var) if n >= span // 2 and var > 0 else None)
    return out


def is_rebalance(dates: list[str], i: int, weekday: int) -> bool:
    """First session on or after ``weekday`` (Mon=0) in its week."""
    d = date.fromisoformat(dates[i][:10])
    if d.weekday() < weekday:
        return False
    if i == 0:
        return True
    p = date.fromisoformat(dates[i - 1][:10])
    same_week = p.isocalendar()[:2] == d.isocalendar()[:2]
    return not same_week or p.weekday() < weekday


# --- trade bookkeeping ------------------------------------------------------------------------

def _months_between(a: str, b: str) -> int:
    da, db = date.fromisoformat(a[:10]), date.fromisoformat(b[:10])
    return max(0, (db.year - da.year) * 12 + db.month - da.month)


def _close_trade(t: dict, exit_price: float, exit_date: str, reason: str, ctx: dict) -> dict:
    sign = 1 if t["direction"] == "long" else -1
    risk = t["_risk_pts"]
    side_bps = ctx["cost_bps"]
    rolls = _months_between(t["entry_date"], exit_date) if ctx["kind"] != "cash" else 0
    cost_pts = (t["entry_price"] + exit_price) * side_bps / 1e4 + rolls * exit_price * ctx["roll_bps"] / 1e4
    gross_pts = (exit_price - t["entry_price"]) * sign
    t.update(exit_date=exit_date[:10], exit_price=round(exit_price, 2), exit_reason=reason, status="closed",
             pnl_r=round((gross_pts - cost_pts) / risk, 3))
    t["pnl_rupees"] = round((gross_pts - cost_pts) * ctx["pv"] * max(t["lots"] or 0, 1), 0) if ctx["pv"] else None
    t["extra"].update(rolls=rolls, cost_r=round(cost_pts / risk, 3), gross_r=round(gross_pts / risk, 3))
    return _public(t)


def _mark_open(t: dict, last_close: float, last_date: str, stop: float, ctx: dict) -> dict:
    sign = 1 if t["direction"] == "long" else -1
    risk = t["_risk_pts"]
    entry_cost = t["entry_price"] * ctx["cost_bps"] / 1e4
    t.update(status="open", exit_date=None, exit_price=None, exit_reason=None, pnl_r=round(((last_close - t["entry_price"]) * sign - entry_cost) / risk, 3))
    t["pnl_rupees"] = round(((last_close - t["entry_price"]) * sign - entry_cost) * ctx["pv"]
                            * max(t["lots"] or 0, 1), 0) if ctx["pv"] else None
    t["extra"].update(last_close=round(last_close, 2), last_date=last_date[:10], stop_now=round(stop, 2))
    return _public(t)


def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_")}


def _open_trade(symbol: str, direction: str, entry_date: str, entry: float, risk_pts: float,
                ctx: dict, extra: dict) -> dict:
    lots = None
    if ctx["pv"]:
        lots = int(ctx["one_r"] // (risk_pts * ctx["pv"])) if risk_pts > 0 else 0
        if lots == 0:
            extra["size_note"] = f"1 lot risks ₹{risk_pts * ctx['pv']:,.0f} > 1R ₹{ctx['one_r']:,.0f} (₹ shown for 1 lot)"
    sign = 1 if direction == "long" else -1
    return {"symbol": symbol, "direction": direction, "entry_date": entry_date[:10], "entry_price": round(entry, 2),
            "stop": round(entry - sign * risk_pts, 2), "lots": lots, "extra": extra, "_risk_pts": risk_pts,
            "_best": entry, "mfe_r": 0.0, "mae_r": 0.0}


def _ctx(symbol: str) -> dict:
    kind = kind_of(symbol)
    pv, contract = point_value(symbol)
    return {"kind": kind, "pv": pv, "contract": contract, "cost_bps": rcfg.cost_bps(kind),
            "roll_bps": rcfg.cost_bps("roll"), "one_r": rcfg.one_r_rupees()}


def _excursion(t: dict, close: float) -> None:
    sign = 1 if t["direction"] == "long" else -1
    move = (close - t["entry_price"]) * sign / t["_risk_pts"]
    t["mfe_r"] = round(max(t["mfe_r"], move), 3)
    t["mae_r"] = round(min(t["mae_r"], move), 3)


# --- American EWMA crossover ------------------------------------------------------------------

def ewma_signal(fast: float, slow: float, atr_v: float, buffer: float) -> int:
    if fast > slow + buffer * atr_v:
        return 1
    if fast < slow - buffer * atr_v:
        return -1
    return 0


def simulate_ewma(bars: list[dict], p: dict, symbol: str, ctx: dict,
                  start: str | None = None, end: str | None = None) -> list[dict]:
    closes = [b["close"] for b in bars]
    dates = [b["ts"][:10] for b in bars]
    fast, slow = ewma(closes, int(p["fast"])), ewma(closes, int(p["slow"]))
    atrs = wilder_atr(bars, int(p["atr_period"]))
    buf, k = float(p["entry_buffer_atr"]), float(p["stop_atr"])
    warm = max(int(p.get("warmup_bars", 300)), int(p["slow"]))
    trades: list[dict] = []
    pos: dict | None = None
    stop = 0.0
    for i in range(warm, len(bars)):
        if atrs[i] is None:
            continue
        sig = ewma_signal(fast[i], slow[i], atrs[i], buf)
        if pos is not None:
            sign = 1 if pos["direction"] == "long" else -1
            _excursion(pos, closes[i])
            breach = closes[i] < stop if sign > 0 else closes[i] > stop
            signal_on = sig == sign
            if breach and (p.get("exit_rule") == "stop_only" or not signal_on):
                if i + 1 < len(bars):
                    trades.append(_close_trade(pos, bars[i + 1]["open"], dates[i + 1], "trailing_stop", ctx))
                    pos = None
                    continue
            new_stop = closes[i] - sign * k * atrs[i]
            stop = max(stop, new_stop) if sign > 0 else min(stop, new_stop)
            continue
        if end and dates[i] > end:
            break           # no new entries after the window; an open trade is followed to its exit
        if sig == 0 or i + 1 >= len(bars):
            continue
        if (sig > 0 and not p.get("long_enabled", True)) or (sig < 0 and not p.get("short_enabled", True)):
            continue
        entry_date = dates[i + 1]
        if (start and entry_date < start) or (end and entry_date > end):
            continue
        direction = "long" if sig > 0 else "short"
        risk_pts = k * atrs[i]
        pos = _open_trade(symbol, direction, entry_date, bars[i + 1]["open"], risk_pts, ctx,
                          {"signal_date": dates[i], "fast": round(fast[i], 2), "slow": round(slow[i], 2),
                           "atr": round(atrs[i], 2), "contract": ctx.get("contract")})
        stop = pos["stop"]
    if pos is not None:
        trades.append(_mark_open(pos, closes[-1], dates[-1], stop, ctx))
    return trades


def ewma_state(bars: list[dict], p: dict) -> dict:
    """Today's signal numbers for the page."""
    if len(bars) < int(p["slow"]):
        return {"signal": None, "reason": f"only {len(bars)} daily bars (need {p['slow']}+)"}
    closes = [b["close"] for b in bars]
    fast, slow = ewma(closes, int(p["fast"])), ewma(closes, int(p["slow"]))
    a = wilder_atr(bars, int(p["atr_period"]))[-1]
    if a is None:
        return {"signal": None, "reason": "ATR not ready"}
    sig = ewma_signal(fast[-1], slow[-1], a, float(p["entry_buffer_atr"]))
    return {"signal": {1: "long", -1: "short", 0: "flat"}[sig], "date": bars[-1]["ts"][:10],
            "close": round(closes[-1], 2), "fast": round(fast[-1], 2), "slow": round(slow[-1], 2),
            "atr": round(a, 2), "gap_atr": round((fast[-1] - slow[-1]) / a, 2)}


# --- time-series momentum ---------------------------------------------------------------------

def tsmom_signals(closes: list[float], blocks: int, block_days: int, vol_span: int) -> list[float | None]:
    rets: list[float | None] = [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    vol = ewma_vol(rets, vol_span)
    z: list[float | None] = [None] * len(closes)
    for i in range(2, len(closes)):
        if rets[i] is not None and vol[i - 1]:
            z[i] = rets[i] / vol[i - 1]
    out: list[float | None] = [None] * len(closes)
    need = blocks * block_days
    for i in range(need + 1, len(closes)):
        signs = []
        for m in range(blocks):
            window = z[i - (m + 1) * block_days + 1:i - m * block_days + 1]
            if any(v is None for v in window):
                break
            s = sum(window)
            signs.append(1 if s > 0 else -1 if s < 0 else 0)
        if len(signs) == blocks:
            out[i] = sum(signs) / blocks
    return out


def simulate_tsmom(bars: list[dict], p: dict, symbol: str, ctx: dict,
                   start: str | None = None, end: str | None = None) -> list[dict]:
    closes = [b["close"] for b in bars]
    dates = [b["ts"][:10] for b in bars]
    sig = tsmom_signals(closes, int(p["blocks"]), int(p["block_days"]), int(p["vol_span"]))
    atrs = wilder_atr(bars, int(p["atr_period"]))
    thr, k, wd = float(p["entry_threshold"]), float(p["stop_atr"]), int(p.get("rebalance_weekday", 4))
    warm = max(int(p.get("warmup_bars", 150)), int(p["blocks"]) * int(p["block_days"]) + 2)
    trades: list[dict] = []
    pos: dict | None = None
    stop = 0.0
    for i in range(warm, len(bars)):
        if atrs[i] is None:
            continue
        if pos is not None:
            sign = 1 if pos["direction"] == "long" else -1
            _excursion(pos, closes[i])
            breach = closes[i] < stop if sign > 0 else closes[i] > stop
            flip = False
            if is_rebalance(dates, i, wd) and sig[i] is not None:
                want = 1 if sig[i] >= thr else -1 if sig[i] <= -thr else 0
                flip = want != sign
            if (breach or flip) and i + 1 < len(bars):
                trades.append(_close_trade(pos, bars[i + 1]["open"], dates[i + 1],
                                           "trailing_stop" if breach else "signal_change", ctx))
                pos = None
                if breach:
                    continue
            else:
                new_stop = closes[i] - sign * k * atrs[i]
                stop = max(stop, new_stop) if sign > 0 else min(stop, new_stop)
                continue
        if end and dates[i] > end:
            break
        if i + 1 >= len(bars) or sig[i] is None or not is_rebalance(dates, i, wd):
            continue
        want = 1 if sig[i] >= thr else -1 if sig[i] <= -thr else 0
        if want == 0 or (want > 0 and not p.get("long_enabled", True)) or (want < 0 and not p.get("short_enabled", True)):
            continue
        entry_date = dates[i + 1]
        if (start and entry_date < start) or (end and entry_date > end):
            continue
        pos = _open_trade(symbol, "long" if want > 0 else "short", entry_date, bars[i + 1]["open"], k * atrs[i], ctx,
                          {"signal_date": dates[i], "signal": round(sig[i], 2), "atr": round(atrs[i], 2),
                           "contract": ctx.get("contract")})
        stop = pos["stop"]
    if pos is not None:
        trades.append(_mark_open(pos, closes[-1], dates[-1], stop, ctx))
    return trades


def tsmom_state(bars: list[dict], p: dict) -> dict:
    closes = [b["close"] for b in bars]
    sig = tsmom_signals(closes, int(p["blocks"]), int(p["block_days"]), int(p["vol_span"])) if closes else []
    if not sig or sig[-1] is None:
        return {"signal": None, "reason": f"only {len(bars)} daily bars"}
    thr = float(p["entry_threshold"])
    want = "long" if sig[-1] >= thr else "short" if sig[-1] <= -thr else "flat"
    return {"signal": want, "value": round(sig[-1], 2), "date": bars[-1]["ts"][:10], "close": round(closes[-1], 2)}


# --- strategy entry points (registry) -----------------------------------------------------------

SIMULATORS = {"trend_ewma": (simulate_ewma, ewma_state), "tsmom": (simulate_tsmom, tsmom_state)}
GRID_KEYS = {"trend_ewma": ("fast", "slow", "entry_buffer_atr", "stop_atr"),
             "tsmom": ("blocks", "block_days", "entry_threshold", "stop_atr")}


def run_trades(key: str, p: dict, start: str | None = None, end: str | None = None,
               bars_for: Callable[[str], list[dict]] = daily_bars) -> tuple[list[dict], dict]:
    sim, state_fn = SIMULATORS[key]
    trades, states = [], {}
    for symbol in p["instruments"]:
        bars = bars_for(symbol)
        states[symbol] = state_fn(bars, p) if bars else {"signal": None, "reason": "no daily bars stored"}
        if len(bars) < int(p.get("warmup_bars", 300)) + 5:
            states[symbol]["note"] = f"{len(bars)} daily bars stored — download research history"
            continue
        trades += sim(bars, p, symbol, _ctx(symbol), start, end)
    return trades, states


def live(key: str, today: date | None = None) -> dict:
    from backend.research import journal
    p = rcfg.strategy(key)
    trades, states = run_trades(key, p)
    synced = journal.sync(key, trades, rcfg.live_start())
    return {"status": "ok", **synced, "states": states}


def backtest(key: str, params: dict, progress: Callable[[str], None] | None = None) -> dict:
    """Base settings from research.yaml, overridden by ``params['overrides']``; with
    ``params['grid']`` (key → list of values) every combination is run (sensitivity, not
    selection) and the base combination's trades are returned."""
    base = {**rcfg.strategy(key), **(params.get("overrides") or {})}
    start, end = params.get("start_date"), params.get("end_date")
    trades, _ = run_trades(key, base, start, end)
    grid_rows = []
    grid = {k: v for k, v in (params.get("grid") or {}).items() if k in GRID_KEYS[key] and v}
    combos = list(product(*grid.values())) if grid else []
    from backend.research.stats import stats
    for n, combo in enumerate(combos, 1):
        p = {**base, **dict(zip(grid.keys(), combo))}
        if key == "trend_ewma" and int(p["fast"]) >= int(p["slow"]):
            continue
        rows, _ = run_trades(key, p, start, end)
        grid_rows.append({"params": dict(zip(grid.keys(), combo)), **stats(rows)})
        if progress:
            progress(f"grid {n}/{len(combos)}")
    return {"trades": trades, "grid": grid_rows, "settings": base}


def status(key: str) -> dict:
    """Signal state per instrument now (nothing stored)."""
    p = rcfg.strategy(key)
    states = {}
    _, state_fn = SIMULATORS[key]
    for symbol in p["instruments"]:
        bars = daily_bars(symbol)
        states[symbol] = state_fn(bars, p) if bars else {"signal": None, "reason": "no daily bars stored"}
        states[symbol]["bars"] = len(bars)
    return {"settings": p, "states": states}
