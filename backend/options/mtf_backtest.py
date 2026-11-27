"""Backtest of the intraday option-spread entry (asked 27 Nov 2026).

The live spread engine enters on the multi-timeframe trigger in ``spread_mtf.evaluate`` (daily
regime → 4H → 1H pullback/breakout → 15m trigger). The plain spread ``backtest()`` replays the
old evening rules, so it can't test the trigger's own settings. This replays the trigger bar by
bar on stored index bars and compares variants of the 1H setup — ``pullback_touch_atr`` (how
close a bar must come to the 1H EMA20) and ``pullback_bars`` (how many 1H bars back the touch may
be) — on identical everything else.

How a variant is replayed, per index:
* the daily regime is the one known before the session (daily bars before that day);
* every completed 15-minute bar in the entry window is evaluated with only the bars completed by
  its end (1H, 4H resampled from 1H, 15m; 15m resampled from stored 5-minute bars when no 15m
  history is stored); one spread per index at a time, as live;
* a signal is priced on that day's **end-of-day** option chain (no intraday chains are stored) with
  the live strike, credit, VIX and sizing rules (``spreads.candidate``), then followed on daily
  closes: take profit / stop on the day's chain, the invalidation level on that day's 1H closes
  (exit at that day's close value), and the time exit;
* costs from the research cost model.

SENSEX follows NIFTY's bars (as live). Limitations are listed with every result.
"""

from __future__ import annotations

import bisect
import logging
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Callable

log = logging.getLogger(__name__)

VARIANTS_DEFAULT = [
    {"name": "live (0.25 ATR, 6 bars)", "pullback_touch_atr": 0.25, "pullback_bars": 6},
    {"name": "0.75 ATR, 6 bars", "pullback_touch_atr": 0.75, "pullback_bars": 6},
    {"name": "0.25 ATR, 10 bars", "pullback_touch_atr": 0.25, "pullback_bars": 10},
    {"name": "0.75 ATR, 10 bars", "pullback_touch_atr": 0.75, "pullback_bars": 10},
]

_state = {"status": "idle", "progress": None, "result": None, "error": None}
_lock = threading.Lock()


def resample_15(rows: list[dict]) -> list[dict]:
    """5-minute (or 10-minute) bars → session-anchored 15-minute bars (09:15, 09:30, …)."""
    out: list[dict] = []
    cur, key = None, None
    for b in rows:
        t = datetime.fromisoformat(b["ts"])
        minutes = (t.hour * 60 + t.minute) - (9 * 60 + 15)
        if minutes < 0:
            continue
        start = datetime.combine(t.date(), time(9, 15)) + timedelta(minutes=(minutes // 15) * 15)
        if start != key:
            if cur:
                out.append(cur)
            key = start
            cur = {"ts": start.isoformat(timespec="seconds"), "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"], "volume": b.get("volume") or 0}
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b.get("volume") or 0
    if cur:
        out.append(cur)
    return out


def _bars(name: str) -> dict:
    from backend.marketdata.candles import get_candles, resample_4h
    from backend.marketdata.instruments import spot_instrument
    from tradingagents.utils.market_calendar import NSE

    inst = spot_instrument(name)
    if not inst:
        return {"h1": [], "h4": [], "m15": [], "daily": [], "m15_source": None}
    tok = inst["instrument_token"]
    h1 = get_candles(tok, "60minute")
    m15 = get_candles(tok, "15minute")
    source = "15minute"
    five = get_candles(tok, "5minute")
    if five and (not m15 or five[0]["ts"] < m15[0]["ts"]):
        m15, source = resample_15(five), "5minute resampled"
    return {"h1": h1, "h4": resample_4h(h1, NSE), "m15": m15, "daily": get_candles(tok, "day"),
            "m15_source": source if m15 else None}


class _View:
    """Bars completed by a moment, sliced from a stored series without copying all of it."""

    def __init__(self, bars: list[dict], minutes: int):
        from backend.options.spread_mtf import bar_end
        self.bars = bars
        self.ends = [bar_end(b, minutes) for b in bars]

    def upto(self, now: datetime, keep: int) -> list[dict]:
        k = bisect.bisect_right(self.ends, now)
        return self.bars[max(0, k - keep):k]


def _regime_before(daily: list[dict], day: str) -> str:
    from backend.signals.regime import current_regime
    i = bisect.bisect_left([b["ts"][:10] for b in daily], day)
    return current_regime(daily[max(0, i - 300):i])["regime"] if i else "UNSTABLE"


def run(start: date, end: date, variants: list[dict] | None = None, split_date: date | None = None,
        symbols: list[str] | None = None, progress: Callable[[str], None] | None = None) -> dict:
    from backend.options import spreads as sp_mod
    from backend.options import store
    from backend.options.spread_mtf import evaluate, invalidated
    from backend.options.spread_research import cost_points, settings as research_settings
    from tradingagents.utils.market_calendar import is_trading_day

    cfg = sp_mod.settings()
    base_mtf = dict(cfg.get("intraday") or {})
    variants = variants or VARIANTS_DEFAULT
    symbols = symbols or list(cfg["symbols"])
    costs = research_settings()["costs"]
    trend_of = {s: cfg["regime_source"].get(s, s) for s in symbols}
    data = {src: _bars(src) for src in sorted(set(trend_of.values()))}
    for src, d in data.items():   # BANKNIFTY etc.: daily regime from its own daily bars
        if not d["daily"]:
            from backend.marketdata.candles import get_candles
            from backend.marketdata.instruments import spot_instrument
            inst = spot_instrument(src)
            d["daily"] = get_candles(inst["instrument_token"], "day") if inst else []
    coverage = {src: {"h1_from": d["h1"][0]["ts"][:10] if d["h1"] else None,
                      "m15_from": d["m15"][0]["ts"][:10] if d["m15"] else None,
                      "m15_source": d["m15_source"]} for src, d in data.items()}
    views = {src: (_View(d["h1"], 60), _View(d["h4"], 240), _View(d["m15"], 15)) for src, d in data.items()}
    h1_by_day = {src: defaultdict(list) for src in data}
    for src, d in data.items():
        for b in d["h1"]:
            h1_by_day[src][b["ts"][:10]].append(b)

    results = []
    for vi, var in enumerate(variants):
        mcfg = {**base_mtf, "pullback_touch_atr": float(var["pullback_touch_atr"]),
                "pullback_bars": int(var["pullback_bars"])}
        trades, signals, skipped = [], 0, defaultdict(int)
        for symbol in symbols:
            src = trend_of[symbol]
            vh1, vh4, vm15 = views[src]
            m15 = data[src]["m15"]
            if not m15:
                skipped[f"{symbol}: no 15-minute history"] += 1
                continue
            book: dict | None = None
            d = max(start, date.fromisoformat(m15[0]["ts"][:10]))
            while d <= end:
                iso = d.isoformat()
                if not is_trading_day(d):
                    d += timedelta(days=1)
                    continue
                if progress:
                    progress(f"{var['name']} · {symbol} · {iso} ({vi + 1}/{len(variants)})")
                if book:            # follow the open spread on this day's close and 1H closes
                    chain = store.chain(symbol, iso, book["expiry"])
                    val = sp_mod.value(book["legs"], sp_mod._prices(chain), sp_mod.is_debit(book["strategy"]))
                    reason = sp_mod.exit_check(book, val, iso, book["exit_by"], cfg)
                    if not reason and val is not None and any(
                            invalidated(book["invalid"], book["direction"], b["close"]) for b in h1_by_day[src][iso]):
                        reason = "invalidated"
                    if reason and val is not None:
                        cost = cost_points(book, sp_mod._prices(chain), costs)
                        res = sp_mod.pnl(book, val, cfg)
                        one_r = sp_mod.one_r_points(book, cfg)
                        net = res["pnl_points"] - cost
                        trades.append({**book, "exit_date": iso, "exit_value": round(val, 2), "exit_reason": reason,
                                       "pnl_points": round(net, 2), "pnl_r": round(net / one_r, 3) if one_r else 0.0,
                                       "pnl_inr": round(net * book["lot_size"] * book["lots"], 0)})
                        book = None
                if book:
                    d += timedelta(days=1)
                    continue
                regime = _regime_before(data[src]["daily"], iso)
                lo = bisect.bisect_left(vm15.ends, datetime.combine(d, time(0, 0)))
                hi = bisect.bisect_right(vm15.ends, datetime.combine(d, time(23, 59)))
                fired = None
                for k in range(lo, hi):
                    now = vm15.ends[k] + timedelta(minutes=2)
                    out = evaluate(regime, vh1.upto(now, 400), vh4.upto(now, 150), vm15.upto(now, 120), now, mcfg)
                    if out.get("signal"):
                        fired = out["signal"]
                        break
                if not fired:
                    d += timedelta(days=1)
                    continue
                signals += 1
                exps = store.expiries(symbol, iso)
                if not exps:
                    skipped["no option file that day"] += 1
                    d += timedelta(days=1)
                    continue
                c = sp_mod.candidate(symbol, d, cfg, lambda day, e: store.chain(symbol, day, e), lambda day: exps,
                                     regime, store.vix_on(iso))
                if not c["ok"]:
                    skipped[sp_mod._skip_key(c["reason"], c.get("strategy"))] += 1
                    d += timedelta(days=1)
                    continue
                book = {k: c[k] for k in ("symbol", "signal_date", "expiry", "strategy", "legs", "credit", "width",
                                          "lot_size", "lots", "regime", "vix", "exit_by")}
                book.update(direction=fired["direction"], invalid=fired.get("invalid"), signal_ts=fired["signal_ts"])
                d += timedelta(days=1)
            if book:
                skipped["still open at the end"] += 1
        results.append({"variant": var, "signals": signals, "trades": trades, "skipped": dict(skipped)})

    def stats(rows: list[dict]) -> dict:
        s = sp_mod._stats(rows)
        if not rows:
            return s
        gains = sum(x["pnl_r"] for x in rows if x["pnl_r"] > 0)
        losses = -sum(x["pnl_r"] for x in rows if x["pnl_r"] < 0)
        return {**s, "profit_factor": round(gains / losses, 2) if losses else None,
                "total_inr": round(sum(x["pnl_inr"] for x in rows), 0),
                "avg_inr": round(sum(x["pnl_inr"] for x in rows) / len(rows), 0)}

    out_rows = []
    for r in results:
        t = r["trades"]
        row = {"name": r["variant"]["name"], "pullback_touch_atr": r["variant"]["pullback_touch_atr"],
               "pullback_bars": r["variant"]["pullback_bars"], "signal_days": r["signals"], "all": stats(t),
               "by_symbol": {s: stats([x for x in t if x["symbol"] == s]) for s in symbols},
               "by_exit": {k: stats([x for x in t if x["exit_reason"] == k])
                           for k in sorted({x["exit_reason"] for x in t})},
               "skipped": dict(sorted(r["skipped"].items(), key=lambda kv: -kv[1])),
               "trades": [{k: x.get(k) for k in ("symbol", "signal_ts", "strategy", "credit", "width", "exit_date",
                                                 "exit_reason", "pnl_r", "pnl_inr")} for x in t][-60:]}
        if split_date:
            cut = split_date.isoformat()
            row["by_period"] = {"train": stats([x for x in t if x["signal_date"] < cut]),
                                "test": stats([x for x in t if x["signal_date"] >= cut])}
        out_rows.append(row)
    return {"status": "ok", "start": start.isoformat(), "end": end.isoformat(),
            "split_date": split_date.isoformat() if split_date else None, "coverage": coverage,
            "variants": out_rows,
            "limitations": [
                "Entries are priced on the signal day's closing option prices (no intraday option history is stored).",
                "Take profit and stop are checked on daily closes; the invalidation on that day's 1H closes, exiting "
                "at the close.",
                "Only days with stored 1H and 15-minute (or 5-minute) index bars can signal — see coverage.",
                "RBI/Budget before expiry blocks the spread here (live opens it at half size).",
                "SENSEX uses NIFTY's bars and regime; BANKNIFTY needs its own 15-minute/5-minute history.",
                "Costs: the research cost model."]}


# --- background job --------------------------------------------------------------------------------

def status() -> dict:
    with _lock:
        return dict(_state)


def start_job(start: date, end: date, variants: list[dict] | None, split_date: date | None) -> dict:
    with _lock:
        if _state["status"] == "running":
            return {"status": "busy"}
        _state.update(status="running", progress="starting", result=None, error=None)

    def progress(msg: str) -> None:
        with _lock:
            _state["progress"] = msg

    def body() -> None:
        try:
            res = run(start, end, variants, split_date, progress=progress)
            with _lock:
                _state.update(status="done", result=res, progress=None)
        except Exception as exc:  # report, don't crash the server
            log.exception("spread trigger backtest failed")
            with _lock:
                _state.update(status="error", error=f"{type(exc).__name__}: {exc}", progress=None)

    threading.Thread(target=body, name="spread-mtf-backtest", daemon=True).start()
    return {"status": "started"}
