"""Index option spreads (NIFTY, BANKNIFTY, SENSEX).

**Live mode ``intraday`` (agreed 24 Nov 2026, replaces the evening run):** the daily regime gives
the direction, then 4H confirmation, a 1H setup and a 15-minute trigger time the entry
(``spread_mtf.evaluate()``; SIDEWAYS → iron condor on a 1H range rejection). The scan runs after
each completed 15-minute bar (scheduler ``scan:spreads``); strikes come from the live Kite option
chain (bid/ask mid, IV and delta from Black-76, legs must pass a bid-ask check); credit vs debit
from the ATM IV percentile (``debit_below_iv_pct`` against ``iv_lookback`` stored days; India VIX
``debit_below_vix`` only while there is too little IV history). Open spreads are checked on each
1H close (``follow_intraday()``): take profit / stop on the live spread value, or the trend index
closing beyond the setup's invalidation level, or the 1H close at/after 15:15 on the exit day.
The evening run only follows spreads no 1H check reached that day (e.g. no Kite login).

The rules below are the original end-of-day version, still used by ``backtest()`` and
``spread_research`` (mode ``eod``):

Config ``instruments.option_spreads``. Agreed spec (22 Nov 2026):

* strategy by the index's daily regime: STRONG_UP → bull put spread, STRONG_DOWN →
  bear call spread, SIDEWAYS → iron condor; nothing in HIGH_VOL / UNSTABLE
  (``regime_strategy``). SENSEX uses the NIFTY regime (no stored SENSEX candles).
* short leg = the option whose |delta| is inside ``short_delta`` (0.15–0.20),
  closest to the middle, with open interest ≥ ``min_oi``; long leg ``wing`` points
  further out (per symbol).
* entry window per symbol: weekdays and calendar days to expiry (NIFTY Wed–Fri
  before the Tuesday expiry, SENSEX Fri–Mon before Thursday, BANKNIFTY monthly
  10–20 days out); credit ≥ ``min_credit_ratio`` × width; India VIX ≤ ``max_vix``;
  no RBI/Budget day before expiry; one open spread per index, ``max_open`` in all.
* exits on daily closes: take profit when the spread can be bought back for
  ``take_profit_pct`` % of the credit or less; stop when it costs
  ``stop_multiple`` × the credit; otherwise close on the last trading day before
  expiry.
* 1R = the loss at the stop (credit × (stop_multiple − 1)); lots so 1R ≤
  ``risk_per_trade_pct`` of capital and the worst case (width − credit) ≤
  ``max_loss_pct``; 0 lots → not traded.

Live: ``run_eod()`` after the evening option files are stored — follows open
spreads, then builds today's candidates, sends shortlisted ones to the AI
(sharing the daily AI budget) and alerts Telegram on approval. Paper only:
prices are the day's closing prices; the real fill is next morning.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import get_config

CREDIT = ("bull_put", "bear_call", "iron_condor")
# debit spreads (asked 24 Nov 2026): bought, so they gain when the index moves the right way;
# backtest only until the live ``regime_strategy`` names them
DEBIT = ("bull_call", "bear_put")
STRATEGIES = CREDIT + DEBIT
# which strategies fit which regime when a backtest compares several (each has its own book)
REGIME_CHOICES = {"STRONG_UP": ("bull_put", "bull_call"), "STRONG_DOWN": ("bear_call", "bear_put"),
                  "MODERATE_UP": ("bull_put", "bull_call"), "MODERATE_DOWN": ("bear_call", "bear_put"),
                  "SIDEWAYS": ("iron_condor",), "UNSTABLE": ("iron_condor",)}


# credit spread → the debit spread with the same view, used when India VIX is low
DEBIT_FOR = {"bull_put": "bull_call", "bear_call": "bear_put"}
NAMES = {"bull_put": "Bull put spread", "bear_call": "Bear call spread", "iron_condor": "Iron condor",
         "bull_call": "Bull call spread", "bear_put": "Bear put spread"}


def base_strategy(regime: str | None, cfg: dict) -> str | None:
    """The credit-side strategy for a regime; a moderate trend uses its strong trend's choice."""
    from backend.signals.regime import direction
    rs = cfg["regime_strategy"]
    if regime in rs:
        return rs[regime]
    d = direction(regime)
    return rs.get("STRONG_UP" if d == "up" else "STRONG_DOWN") if d else None


def by_iv(strategy: str, iv_pct: float | None, vix: float | None, cfg: dict) -> str:
    """Intraday (24 Nov 2026): debit spread when the ATM IV percentile is below
    ``debit_below_iv_pct``, credit at or above it; without enough IV history the VIX switch."""
    limit = cfg.get("debit_below_iv_pct")
    if strategy in DEBIT_FOR and limit is not None and iv_pct is not None:
        return DEBIT_FOR[strategy] if iv_pct < limit else strategy
    return by_vix(strategy, vix, cfg)


def is_debit(strategy: str) -> bool:
    return strategy in DEBIT


def by_vix(strategy: str, vix: float | None, cfg: dict) -> str:
    """Asked 24 Nov 2026: in a trend, sell premium (bull put / bear call) when VIX is normal or high,
    buy it (bull call / bear put) when VIX is below ``debit_below_vix``. Unknown VIX → credit."""
    limit = cfg.get("debit_below_vix")
    if strategy in DEBIT_FOR and limit is not None and vix is not None and vix < limit:
        return DEBIT_FOR[strategy]
    return strategy


def settings() -> dict:
    cfg = get_config()
    base = {
        "enabled": False, "short_delta": [0.15, 0.20], "min_oi": 0, "min_credit_ratio": 0.15,
        "take_profit_pct": 50, "stop_multiple": 2.0, "max_vix": 25.0, "max_open": 3,
        "regime_strategy": {"STRONG_UP": "bull_put", "STRONG_DOWN": "bear_call", "SIDEWAYS": "iron_condor"},
        "symbols": {
            "NIFTY": {"wing": 200, "lot_size": 65, "weekdays": [2, 3, 4], "min_days": 4, "max_days": 6},
            "BANKNIFTY": {"wing": 500, "lot_size": 30, "weekdays": [0, 1, 2, 3, 4], "min_days": 10, "max_days": 20},
            "SENSEX": {"wing": 500, "lot_size": 20, "weekdays": [4, 0], "min_days": 3, "max_days": 6},
        },
        "regime_source": {"SENSEX": "NIFTY"}, "max_loss_pct": 3.0,
        # one-sided credit spreads (bull put / bear call) collect less than a condor; None = min_credit_ratio
        "min_credit_ratio_single": None,
        "debit_below_vix": 11.0,       # trend spreads: debit below this India VIX, credit at or above it
        # debit spreads: long leg near the money, short leg ``wing`` further out
        "debit": {"long_delta": [0.40, 0.60], "max_debit_ratio": 0.5, "take_profit_pct": 100, "stop_pct": 50},
        "mode": "eod",                 # intraday | eod (live entries; backtests always use the eod rules)
        "intraday": {},                # spread_mtf.DEFAULTS overrides
        "debit_below_iv_pct": None,    # intraday credit/debit switch on the ATM IV percentile
        "iv_lookback": 252,            # stored days the percentile compares with
        "iv_min_history": 60,          # fewer → fall back to debit_below_vix
        # RBI/Budget before expiry (25 Nov 2026): None = no spread; a number = open it at that share of the
        # normal risk (0.5 = half size) with the event named in the AI prompt and the alert. Live intraday only.
        "event_size_mult": None,
        "risk_per_trade_pct": cfg.global_.risk.risk_per_trade_pct, "capital": cfg.global_.capital,
    }
    try:
        inst = cfg.instrument("option_spreads")
    except KeyError:
        return base
    extra = dict(inst.model_extra or {})
    if "symbols" in extra:
        extra["symbols"] = {k: {**base["symbols"].get(k, {}), **v} for k, v in extra["symbols"].items()}
    if "debit" in extra:
        extra["debit"] = {**base["debit"], **extra["debit"]}
    from backend.options.spread_mtf import DEFAULTS as MTF_DEFAULTS
    extra["intraday"] = {**MTF_DEFAULTS, **(extra.get("intraday") or {})}
    return {**base, **extra, "enabled": inst.enabled}


# --- pure construction -------------------------------------------------------------------

def pick_expiry(day: date, expiries: list[str], rule: dict) -> str | None:
    if day.weekday() not in rule["weekdays"]:
        return None
    ok = [e for e in sorted(expiries) if rule["min_days"] <= (date.fromisoformat(e) - day).days <= rule["max_days"]]
    return ok[0] if ok else None


def _price(row: dict) -> float | None:
    p = row.get("close") or row.get("settle")
    return float(p) if p and p > 0 else None


def pick_short(chain: list[dict], opt_type: str, band: list[float], min_oi: float) -> dict | None:
    lo, hi = band
    mid = (lo + hi) / 2
    rows = [r for r in chain if r["opt_type"] == opt_type and r.get("delta") is not None and _price(r)
            and lo <= abs(r["delta"]) <= hi and (r.get("oi") or 0) >= min_oi]
    return min(rows, key=lambda r: abs(abs(r["delta"]) - mid)) if rows else None


def build_debit(chain: list[dict], strategy: str, rule: dict, cfg: dict) -> tuple[dict | None, str]:
    """Bull call / bear put: buy the option with |delta| in ``debit.long_delta`` (closest to the
    middle), sell the one ``wing`` points further out. ``credit`` holds the debit paid."""
    d = cfg["debit"]
    t = "CE" if strategy == "bull_call" else "PE"
    by_key = {(r["strike"], r["opt_type"]): r for r in chain}
    near = pick_short(chain, t, d["long_delta"], cfg["min_oi"])
    if not near:
        return None, f"no {t} with delta {d['long_delta'][0]}–{d['long_delta'][1]}"
    far_strike = near["strike"] + rule["wing"] if t == "CE" else near["strike"] - rule["wing"]
    far = by_key.get((far_strike, t))
    if not far or not _price(far):
        return None, f"no priced {t} {far_strike:g} for the wing"
    debit = _price(near) - _price(far)
    width = float(rule["wing"])
    if debit <= 0:
        return None, f"{t} spread has no debit"
    if debit > d["max_debit_ratio"] * width:
        return None, f"debit {debit:.1f} > {d['max_debit_ratio']:g} × width {width:g}"
    legs = [{"type": t, "strike": near["strike"], "side": "long", "price": _price(near),
             "delta": round(near["delta"], 3), "iv": near.get("iv")},
            {"type": t, "strike": far_strike, "side": "short", "price": _price(far)}]
    return {"strategy": strategy, "legs": legs, "credit": round(debit, 2), "width": width,
            "max_loss_points": round(debit, 2)}, ""


def build(chain: list[dict], strategy: str, rule: dict, cfg: dict) -> tuple[dict | None, str]:
    """Legs, credit and width for one strategy from one day's chain of one expiry."""
    if is_debit(strategy):
        return build_debit(chain, strategy, rule, cfg)
    by_key = {(r["strike"], r["opt_type"]): r for r in chain}
    sides = {"bull_put": ["PE"], "bear_call": ["CE"], "iron_condor": ["PE", "CE"]}[strategy]
    legs, credit = [], 0.0
    for t in sides:
        short = pick_short(chain, t, cfg["short_delta"], cfg["min_oi"])
        if not short:
            return None, f"no {t} with delta {cfg['short_delta'][0]}–{cfg['short_delta'][1]}"
        far = short["strike"] - rule["wing"] if t == "PE" else short["strike"] + rule["wing"]
        long = by_key.get((far, t))
        if not long or not _price(long):
            return None, f"no priced {t} {far:g} for the wing"
        c = _price(short) - _price(long)
        if c <= 0:
            return None, f"{t} side has no credit"
        credit += c
        legs += [{"type": t, "strike": short["strike"], "side": "short", "price": _price(short),
                  "delta": round(short["delta"], 3), "iv": short.get("iv")},
                 {"type": t, "strike": far, "side": "long", "price": _price(long)}]
    width = float(rule["wing"])
    ratio = cfg["min_credit_ratio"]
    if strategy != "iron_condor" and cfg.get("min_credit_ratio_single") is not None:
        ratio = cfg["min_credit_ratio_single"]
    if credit < ratio * width:
        return None, f"credit {credit:.1f} < {ratio:g} × width {width:g}"
    # an iron condor can only lose on one side at expiry: worst case is one width
    return {"strategy": strategy, "legs": legs, "credit": round(credit, 2), "width": width,
            "max_loss_points": round(width - credit, 2)}, ""


def value(legs: list[dict], prices: dict, debit: bool = False) -> float | None:
    """What the spread is worth now, at ``prices[(strike, type)]``: for a credit spread the cost to
    buy it back (short legs minus long legs), for a debit spread what selling it brings (long minus
    short). Never below zero."""
    total = 0.0
    for leg in legs:
        p = prices.get((leg["strike"], leg["type"]))
        if p is None:
            return None
        total += p if leg["side"] == "short" else -p
    return max(0.0, -total if debit else total)


def one_r_points(sp: dict, cfg: dict) -> float:
    """The loss at the stop, in points: credit × (stop_multiple − 1), or for a debit spread
    ``debit.stop_pct`` % of the debit."""
    if is_debit(sp.get("strategy", "")):
        return sp["credit"] * cfg["debit"]["stop_pct"] / 100
    return sp["credit"] * (cfg["stop_multiple"] - 1)


def size(credit: float, width: float, lot_size: int, cfg: dict, strategy: str = "iron_condor") -> tuple[int, str]:
    stop_loss = one_r_points({"credit": credit, "strategy": strategy}, cfg) * lot_size
    worst = (credit if is_debit(strategy) else width - credit) * lot_size
    budget = cfg["capital"] * cfg["risk_per_trade_pct"] / 100
    cap = cfg["capital"] * cfg["max_loss_pct"] / 100
    lots = min(int(budget // stop_loss) if stop_loss > 0 else 0, int(cap // worst) if worst > 0 else 0)
    if lots < 1:
        return 0, f"1 lot: stop loss ₹{stop_loss:,.0f} / worst case ₹{worst:,.0f} above the limits"
    return lots, ""


def exit_check(sp: dict, val: float | None, day: str, last_day: str, cfg: dict) -> str | None:
    if val is None:
        return None
    if is_debit(sp.get("strategy", "")):
        d = cfg["debit"]
        target = min(sp["credit"] * (1 + d["take_profit_pct"] / 100), 0.9 * sp["width"])
        if val >= target:
            return "take_profit"
        if val <= sp["credit"] * (1 - d["stop_pct"] / 100):
            return "stop"
        return "expiry_exit" if day >= last_day else None
    if val <= sp["credit"] * (1 - cfg["take_profit_pct"] / 100):
        return "take_profit"
    if val >= sp["credit"] * cfg["stop_multiple"]:
        return "stop"
    if day >= last_day:
        return "expiry_exit"
    return None


def pnl(sp: dict, exit_value: float, cfg: dict) -> dict:
    points = (exit_value - sp["credit"]) if is_debit(sp.get("strategy", "")) else sp["credit"] - exit_value
    one_r = one_r_points(sp, cfg)
    return {"pnl_points": round(points, 2), "pnl_r": round(points / one_r, 3) if one_r else 0.0,
            "pnl_inr": round(points * sp["lot_size"] * sp["lots"], 2)}


def last_trading_day_before(expiry: str) -> str:
    from tradingagents.utils.market_calendar import is_trading_day
    d = date.fromisoformat(expiry) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


# --- context: regime, events, VIX -----------------------------------------------------------

def _regime_on(symbol: str, day: str, cfg: dict, cache: dict) -> str:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.signals.regime import current_regime

    src = cfg["regime_source"].get(symbol, symbol)
    if src not in cache:
        inst = spot_instrument(src)
        cache[src] = get_candles(inst["instrument_token"], "day", limit=5000) if inst else []
    bars = [b for b in cache[src] if b["ts"][:10] <= day]
    return current_regime(bars[-300:])["regime"] if bars else "UNSTABLE"


def _blocked(day: date, expiry: str) -> str | None:
    from backend.signals import events
    m = events.market_events_between(day + timedelta(days=1), date.fromisoformat(expiry), kinds=("RBI", "BUDGET"))
    if not m["verified"]:
        return "event calendar not verified to expiry"
    if m["events"]:
        return "event before expiry: " + ", ".join(f"{e['event']} {e['date']}" for e in m["events"])
    return None


def candidate(symbol: str, day: date, cfg: dict, chain_for: Callable[[str, str], list[dict]],
              expiries_for: Callable[[str], list[str]], regime: str, vix: float | None,
              strategy: str | None = None) -> dict:
    """One index on one day: a buildable spread, or why not. ``strategy`` (backtests comparing
    strategies) replaces the regime's own choice; the regime must still allow it."""
    rule = cfg["symbols"][symbol]
    base = {"symbol": symbol, "signal_date": day.isoformat(), "regime": regime, "vix": vix}
    expiry = pick_expiry(day, expiries_for(day.isoformat()), rule)
    if not expiry:
        return {**base, "ok": False, "reason": "outside the entry window"}
    strategy = strategy or by_vix(base_strategy(regime, cfg) or "", vix, cfg) or None
    if not strategy:
        return {**base, "ok": False, "reason": f"no spread in a {regime} regime", "expiry": expiry}
    if vix is not None and vix > cfg["max_vix"]:
        return {**base, "ok": False, "reason": f"India VIX {vix:.1f} > {cfg['max_vix']:g}", "expiry": expiry}
    blocked = _blocked(day, expiry)
    if blocked:
        return {**base, "ok": False, "reason": blocked, "expiry": expiry}
    return _finish(base, chain_for(day.isoformat(), expiry), strategy, rule, cfg, expiry)


def _finish(base: dict, chain: list[dict], strategy: str, rule: dict, cfg: dict, expiry: str) -> dict:
    """Build the legs from one expiry's chain and size the position."""
    built, why = build(chain, strategy, rule, cfg)
    if not built:
        return {**base, "ok": False, "reason": why, "expiry": expiry, "strategy": strategy}
    lots, why = size(built["credit"], built["width"], rule["lot_size"], cfg, strategy)
    if not lots:
        return {**base, "ok": False, "reason": why, "expiry": expiry, **built}
    return {**base, "ok": True, "reason": "", "expiry": expiry, "lot_size": rule["lot_size"], "lots": lots,
            "exit_by": last_trading_day_before(expiry), **built}


def _prices(chain: list[dict]) -> dict:
    return {(r["strike"], r["opt_type"]): _price(r) for r in chain if _price(r) is not None}


# --- backtest -----------------------------------------------------------------------------------

def _stats(rows: list[dict]) -> dict:
    rs = [r["pnl_r"] for r in rows]
    if not rs:
        return {"trades": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    mean = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs), 1), "avg_r": round(mean, 3),
            "total_r": round(sum(rs), 2), "max_drawdown_r": round(dd, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "sharpe_trade": round(mean / sd, 3) if sd else None,
            "total_inr_per_lot": round(sum(r["pnl_points"] * r["lot_size"] for r in rows), 0)}


def _skip_key(reason: str, strategy: str | None) -> str:
    key = reason.split(" (")[0].split(":")[0]
    key = "".join(ch for ch in key if not ch.isdigit() and ch != ".").replace("  ", " ").strip()
    return f"{strategy.replace('_', ' ')}: {key}" if strategy else key


def backtest(start: date, end: date, symbols: list[str] | None = None, split_date: date | None = None,
             cfg: dict | None = None, strategies: list[str] | None = None,
             condor_in_unstable: bool = False, min_credit_ratio_single: float | None = None,
             debit_below_vix: float | None = None) -> dict:
    """Replay the rules over stored EOD chains.

    Default: the live rules (``regime_strategy``), one spread per index at a time. With
    ``strategies`` (asked 24 Nov 2026) every listed strategy the day's regime allows
    (``REGIME_CHOICES``) is tried and each keeps its own book — one open spread per index and
    strategy — so credit and debit spreads can be compared side by side. ``condor_in_unstable``
    also trades iron condors in an UNSTABLE regime; ``min_credit_ratio_single`` sets the minimum
    credit for bull put / bear call spreads."""
    from backend.options import store
    from tradingagents.utils.market_calendar import is_trading_day

    cfg = dict(cfg or settings())
    if min_credit_ratio_single is not None:
        cfg["min_credit_ratio_single"] = min_credit_ratio_single
    if debit_below_vix is not None:                      # 0 = never switch to debit spreads
        cfg["debit_below_vix"] = debit_below_vix or None
    symbols = symbols or list(cfg["symbols"])
    compare = bool(strategies)
    chosen = [s for s in (strategies or []) if s in STRATEGIES]

    def allowed(regime: str) -> list[str]:
        if not compare:
            one = base_strategy(regime, cfg)
            if one is None and condor_in_unstable and regime == "UNSTABLE":
                one = "iron_condor"
            return [one] if one else []           # the VIX switch is applied per day below
        if regime == "UNSTABLE" and not condor_in_unstable:
            return []
        return [s for s in REGIME_CHOICES.get(regime, ()) if s in chosen]

    cache: dict = {}
    trades, skipped = [], {}
    funnel = {s: {"trading_days": 0, "no_option_file": 0, "entry_days": 0, "opened": 0, "book_busy": 0}
              for s in symbols}
    for symbol in symbols:
        books: dict[str, dict | None] = {}
        f = funnel[symbol]
        d = start
        while d <= end:
            iso = d.isoformat()
            if not is_trading_day(d):
                d += timedelta(days=1)
                continue
            f["trading_days"] += 1
            exps = store.expiries(symbol, iso)
            if not exps:
                f["no_option_file"] += 1
                d += timedelta(days=1)
                continue
            for strat, sp in list(books.items()):
                if not sp:
                    continue
                val = value(sp["legs"], _prices(store.chain(symbol, iso, sp["expiry"])), is_debit(strat))
                reason = exit_check(sp, val, iso, sp["exit_by"], cfg)
                if reason:
                    trades.append({**sp, "exit_date": iso, "exit_value": round(val, 2), "exit_reason": reason,
                                   **pnl(sp, val, cfg)})
                    books[strat] = None
            if not pick_expiry(d, exps, cfg["symbols"][symbol]):
                d += timedelta(days=1)
                continue
            f["entry_days"] += 1
            regime = _regime_on(symbol, iso, cfg, cache)
            options = allowed(regime)
            if not options:
                key = _skip_key(f"no spread in a {regime} regime", None)
                skipped[key] = skipped.get(key, 0) + 1
            vix = store.vix_on(iso)
            if not compare:
                options = [by_vix(o, vix, cfg) for o in options]
            for strat in options:
                if books.get(strat):
                    f["book_busy"] += 1
                    continue
                c = candidate(symbol, d, cfg, lambda day, e: store.chain(symbol, day, e), lambda day: exps,
                              regime, vix, strategy=strat)
                if c["ok"]:
                    books[strat] = {k: c[k] for k in ("symbol", "signal_date", "expiry", "strategy", "legs", "credit",
                                                       "width", "lot_size", "lots", "regime", "vix", "exit_by")}
                    f["opened"] += 1
                else:
                    key = _skip_key(c["reason"], strat)
                    skipped[key] = skipped.get(key, 0) + 1
            d += timedelta(days=1)
        open_left = sum(1 for sp in books.values() if sp)
        if open_left:
            skipped["still open at the end"] = skipped.get("still open at the end", 0) + open_left
    shown = [s for s in STRATEGIES if s in chosen] if compare else \
        list(CREDIT) + (list(DEBIT) if cfg.get("debit_below_vix") else [])
    out = {"status": "ok", "trades": trades, "skipped": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
           "funnel": funnel,
           "options": {"strategies": chosen or None, "condor_in_unstable": condor_in_unstable,
                       "min_credit_ratio_single": cfg.get("min_credit_ratio_single"),
                       "debit_below_vix": cfg.get("debit_below_vix"),
                       "min_credit_ratio": cfg["min_credit_ratio"]},
           "all": _stats(trades),
           "by_symbol": {s: _stats([t for t in trades if t["symbol"] == s]) for s in symbols},
           "by_strategy": {s: _stats([t for t in trades if t["strategy"] == s]) for s in shown},
           "by_regime": {r: _stats([t for t in trades if t["regime"] == r])
                         for r in sorted({t["regime"] for t in trades})},
           "by_exit": {k: _stats([t for t in trades if t["exit_reason"] == k])
                       for k in ("take_profit", "stop", "expiry_exit")},
           "by_year": {y: _stats([t for t in trades if t["signal_date"][:4] == y])
                       for y in sorted({t["signal_date"][:4] for t in trades})},
           "limitations": ["Exits are checked on daily closes only: a stop can be passed during the day.",
                           "Fills at the day's closing prices; no bid-ask spread, costs or slippage.",
                           "SENSEX uses the NIFTY regime.",
                           "Only days with stored option files are replayed."]}
    if compare:
        out["limitations"].append("Comparing strategies: each strategy keeps its own book, so on one index a "
                                  "bull put and a bull call can be open together; add them up only as separate books.")
    if split_date:
        cut = split_date.isoformat()
        out["by_period"] = {"train": _stats([t for t in trades if t["signal_date"] < cut]),
                            "test": _stats([t for t in trades if t["signal_date"] >= cut])}
    return out


# --- live book ------------------------------------------------------------------------------------

def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS option_spreads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, signal_date TEXT NOT NULL,
            expiry TEXT NOT NULL, strategy TEXT NOT NULL, legs TEXT NOT NULL, credit REAL, width REAL,
            lot_size INTEGER, lots INTEGER, regime TEXT, vix REAL, exit_by TEXT,
            decision TEXT NOT NULL DEFAULT 'shortlisted', panel_decision TEXT, panel TEXT,
            status TEXT NOT NULL DEFAULT 'open', last_value REAL, exit_date TEXT, exit_value REAL,
            exit_reason TEXT, pnl_points REAL, pnl_r REAL, pnl_inr REAL, alert TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE (symbol, signal_date, expiry, strategy))""")
        have = {r["name"] for r in conn.execute("PRAGMA table_info(option_spreads)")}
        for col, kind in INTRADAY_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE option_spreads ADD COLUMN {col} {kind}")


# intraday entries (24 Nov 2026): when and why, and the 1H close that invalidates the trade
INTRADAY_COLUMNS = (("mode", "TEXT"), ("signal_ts", "TEXT"), ("setup", "TEXT"), ("direction", "TEXT"),
                    ("invalid", "TEXT"), ("iv_pct", "REAL"), ("spot", "REAL"), ("last_check_at", "TEXT"),
                    ("event_note", "TEXT"))


def _log_scan(now: datetime, manual: bool, results: dict, signals: dict) -> None:
    """Every intraday spread scan's outcome per index (asked 25 Nov 2026: the reasons were lost)."""
    try:
        with get_db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS spread_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, bar TEXT, symbol TEXT NOT NULL,
                result TEXT, signal INTEGER NOT NULL DEFAULT 0, regime TEXT, manual INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_spread_scans_at ON spread_scans(at)")
            for symbol, result in results.items():
                ev = signals.get(symbol) or {}
                sig = ev.get("signal") or {}
                conn.execute("INSERT INTO spread_scans (at, bar, symbol, result, signal, regime, manual) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (now.isoformat(timespec="seconds"), sig.get("signal_ts"), symbol, str(result)[:300],
                              1 if sig else 0, ev.get("regime"), 1 if manual else 0))
    except Exception:   # the log must never stop a scan
        pass


def scan_log(day: date | None = None) -> list[dict]:
    """The day's intraday spread scans, newest first."""
    day = day or date.today()
    with get_db() as conn:
        have = conn.execute("SELECT name FROM sqlite_master WHERE name = 'spread_scans'").fetchone()
        if not have:
            return []
        rows = conn.execute("SELECT at, bar, symbol, result, signal, regime, manual FROM spread_scans "
                            "WHERE substr(at, 1, 10) = ? ORDER BY at DESC, symbol", (day.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def evaluations_today(today: date) -> int:
    ensure_table()
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM option_spreads WHERE panel_decision IN ('approve', 'reject') "  # errors don't use the budget
            "AND substr(json_extract(panel, '$.evaluated_at'), 1, 10) = ?", (today.isoformat(),)).fetchone()[0]


def spreads(status: str | None = None) -> list[dict]:
    ensure_table()
    q, args = "SELECT * FROM option_spreads", ()
    if status:
        q, args = q + " WHERE status = ?", (status,)
    with get_db() as conn:
        rows = conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["legs"] = json.loads(item["legs"])
        item["panel"] = json.loads(item["panel"]) if item["panel"] else None
        item["invalid"] = json.loads(item["invalid"]) if item.get("invalid") else None
        out.append(item)
    return out


def _legs_text(sp: dict) -> str:
    return " / ".join(f"{'Sell' if l['side'] == 'short' else 'Buy'} {l['strike']:g} {l['type']} @ {l['price']:.1f}"
                      for l in sp["legs"])


def build_prompt(sp: dict) -> str:
    plan = {k: sp[k] for k in ("symbol", "strategy", "expiry", "credit", "width", "lots", "lot_size", "regime", "vix",
                               "exit_by")}
    plan["legs"] = sp["legs"]
    intraday = sp.get("mode") == "intraday"
    if intraday:
        plan.update({k: sp.get(k) for k in ("signal_ts", "setup", "direction", "invalid", "iv_pct", "spot")})
    cfg = settings()
    if is_debit(sp["strategy"]):
        plan["debit"] = plan.pop("credit")
        d = cfg["debit"]
        exits = (f"Exits: take profit when the spread is worth +{d['take_profit_pct']}% of the debit, stop when it "
                 f"has lost {d['stop_pct']}% of the debit, otherwise close on {sp['exit_by']} (day before expiry). "
                 f"Chosen because India VIX is below {cfg.get('debit_below_vix')}: options are cheap, so it buys "
                 "the spread instead of selling one.\n")
        kind = "debit"
    else:
        exits = (f"Exits: take profit at {cfg['take_profit_pct']}% of the credit, stop when the spread costs "
                 f"{cfg['stop_multiple']:g}× the credit, otherwise close on {sp['exit_by']} (day before expiry).\n")
        kind = "credit"
    if intraday:
        exits = exits.replace(f"otherwise close on {sp['exit_by']}", f"or when a 1H close of the index passes the "
                              f"invalidation level; otherwise close at the 15:15 1H close on {sp['exit_by']}")
        exits += ("Exits are checked on each 1H close. Entry: daily regime, 4H trend confirmation, a 1H setup "
                  "(pullback/breakout, or a range rejection for an iron condor) and a 15-minute trigger all agreed. "
                  "iv_pct = today's ATM implied volatility percentile (debit spread below the threshold).\n")
        priced = f"live bid/ask mid prices at {sp.get('signal_ts')}"
    else:
        priced = f"closing prices of {sp['signal_date']}"
    if sp.get("event_note"):
        exits += (f"Event risk: {sp['event_note']}. The rules allow it at reduced size; weigh the gap risk "
                  "through the short strike.\n")
    return (
        f"You review one index option {kind} spread for a paper-trading book (advisory only, the user "
        "places orders by hand). The rules already chose it; you can only approve or reject, not change it.\n"
        + exits +
        f"Plan ({priced}):\n{json.dumps(plan, indent=1)}\n"
        'Answer with JSON only: {"decision": "approve" | "reject", "confidence": 0-100, '
        '"reasons": [up to 3 short strings], "risks": [up to 2 short strings]}')


def build_alert(sp: dict, panel: dict | None, kind: str) -> str:
    name = NAMES[sp["strategy"]]
    cfg = settings()
    head = "✅ AI-approved" if kind == "approved" else "⚠️ Rules only (" + kind + ")"
    if is_debit(sp["strategy"]):
        d = cfg["debit"]
        target = min(sp["credit"] * (1 + d["take_profit_pct"] / 100), 0.9 * sp["width"])
        money = f"Debit {sp['credit']:.1f} pts (you pay) · width {sp['width']:g} · {sp['lots']} lot(s) × {sp['lot_size']}"
        exits = (f"Take profit ≥ {target:.1f} · stop ≤ {sp['credit'] * (1 - d['stop_pct'] / 100):.1f} · "
                 f"exit by {sp['exit_by']}")
    else:
        money = f"Credit {sp['credit']:.1f} pts · width {sp['width']:g} · {sp['lots']} lot(s) × {sp['lot_size']}"
        exits = (f"Take profit ≤ {sp['credit'] * (1 - cfg['take_profit_pct'] / 100):.1f} · "
                 f"stop ≥ {sp['credit'] * cfg['stop_multiple']:.1f} · exit by {sp['exit_by']}")
    intraday = sp.get("mode") == "intraday"
    if intraday:
        exits = exits.replace(f"exit by {sp['exit_by']}", f"exit at the 15:15 1H close on {sp['exit_by']}")
    lines = [f"{head} — {sp['symbol']} {name} (paper)", f"Expiry {sp['expiry']} · {_legs_text(sp)}", money, exits]
    if sp.get("event_note"):
        lines.append(f"⚠️ {sp['event_note']}")
    if intraday:
        inv = sp.get("invalid")
        inv_text = (f"1H close outside {inv['below']:.0f}–{inv['above']:.0f}" if isinstance(inv, dict) else
                    f"1H close {'below' if sp.get('direction') == 'up' else 'above'} {inv:.0f}" if inv else "—")
        iv = f"IV pct {sp['iv_pct']:.0f}" if sp.get("iv_pct") is not None else "IV pct —"
        lines += [f"Setup {str(sp.get('setup') or '').replace('_', ' ')} · invalid on {inv_text} (checked each 1H close)",
                  f"Regime {sp['regime']} · {iv} · India VIX {sp['vix'] or '—'}",
                  f"Prices are live mids at {str(sp.get('signal_ts') or '')[11:16]}; check them before you place it."]
    else:
        lines += [f"Regime {sp['regime']} · India VIX {sp['vix'] or '—'}",
                  "Prices are today's closes; check them at the open."]
    if panel and panel.get("reasons"):
        lines.append("AI: " + "; ".join(panel["reasons"]))
    if panel and panel.get("risks"):
        lines.append("Risks: " + "; ".join(panel["risks"]))
    return "\n".join(lines)


def _send(text: str) -> str:
    try:
        from backend.notifications.telegram import send_message
        send_message(text)
        return "sent"
    except Exception as exc:
        return f"not sent: {type(exc).__name__}"


def run_now(today: date | None = None, llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    """The page's "Run now" (asked 24 Nov 2026): run on the latest day that has stored option files.
    Today's files only exist after the evening download (holidays have none), so before that this
    re-runs the latest evening — safe to repeat: candidates are unique per index and day, and only
    new ones go to the AI."""
    from backend.options import store
    if intraday_on():
        return scan_intraday(llm_call=llm_call, manual=True)
    today = today or date.today()
    days = [d for d in (store.latest_date(sym) for sym in settings()["symbols"]) if d and d <= today.isoformat()]
    if not days:
        return {"status": "skipped", "reason": "no option files stored yet — download them on the Options data page"}
    latest = date.fromisoformat(max(days))
    out = run_eod(latest, llm_call=llm_call)
    if latest != today:
        from tradingagents.utils.market_calendar import holiday_name, is_trading_day
        why = ("market holiday" if today.weekday() < 5 and not is_trading_day(today) else
               "weekend" if today.weekday() >= 5 else "today's files arrive after the evening download (~19:00)")
        name = holiday_name(today)
        out["note"] = (f"No option file for today ({why}{': ' + name if name and why == 'market holiday' else ''}); "
                       f"ran on the latest stored files, {latest:%a %d %b}.")
    return out


def run_eod(day: date | None = None, llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    """After the day's option files are stored: follow open spreads, then today's candidates."""
    from backend.journal import panel as journal_panel
    from backend.options import store
    from backend.system_state import is_paused

    ensure_table()
    cfg = settings()
    day = day or date.today()
    iso = day.isoformat()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "option spreads disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    intraday = cfg["mode"] == "intraday"
    closed = []
    for sp in spreads("open"):
        if intraday and (sp.get("last_check_at") or "")[:10] >= iso:
            continue        # already followed on today's 1H closes
        chain = store.chain(sp["symbol"], iso, sp["expiry"])
        if not chain:
            continue
        val = value(sp["legs"], _prices(chain), is_debit(sp["strategy"]))
        reason = exit_check(sp, val, iso, sp["exit_by"], cfg)
        with get_db() as conn:
            if reason:
                p = pnl(sp, val, cfg)
                conn.execute("UPDATE option_spreads SET status='closed', last_value=?, exit_date=?, exit_value=?, "
                             "exit_reason=?, pnl_points=?, pnl_r=?, pnl_inr=? WHERE id=?",
                             (val, iso, round(val, 2), reason, p["pnl_points"], p["pnl_r"], p["pnl_inr"], sp["id"]))
                if sp["decision"] == "approved":
                    _send(f"🔚 {sp['symbol']} {sp['strategy'].replace('_', ' ')} — close ({reason.replace('_', ' ')}) "
                          f"at {val:.1f} pts · {p['pnl_r']:+.2f}R · ₹{p['pnl_inr']:,.0f} (paper)")
                closed.append(sp["symbol"])
            elif val is not None:
                conn.execute("UPDATE option_spreads SET last_value=? WHERE id=?", (round(val, 2), sp["id"]))
    if intraday:
        return {"status": "ok", "date": iso, "closed": closed, "results": {}, "new": [], "reviews": [],
                "note": "intraday mode: new spreads come from the 15-minute scan; this run only followed "
                        "spreads no 1H check reached today"}
    open_now = spreads("open")
    held = {s["symbol"] for s in open_now if s["decision"] in ("approved", "shortlisted")}
    cache: dict = {}
    results, new = {}, []
    for symbol in cfg["symbols"]:
        if symbol in held:
            results[symbol] = "spread already open"
            continue
        if len([s for s in open_now if s["decision"] == "approved"]) >= cfg["max_open"]:
            results[symbol] = f"{cfg['max_open']} spreads already open"
            continue
        exps = store.expiries(symbol, iso)
        if not exps:
            results[symbol] = "no option file for today"
            continue
        c = candidate(symbol, day, cfg, lambda d, e: store.chain(symbol, d, e), lambda d: exps,
                      _regime_on(symbol, iso, cfg, cache), store.vix_on(iso))
        if not c["ok"]:
            results[symbol] = c["reason"]
            continue
        with get_db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO option_spreads (symbol, signal_date, expiry, strategy, legs, credit, width, "
                "lot_size, lots, regime, vix, exit_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, iso, c["expiry"], c["strategy"], json.dumps(c["legs"]), c["credit"], c["width"],
                 c["lot_size"], c["lots"], c["regime"], c["vix"], c["exit_by"]))
            if not cur.rowcount:
                results[symbol] = "already recorded today"
                continue
            c["id"] = cur.lastrowid
        new.append(c)
        results[symbol] = f"{c['strategy']} shortlisted"
    reviews = _review_new(new, day, llm_call)
    return {"status": "ok", "date": iso, "closed": closed, "results": results, "new": [c["symbol"] for c in new],
            "reviews": reviews}


def _review_new(new: list[dict], day: date, llm_call) -> list[dict]:
    from backend.journal import panel as journal_panel
    if not new:
        return []
    if llm_call is None and os.environ.get("TRADINGAGENTS_AI_PANEL", "").lower() == "off":
        reviews = [{"symbol": c["symbol"], "decision": "shortlisted", "reason": "AI panel off (env)"} for c in new]
    elif get_config().global_.is_shadow("option_spreads"):
        reviews = [_skip(c, "shadow strategy: tracked, not reviewed") for c in new]
    else:
        # Best candidate first (credit as a share of the width); with top_per_scan 1 only it is reviewed.
        new.sort(key=lambda c: -(c["credit"] / c["width"] if c.get("width") else 0))
        top = get_config().global_.ai_panel.top_per_scan
        reviews = [_review(c, day, llm_call or journal_panel._default_llm_call) for c in new[:top]]
        reviews += [_skip(c, f"lower credit/width than {new[0]['symbol']} in the same run") for c in new[top:]]
    return reviews


def _review(sp: dict, day: date, llm_call) -> dict:
    from backend.journal import panel as journal_panel

    g = get_config().global_
    budget = journal_panel.review_budget_block(day, "option_spreads")   # total cap + spreads' own share
    if not g.ai_panel.enabled or budget:
        why = "AI panel off" if not g.ai_panel.enabled else budget
        alert = _send(build_alert(sp, None, "not reviewed")) if g.ai_panel.notify_unreviewed else "off"
        _update(sp["id"], "not_sent", {"evaluated_at": None, "skipped": why}, None, alert)
        return {"symbol": sp["symbol"], "decision": "not_sent", "reason": why}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        text, model = llm_call(build_prompt(sp))
        verdict = journal_panel.parse_reply(text)
    except Exception as exc:
        _update(sp["id"], "error", {"evaluated_at": now, "error": f"{type(exc).__name__}: {exc}"[:200]}, None,
                _send(build_alert(sp, None, "AI review failed")))
        return {"symbol": sp["symbol"], "decision": "error"}
    panel = {**verdict, "evaluated_at": now, "model": model}
    approved = verdict["decision"] == "approve" and verdict["confidence"] >= g.ai_panel.min_confidence
    alert = _send(build_alert(sp, panel, "approved")) if approved and g.ai_panel.notify_telegram else None
    _update(sp["id"], verdict["decision"], panel, "approved" if approved else "rejected_panel", alert)
    return {"symbol": sp["symbol"], "decision": "approved" if approved else "rejected_panel",
            "confidence": verdict["confidence"]}


def _skip(sp: dict, why: str) -> dict:
    """Not reviewed and not alerted; still tracked."""
    _update(sp["id"], "not_sent", {"evaluated_at": None, "skipped": why}, None, "off")
    return {"symbol": sp["symbol"], "decision": "not_sent", "reason": why}


def _update(sid: int, panel_decision: str, panel: dict, decision: str | None, alert: str | None) -> None:
    with get_db() as conn:
        conn.execute("UPDATE option_spreads SET panel_decision=?, panel=?, decision=COALESCE(?, decision), alert=? "
                     "WHERE id=?", (panel_decision, json.dumps(panel), decision, alert, sid))


def record() -> dict:
    rows = spreads("closed")
    return {"paper": _stats([r for r in rows if r["decision"] == "approved"]), "all": _stats(rows)}


# --- intraday (multi-timeframe) live mode, agreed 24 Nov 2026 ------------------------------------

def intraday_on() -> bool:
    try:
        cfg = settings()
    except Exception:
        return False
    return bool(cfg["enabled"]) and cfg["mode"] == "intraday"


def trend_sources() -> list[str]:
    """Indices whose bars time the entries (SENSEX follows NIFTY through ``regime_source``)."""
    cfg = settings()
    return sorted({cfg["regime_source"].get(s, s) for s in cfg["symbols"]})


def trend_bars(name: str, now: datetime) -> dict:
    """Completed 1H, 4H and 15m bars of an index's spot, as of ``now``."""
    from backend.marketdata.candles import get_candles, resample_4h
    from backend.marketdata.instruments import spot_instrument
    from backend.options.spread_mtf import completed, completed_4h
    from tradingagents.utils.market_calendar import NSE

    spot = spot_instrument(name)
    if not spot:
        return {"h1": [], "h4": [], "m15": []}
    h1 = completed(get_candles(spot["instrument_token"], "60minute", limit=700), 60, now)
    return {"h1": h1, "h4": completed_4h(resample_4h(h1, NSE), now),
            "m15": completed(get_candles(spot["instrument_token"], "15minute", limit=300), 15, now)}


def alignment(now: datetime | None = None) -> dict:
    """Daily · 4H · 1H · 15m state per index (the Option Spreads page), no Kite call."""
    from backend.options.spread_mtf import evaluate
    cfg = settings()
    now = now or datetime.now()
    cache: dict = {}
    bars: dict = {}
    out = {}
    for symbol in cfg["symbols"]:
        src = cfg["regime_source"].get(symbol, symbol)
        if src not in bars:
            bars[src] = trend_bars(src, now)
        regime = _regime_on(symbol, now.date().isoformat(), cfg, cache)
        b = bars[src]
        ev = evaluate(regime, b["h1"], b["h4"], b["m15"], now, cfg["intraday"])
        ev["source"] = src
        ev["last_15m"] = b["m15"][-1]["ts"] if b["m15"] else None
        out[symbol] = ev
    return {"mode": cfg["mode"], "at": now.isoformat(timespec="minutes"), "symbols": out}


def _mid(q: dict) -> tuple[float | None, float | None, float | None]:
    depth = q.get("depth") or {}
    try:
        bid, ask = depth["buy"][0]["price"], depth["sell"][0]["price"]
    except (KeyError, IndexError, TypeError):
        bid = ask = None
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2, bid, ask
    lp = q.get("last_price")
    return (lp if lp and lp > 0 else None), bid, ask


def _spot_key(symbol: str) -> str:
    from backend.marketdata.store import BFO_INDICES, INDEX_SPOT_SYMBOLS
    return f"{'BSE' if symbol in BFO_INDICES else 'NSE'}:{INDEX_SPOT_SYMBOLS[symbol]}"


def live_chain(client, symbol: str, expiry: str, today: date, cfg: dict) -> tuple[list[dict], float | None, float | None]:
    """One expiry's options within ``strike_range_pct`` of spot from Kite quotes (mid price, bid,
    ask, OI), with forward / IV / Greeks added as for the evening files. Returns (rows, spot, live VIX)."""
    from backend.marketdata.instruments import option_contracts
    from backend.options.bhavcopy import enrich

    head = client.quote([_spot_key(symbol), "NSE:INDIA VIX"]) or {}
    spot = (head.get(_spot_key(symbol)) or {}).get("last_price")
    vix = (head.get("NSE:INDIA VIX") or {}).get("last_price") or None
    if not spot:
        return [], None, vix
    band = cfg["intraday"]["strike_range_pct"] / 100 * spot
    contracts = [c for c in option_contracts(symbol, expiry) if abs(c["strike"] - spot) <= band]
    keys = [f"{c['exchange']}:{c['tradingsymbol']}" for c in contracts]
    quotes: dict = {}
    for i in range(0, len(keys), 450):       # Kite: at most 500 instruments per quote call
        quotes.update(client.quote(keys[i:i + 450]) or {})
    rows = []
    for c, key in zip(contracts, keys):
        mid, bid, ask = _mid(quotes.get(key) or {})
        rows.append({"symbol": symbol, "expiry": expiry, "strike": float(c["strike"]), "opt_type": c["instrument_type"],
                     "close": round(mid, 2) if mid else None, "settle": None, "bid": bid, "ask": ask,
                     "oi": (quotes.get(key) or {}).get("oi"), "tradingsymbol": c["tradingsymbol"],
                     "exchange": c["exchange"]})
    r = get_config().global_.option_data.risk_free_rate
    return enrich(rows, today, r), spot, vix


def atm_iv(chain: list[dict]) -> float | None:
    pairs: dict = {}
    for row in chain:
        if row.get("iv") and row.get("forward"):
            pairs.setdefault(row["strike"], {})[row["opt_type"]] = row
    both = [(k, v) for k, v in pairs.items() if "CE" in v and "PE" in v]
    if not both:
        return None
    k, v = min(both, key=lambda kv: abs(kv[0] - kv[1]["CE"]["forward"]))
    return (v["CE"]["iv"] + v["PE"]["iv"]) / 2


def iv_percentile(symbol: str, day: date, iv_now: float | None, cfg: dict) -> float | None:
    """Share (%) of the last ``iv_lookback`` stored days whose ATM IV was below ``iv_now``."""
    from backend.options import store
    if iv_now is None:
        return None
    hist = [v for _, v in store.atm_iv_history(symbol, day.isoformat(), days=int(cfg["iv_lookback"] * 1.6))]
    hist = hist[-int(cfg["iv_lookback"]):]
    if len(hist) < cfg["iv_min_history"]:
        return None
    return round(100 * sum(1 for v in hist if v < iv_now) / len(hist), 1)


def legs_liquid(legs: list[dict], chain: list[dict], cfg: dict) -> str:
    """'' or why a leg's bid-ask is too wide (or missing)."""
    by_key = {(r["strike"], r["opt_type"]): r for r in chain}
    ic = cfg["intraday"]
    for leg in legs:
        row = by_key.get((leg["strike"], leg["type"])) or {}
        bid, ask = row.get("bid"), row.get("ask")
        if not bid or not ask or bid <= 0 or ask <= 0:
            return f"no bid/ask on {leg['strike']:g} {leg['type']}"
        mid = (bid + ask) / 2
        limit = max(ic["min_leg_spread_points"], ic["max_leg_spread_pct"] / 100 * mid)
        if ask - bid > limit:
            return f"{leg['strike']:g} {leg['type']} bid-ask {ask - bid:.1f} > {limit:.1f}"
        leg["tradingsymbol"], leg["exchange"] = row.get("tradingsymbol"), row.get("exchange")
    return ""


def _strategy_for(signal: dict, regime: str, iv_pct: float | None, vix: float | None, cfg: dict) -> str | None:
    if signal["direction"] == "range":
        return cfg["regime_strategy"].get("SIDEWAYS")
    base = base_strategy(regime, cfg)
    return by_iv(base, iv_pct, vix, cfg) if base else None


def _nse_open(now: datetime) -> bool:
    from tradingagents.utils.market_calendar import NSE, session_windows
    return any(a <= now.time() <= b for a, b in session_windows(NSE, now.date()))


def scan_intraday(now: datetime | None = None, client=None, llm_call=None, manual: bool = False) -> dict:
    """After each completed 15-minute bar: every index without an open spread is checked on the
    daily → 4H → 1H → 15m chain; a full agreement builds the spread from the live chain."""
    from backend.marketdata.instruments import option_expiries
    from backend.options import store
    from backend.options.spread_mtf import evaluate
    from backend.system_state import is_paused

    ensure_table()
    cfg = settings()
    now = now or datetime.now()
    day, iso = now.date(), now.date().isoformat()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "option spreads disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    if manual and not _nse_open(now):
        return {"status": "skipped", "reason": "NSE is closed — the spread scan runs after each 15-minute bar "
                                               "in market hours", "alignment": alignment(now)["symbols"]}
    open_now = spreads("open")
    held = {s["symbol"] for s in open_now if s["decision"] in ("approved", "shortlisted")}
    cache, bars = {}, {}
    results, new, signals = {}, [], {}
    for symbol in cfg["symbols"]:
        src = cfg["regime_source"].get(symbol, symbol)
        if src not in bars:
            bars[src] = trend_bars(src, now)
        regime = _regime_on(symbol, iso, cfg, cache)
        b = bars[src]
        ev = evaluate(regime, b["h1"], b["h4"], b["m15"], now, cfg["intraday"])
        signals[symbol] = ev
        if symbol in held:
            results[symbol] = "spread already open"
            continue
        if not ev["signal"]:
            results[symbol] = ev.get("reason") or "no signal"
            continue
        if len([s for s in open_now if s["decision"] == "approved"]) >= cfg["max_open"]:
            results[symbol] = f"{cfg['max_open']} spreads already open"
            continue
        sig = ev["signal"]
        with get_db() as conn:
            seen = conn.execute("SELECT 1 FROM option_spreads WHERE symbol = ? AND signal_ts = ?",
                                (symbol, sig["signal_ts"])).fetchone()
        if seen:
            results[symbol] = "already recorded for this bar"
            continue
        rule = cfg["symbols"][symbol]
        expiry = pick_expiry(day, option_expiries(symbol, day), rule)
        if not expiry:
            results[symbol] = "outside the entry window (days to expiry / weekday)"
            continue
        blocked = _blocked(day, expiry)
        event_note, size_cfg = None, cfg
        if blocked and blocked.startswith("event before expiry") and cfg.get("event_size_mult"):
            mult = float(cfg["event_size_mult"])
            event_note = f"{blocked[len('event before expiry: '):]} before expiry — {mult:g}× size"
            size_cfg = {**cfg, "risk_per_trade_pct": cfg["risk_per_trade_pct"] * mult,
                        "max_loss_pct": cfg["max_loss_pct"] * mult}
        elif blocked:
            results[symbol] = blocked
            continue
        if client is None:
            try:
                from backend.brokers.kite import get_authenticated_client
                client = get_authenticated_client()
            except Exception as exc:
                reason = f"Kite: {type(exc).__name__}: {exc}"[:200]
                results[symbol] = reason
                _log_scan(now, manual, results, signals)
                return {"status": "error", "reason": reason, "results": results}
        try:
            chain, spot, vix_live = live_chain(client, symbol, expiry, day, cfg)
        except Exception as exc:
            results[symbol] = f"live chain failed: {type(exc).__name__}"
            continue
        if not chain:
            results[symbol] = "no live quote for the index"
            continue
        vix = vix_live or store.vix_on(iso)
        iv_pct = iv_percentile(symbol, day, atm_iv(chain), cfg)
        strategy = _strategy_for(sig, regime, iv_pct, vix, cfg)
        if not strategy:
            results[symbol] = f"no spread in a {regime} regime"
            continue
        if vix is not None and vix > cfg["max_vix"]:
            results[symbol] = f"India VIX {vix:.1f} > {cfg['max_vix']:g}"
            continue
        base = {"symbol": symbol, "signal_date": iso, "regime": regime, "vix": round(vix, 2) if vix else None}
        c = _finish(base, chain, strategy, rule, size_cfg, expiry)
        if not c["ok"]:
            results[symbol] = c["reason"] + (f" ({event_note})" if event_note else "")
            continue
        why = legs_liquid(c["legs"], chain, cfg)
        if why:
            results[symbol] = why
            continue
        c.update({"mode": "intraday", "signal_ts": sig["signal_ts"], "setup": sig["kind"],
                  "direction": sig["direction"], "invalid": sig["invalid"], "iv_pct": iv_pct, "spot": spot,
                  "event_note": event_note})
        with get_db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO option_spreads (symbol, signal_date, expiry, strategy, legs, credit, width, "
                "lot_size, lots, regime, vix, exit_by, mode, signal_ts, setup, direction, invalid, iv_pct, spot, "
                "last_check_at, event_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, iso, c["expiry"], c["strategy"], json.dumps(c["legs"]), c["credit"], c["width"],
                 c["lot_size"], c["lots"], c["regime"], c["vix"], c["exit_by"], "intraday", c["signal_ts"],
                 c["setup"], c["direction"], json.dumps(c["invalid"]), iv_pct, spot,
                 now.isoformat(timespec="seconds"), event_note))
            if not cur.rowcount:
                results[symbol] = "already recorded today (same expiry and strategy)"
                continue
            c["id"] = cur.lastrowid
        new.append(c)
        results[symbol] = f"{c['strategy']} shortlisted ({sig['kind'].replace('_', ' ')})" + \
            (f" — {event_note}" if event_note else "")
        open_now.append({**c, "decision": "shortlisted"})
    reviews = _review_new(new, day, llm_call)
    _log_scan(now, manual, results, signals)
    return {"status": "ok", "at": now.isoformat(timespec="minutes"), "results": results,
            "new": [c["symbol"] for c in new], "reviews": reviews,
            "alignment": {k: {kk: v.get(kk) for kk in ("regime", "bias", "h4", "h1", "m15", "reason")}
                          for k, v in signals.items()}}


def _leg_keys(sp: dict) -> dict:
    """'EXCHANGE:TRADINGSYMBOL' per (strike, type); older (evening) spreads are looked up in the master."""
    from backend.marketdata.instruments import option_contracts
    out, missing = {}, []
    for leg in sp["legs"]:
        if leg.get("tradingsymbol") and leg.get("exchange"):
            out[(leg["strike"], leg["type"])] = f"{leg['exchange']}:{leg['tradingsymbol']}"
        else:
            missing.append(leg)
    if missing:
        by = {(float(c["strike"]), c["instrument_type"]): c for c in option_contracts(sp["symbol"], sp["expiry"])}
        for leg in missing:
            c = by.get((float(leg["strike"]), leg["type"]))
            if c:
                out[(leg["strike"], leg["type"])] = f"{c['exchange']}:{c['tradingsymbol']}"
    return out


EXIT_DAY_TIME = "15:15"


def follow_intraday(now: datetime | None = None, client=None) -> dict:
    """On each 1H close: every open spread is valued from live quotes; take profit / stop, the trend
    index's 1H close beyond the invalidation level, or the 15:15 1H close on the exit day closes it."""
    from backend.options.spread_mtf import invalidated
    from backend.system_state import is_paused

    ensure_table()
    cfg = settings()
    now = now or datetime.now()
    iso = now.date().isoformat()
    if not cfg["enabled"] or cfg["mode"] != "intraday":
        return {"status": "skipped", "reason": "intraday mode off"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    open_sp = spreads("open")
    if not open_sp:
        return {"status": "ok", "followed": 0, "closed": []}
    if client is None:
        try:
            from backend.brokers.kite import get_authenticated_client
            client = get_authenticated_client()
        except Exception as exc:
            return {"status": "error", "reason": f"Kite: {type(exc).__name__}: {exc}"[:200]}
    keys = {sp["id"]: _leg_keys(sp) for sp in open_sp}
    try:
        quotes = client.quote(sorted({k for m in keys.values() for k in m.values()})) or {}
    except Exception as exc:
        return {"status": "error", "reason": f"quote failed: {type(exc).__name__}"}
    closes: dict = {}
    closed, followed = [], 0
    exit_time = datetime.strptime(EXIT_DAY_TIME, "%H:%M").time()
    for sp in open_sp:
        prices = {}
        for leg_key, key in keys[sp["id"]].items():
            mid = _mid(quotes.get(key) or {})[0]
            if mid is not None:
                prices[leg_key] = mid
        val = value(sp["legs"], prices, is_debit(sp["strategy"]))
        if val is None:
            continue
        followed += 1
        src = cfg["regime_source"].get(sp["symbol"], sp["symbol"])
        if src not in closes:
            h1 = trend_bars(src, now)["h1"]
            closes[src] = h1[-1]["close"] if h1 and h1[-1]["ts"][:10] == iso else None
        last_day = sp["exit_by"] if now.time() >= exit_time else "9999-12-31"
        reason = exit_check(sp, val, iso, last_day, cfg)
        if not reason and sp.get("mode") == "intraday" and invalidated(sp.get("invalid"), sp.get("direction"), closes[src]):
            reason = "invalidated"
        with get_db() as conn:
            if reason:
                p = pnl(sp, val, cfg)
                conn.execute("UPDATE option_spreads SET status='closed', last_value=?, exit_date=?, exit_value=?, "
                             "exit_reason=?, pnl_points=?, pnl_r=?, pnl_inr=?, last_check_at=? WHERE id=?",
                             (round(val, 2), iso, round(val, 2), reason, p["pnl_points"], p["pnl_r"], p["pnl_inr"],
                              now.isoformat(timespec="seconds"), sp["id"]))
                if sp["decision"] == "approved":
                    extra = (f" (index 1H close {closes[src]:.0f})" if reason == "invalidated" and closes[src] else "")
                    _send(f"🔚 {sp['symbol']} {NAMES[sp['strategy']]} — close ({reason.replace('_', ' ')}{extra}) "
                          f"at {val:.1f} pts · {p['pnl_r']:+.2f}R · ₹{p['pnl_inr']:,.0f} (paper, 1H close {now:%H:%M})")
                closed.append({"symbol": sp["symbol"], "reason": reason})
            else:
                conn.execute("UPDATE option_spreads SET last_value=?, last_check_at=? WHERE id=?",
                             (round(val, 2), now.isoformat(timespec="seconds"), sp["id"]))
    return {"status": "ok", "followed": followed, "closed": closed}
