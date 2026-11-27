"""Pairs trading (research, shadow) — Qureshi & Zaman (2024) "Pairs Trading Using a Novel
Graphical Matching Approach", plus GOLDM/SILVERM.

Each month (first session):
  * for every pair of liquid stocks with complete closes over ``formation_days``: regress
    log(y) on log(x); the residual is the spread; a Dickey-Fuller t-statistic on the spread
    measures mean reversion and its AR(1) coefficient gives the half-life;
  * keep pairs with t < ``max_adf_t`` and a half-life in range, then pick the pairs with a
    maximum-weight matching on the graph (weight = -t), so every stock is in at most one pair
    (the paper's point: pairs sharing a stock add variance);
  * the spread's median and interquartile range over the formation window fix the q-score.
Every day: q = (spread - median) / IQR. |q| ≥ entry_q → trade the spread back towards the
median (short the spread when q > 0). Exit when |q| ≤ exit_q or q crosses zero, stop at
|q| ≥ stop_q (which defines 1R), time exit after ``max_hold_days``, and close any open trade
whose pair isn't kept at the next formation. Signals on the close of day t fill at the close
of day t+1.

R = (spread change in the trade's favour) / ((stop_q - |q at entry|) × IQR) − costs (4 legs).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable

import numpy as np

from backend.research import config as rcfg
from backend.research.data import daily_bars, point_value

LIMITS_STOCKS = [
    "Universe = today's F&O stocks (survivorship); stocks that left F&O or were delisted are missing.",
    "Spot closes stand in for futures; the futures basis and roll are covered only by the cost model.",
    "Fills at the next day's close; lot sizes are today's; borrowing is via futures (no short-sale limits).",
    "Median/IQR of the spread are fixed at each monthly formation (the paper re-fits daily).",
]
LIMITS_METALS = [
    "Kite's continuous MCX series is not back-adjusted: each roll can add a small jump to one leg.",
    "Trades the GOLDM/SILVERM spread even when the cointegration test fails (recorded per month).",
    "Fills at the next day's close; ₹ per point per lot from the YAML (GOLDM 10, SILVERM 5).",
]


# --- statistics -------------------------------------------------------------------------------

def ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """(intercept, slope) of y on x."""
    xm, ym = x.mean(), y.mean()
    var = ((x - xm) ** 2).sum()
    b = float(((x - xm) * (y - ym)).sum() / var) if var > 0 else 0.0
    return float(ym - b * xm), b


def df_test(e: np.ndarray) -> tuple[float, float | None]:
    """Dickey-Fuller on a spread: (t-statistic of gamma in de_t = gamma e_{t-1} + u, half-life)."""
    lag, de = e[:-1] - e.mean(), np.diff(e)
    denom = (lag ** 2).sum()
    if denom <= 0 or len(de) < 20:
        return 0.0, None
    g = float((lag * de).sum() / denom)
    resid = de - g * lag
    s2 = float((resid ** 2).sum() / (len(de) - 1))
    se = math.sqrt(s2 / denom) if s2 > 0 else 0.0
    t = g / se if se > 0 else 0.0
    hl = -math.log(2) / math.log(1 + g) if -1 < g < 0 else None
    return t, hl


def all_pair_stats(logp: np.ndarray, names: list[str]) -> list[dict]:
    """Vectorised over every pair (i < j) of the columns of ``logp`` (rows = days)."""
    n_days, n = logp.shape
    out = []
    for i in range(n - 1):
        x = logp[:, i]
        ys = logp[:, i + 1:]
        xm = x.mean()
        xc = x - xm
        var = (xc ** 2).sum()
        if var <= 0:
            continue
        b = (xc[:, None] * (ys - ys.mean(axis=0))).sum(axis=0) / var
        a = ys.mean(axis=0) - b * xm
        e = ys - (a + b * x[:, None])
        lag = e[:-1] - e.mean(axis=0)
        de = np.diff(e, axis=0)
        denom = (lag ** 2).sum(axis=0)
        g = (lag * de).sum(axis=0) / np.where(denom > 0, denom, np.nan)
        resid = de - g * lag
        s2 = (resid ** 2).sum(axis=0) / (n_days - 2)
        t = g / np.sqrt(s2 / denom)
        for k in range(ys.shape[1]):
            if not np.isfinite(t[k]):
                continue
            gk = float(g[k])
            out.append({"x": names[i], "y": names[i + 1 + k], "a": float(a[k]), "b": float(b[k]), "t": float(t[k]),
                        "half_life": (-math.log(2) / math.log(1 + gk)) if -1 < gk < 0 else None})
    return out


def match_pairs(candidates: list[dict], max_pairs: int) -> list[dict]:
    """Maximum-weight matching (weight = -t): each stock in one pair at most."""
    import networkx as nx
    g = nx.Graph()
    for c in candidates:
        g.add_edge(c["x"], c["y"], weight=-c["t"], cand=c)
    chosen = [g.edges[u, v]["cand"] for u, v in nx.max_weight_matching(g, maxcardinality=False)]
    return sorted(chosen, key=lambda c: c["t"])[:max_pairs]


# --- simulation -------------------------------------------------------------------------------

def _closes(bars: list[dict]) -> dict[str, float]:
    return {b["ts"][:10]: float(b["close"]) for b in bars if b.get("close")}


def _form(day_idx: int, dates: list[str], series: dict[str, dict[str, float]], p: dict,
          eligible: Callable[[str, str], bool], force_pairs: list[tuple[str, str]] | None) -> dict:
    fd = int(p["formation_days"])
    window = dates[max(0, day_idx - fd):day_idx]
    if len(window) < fd * 0.9:
        return {"pairs": [], "tested": 0, "passed": 0, "reason": "not enough history"}
    names = [s for s, c in series.items() if eligible(s, dates[day_idx]) and all(d in c for d in window)]
    if len(names) < 2:
        return {"pairs": [], "tested": 0, "passed": 0, "reason": "fewer than 2 stocks with full history"}
    logp = np.log(np.array([[series[s][d] for s in names] for d in window]))
    if force_pairs:
        idx = {s: k for k, s in enumerate(names)}
        cands = []
        for xs, ys in force_pairs:
            if xs in idx and ys in idx:
                a, b = ols(logp[:, idx[ys]], logp[:, idx[xs]])
                e = logp[:, idx[ys]] - a - b * logp[:, idx[xs]]
                t, hl = df_test(e)
                cands.append({"x": xs, "y": ys, "a": a, "b": b, "t": t, "half_life": hl})
        tested = len(cands)
    else:
        cands = all_pair_stats(logp, names)
        tested = len(cands)
    lo, hi = float(p.get("min_half_life", 2)), float(p.get("max_half_life", 40))
    passing = [c for c in cands if c["b"] > 0 and c["t"] < float(p["max_adf_t"])
               and c["half_life"] is not None and lo <= c["half_life"] <= hi]
    if force_pairs and not p.get("require_cointegration", True):
        chosen = [c for c in cands if c["b"] > 0]
        for c in chosen:
            c["cointegrated"] = c in passing
    else:
        chosen = match_pairs(passing, int(p.get("max_pairs", 10))) if not force_pairs else passing
    idx = {s: k for k, s in enumerate(names)}
    for c in chosen:
        e = logp[:, idx[c["y"]]] - c["a"] - c["b"] * logp[:, idx[c["x"]]]
        q1, med, q3 = np.percentile(e, [25, 50, 75])
        c["median"], c["iqr"] = float(med), float(q3 - q1)
    return {"pairs": [c for c in chosen if c["iqr"] > 0], "tested": tested, "passed": len(passing)}


def _lots(pair: dict, px: float, py: float, risk_e: float, p: dict, unit: Callable[[str], float | None]) -> dict:
    """Whole lots for both legs at ~1R of risk, and the hedge mismatch they leave."""
    one_r = rcfg.one_r_rupees()
    uy, ux = unit(pair["y"]), unit(pair["x"])
    if not uy or not ux:
        return {"ok": True, "note": "lot size unknown", "lots_y": None, "lots_x": None, "notional_y": one_r / risk_e}
    target_y = one_r / risk_e                                  # ₹ notional of the y leg
    lots_y = max(1, round(target_y / (py * uy)))
    lots_x = max(1, round(pair["b"] * lots_y * py * uy / (px * ux)))
    hedge = lots_x * px * ux / (lots_y * py * uy)
    imbalance = abs(hedge - pair["b"]) / pair["b"] * 100
    out = {"lots_y": lots_y, "lots_x": lots_x, "notional_y": lots_y * py * uy, "imbalance_pct": round(imbalance, 1),
           "ok": imbalance <= float(p.get("max_lot_imbalance_pct", 15))}
    if not out["ok"]:
        out["note"] = f"whole lots leave a {imbalance:.0f}% hedge mismatch"
    return out


def simulate(series: dict[str, dict[str, float]], p: dict, start: str | None = None, end: str | None = None,
             eligible: Callable[[str, str], bool] = lambda s, d: True,
             force_pairs: list[tuple[str, str]] | None = None,
             unit: Callable[[str], float | None] = lambda s: None,
             cost_kind: str = "stock_futures") -> tuple[list[dict], dict]:
    dates = sorted({d for c in series.values() for d in c})
    entry_q, exit_q, stop_q = float(p["entry_q"]), float(p["exit_q"]), float(p["stop_q"])
    max_hold = int(p.get("max_hold_days", 30))
    cost = rcfg.cost_bps(cost_kind) / 1e4
    trades: list[dict] = []
    funnel = {"formations": 0, "pairs_tested": 0, "pairs_passed": 0, "pairs_chosen": 0, "signals": 0,
              "skipped_lots": 0, "skipped_beyond_stop": 0}
    formations: list[dict] = []
    active: list[dict] = []
    open_pos: dict[tuple[str, str], dict] = {}
    pending: list[tuple[str, tuple[str, str], dict]] = []     # (action, pair key, info) for the next close
    month = None
    for i, d in enumerate(dates):
        if end and d > end and not open_pos and not pending:
            break
        # 1. fills decided yesterday, at today's close
        todo, pending = pending, []
        for action, key, info in todo:
            pair = info["pair"]
            py, px = series[pair["y"]].get(d), series[pair["x"]].get(d)
            if py is None or px is None:
                pending.append((action, key, info))           # no close today: try tomorrow
                continue
            e = math.log(py) - pair["a"] - pair["b"] * math.log(px)
            q = (e - pair["median"]) / pair["iqr"]
            if action == "enter":
                side = -1 if info["q_signal"] > 0 else 1          # +1 = long the spread
                if abs(q) >= stop_q or (q > 0) != (info["q_signal"] > 0):
                    funnel["skipped_beyond_stop"] += 1
                    continue
                risk_e = (stop_q - abs(q)) * pair["iqr"]
                lots = _lots(pair, px, py, risk_e, p, unit)
                if not lots["ok"]:
                    funnel["skipped_lots"] += 1
                    continue
                open_pos[key] = {"pair": pair, "side": side, "entry_date": d, "e0": e, "q0": q, "risk_e": risk_e,
                                 "px0": px, "py0": py, "lots": lots, "days": 0, "best": 0.0, "worst": 0.0}
            elif action == "exit" and key in open_pos:
                pos = open_pos.pop(key)
                trades.append(_close(pos, d, e, q, px, py, info["reason"], cost))
        # 2. monthly formation
        if month != d[:7] and (not start or d >= start or open_pos) and (not end or d <= end):
            month = d[:7]
            form = _form(i, dates, series, p, eligible, force_pairs)
            funnel["formations"] += 1
            funnel["pairs_tested"] += form["tested"]
            funnel["pairs_passed"] += form["passed"]
            funnel["pairs_chosen"] += len(form["pairs"])
            formations.append({"month": month, "pairs": [f"{c['y']}/{c['x']}" for c in form["pairs"]],
                               "tested": form["tested"], "passed": form["passed"], "reason": form.get("reason")})
            keys = {(c["y"], c["x"]) for c in form["pairs"]}
            for key, pos in list(open_pos.items()):
                if key not in keys:
                    pending.append(("exit", key, {"pair": pos["pair"], "reason": "pair_dropped"}))
            kept = {k: pos["pair"] for k, pos in open_pos.items()}
            active = [kept.get((c["y"], c["x"]), c) for c in form["pairs"]]
        # 3. signals on today's close
        for pair in active:
            key = (pair["y"], pair["x"])
            py, px = series[pair["y"]].get(d), series[pair["x"]].get(d)
            if py is None or px is None or any(k == key for _, k, _ in pending):
                continue
            q = (math.log(py) - pair["a"] - pair["b"] * math.log(px) - pair["median"]) / pair["iqr"]
            if key in open_pos:
                pos = open_pos[key]
                pos["days"] += 1
                move = pos["side"] * ((math.log(py) - pair["a"] - pair["b"] * math.log(px)) - pos["e0"]) / pos["risk_e"]
                pos["best"], pos["worst"] = max(pos["best"], move), min(pos["worst"], move)
                reason = None
                if abs(q) >= stop_q and (q > 0) == (pos["q0"] > 0):
                    reason = "stop"
                elif abs(q) <= exit_q or (q > 0) != (pos["q0"] > 0):
                    reason = "converged"
                elif pos["days"] >= max_hold:
                    reason = "time"
                if reason:
                    pending.append(("exit", key, {"pair": pair, "reason": reason}))
            elif abs(q) >= entry_q and abs(q) < stop_q and (not start or d >= start) and (not end or d < end):
                funnel["signals"] += 1
                pending.append(("enter", key, {"pair": pair, "q_signal": q}))
    for pos in open_pos.values():
        pair = pos["pair"]
        last_y = max(series[pair["y"]])
        last_x = max(series[pair["x"]])
        py, px = series[pair["y"]][last_y], series[pair["x"]][last_x]
        e = math.log(py) - pair["a"] - pair["b"] * math.log(px)
        q_now = (e - pair["median"]) / pair["iqr"]
        t = _close(pos, min(last_x, last_y), e, q_now, px, py, None, cost)
        t.update(status="open", exit_date=None, exit_price=None, exit_reason=None)
        t["extra"].update(last_date=min(last_x, last_y), q_now=round(q_now, 2))
        trades.append(t)
    return trades, {"funnel": funnel, "formations": formations[-24:]}


def _close(pos: dict, d: str, e: float, q: float, px: float, py: float, reason: str | None, cost: float) -> dict:
    pair = pos["pair"]
    gross = pos["side"] * (e - pos["e0"]) / pos["risk_e"]
    cost_r = 2 * cost * (1 + pair["b"]) / pos["risk_e"]
    pnl_r = gross - cost_r
    lots = pos["lots"]
    notional = lots.get("notional_y") or 0
    long_y = pos["side"] > 0
    return {
        "key": f"{pair['y']}/{pair['x']}|{pos['entry_date']}",
        "symbol": f"{pair['y']}/{pair['x']}",
        "direction": "long" if long_y else "short",                 # long = long y, short x (the spread)
        "entry_date": pos["entry_date"], "entry_price": round(pos["q0"], 2), "stop": None,
        "exit_date": d, "exit_price": round(q, 2), "exit_reason": reason, "status": "closed",
        "pnl_r": round(pnl_r, 3), "pnl_rupees": round(pnl_r * notional * pos["risk_e"], 0) if notional else None,
        "lots": {"y": lots.get("lots_y"), "x": lots.get("lots_x")},
        "legs": [{"symbol": pair["y"], "side": "buy" if long_y else "sell", "lots": lots.get("lots_y"),
                  "entry": round(pos["py0"], 2), "exit": round(py, 2)},
                 {"symbol": pair["x"], "side": "sell" if long_y else "buy", "lots": lots.get("lots_x"),
                  "entry": round(pos["px0"], 2), "exit": round(px, 2)}],
        "extra": {"hedge_ratio": round(pair["b"], 3), "adf_t": round(pair["t"], 2),
                  "half_life": round(pair["half_life"], 1) if pair.get("half_life") else None,
                  "cointegrated": pair.get("cointegrated", True), "q_entry": round(pos["q0"], 2),
                  "days": pos["days"], "mfe_r": round(pos["best"], 2), "mae_r": round(pos["worst"], 2),
                  "cost_r": round(cost_r, 3), "imbalance_pct": lots.get("imbalance_pct"),
                  "note": "entry/exit 'price' columns show the q-score"},
    }


# --- data and entry points ----------------------------------------------------------------------

def _stock_series(p: dict) -> tuple[dict[str, dict[str, float]], Callable[[str, str], bool], dict[str, float | None]]:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe
    from backend.research.data import ensure_tables
    ensure_tables()
    series, turnover, units = {}, {}, {}
    for s in fno_universe():
        if not s["spot_token"]:
            continue
        bars = get_candles(s["spot_token"], "day", limit=int(p["formation_days"]) + 1200)
        if len(bars) < int(p["formation_days"]) * 0.9:
            continue
        series[s["symbol"]] = _closes(bars)
        units[s["symbol"]] = float(s["lot_size"]) if s.get("lot_size") else None
        turnover[s["symbol"]] = {b["ts"][:10]: b["close"] * (b.get("volume") or 0) for b in bars}
    floor = float(p.get("min_turnover_cr", 100)) * 1e7
    limit = int(p.get("universe_limit", 120))
    cache: dict[str, set[str]] = {}

    def eligible(sym: str, d: str) -> bool:
        if d not in cache:
            avg = {}
            for name, tv in turnover.items():
                vals = [v for k, v in tv.items() if k < d][-20:]
                if len(vals) >= 15:
                    avg[name] = sum(vals) / len(vals)
            cache[d] = set(sorted((n for n, v in avg.items() if v >= floor), key=lambda n: -avg[n])[:limit])
        return sym in cache[d]
    return series, eligible, units


def _run(key: str, p: dict, start: str | None, end: str | None) -> tuple[list[dict], dict]:
    if key == "pairs_metals":
        legs = p["legs"]
        series = {name: _closes(daily_bars(name)) for name in legs}
        units = {name: point_value(name)[0] for name in legs}
        return simulate(series, p, start, end, force_pairs=[(legs[0], legs[1])],
                        unit=lambda s: units.get(s), cost_kind="mcx")
    series, eligible, units = _stock_series(p)
    if p.get("same_sector_only"):
        from backend.concentration import get_sector_for_ticker
        return simulate_by_sector(series, p, start, end, eligible, units, get_sector_for_ticker)
    return simulate(series, p, start, end, eligible=eligible, unit=lambda s: units.get(s))


def simulate_by_sector(series, p, start, end, eligible, units, sector_of):
    """Same-sector variant: the matching only sees pairs inside one sector."""
    groups: dict[str, dict] = {}
    for s, c in series.items():
        groups.setdefault(sector_of(s), {})[s] = c
    trades, funnel = [], {}
    for sector, ser in groups.items():
        if sector == "Other" or len(ser) < 2:
            continue
        t, extra = simulate(ser, {**p, "max_pairs": max(1, int(p.get("max_pairs", 10)) // 3)}, start, end,
                            eligible=eligible, unit=lambda s: units.get(s))
        trades += t
        for k, v in extra["funnel"].items():
            funnel[k] = funnel.get(k, 0) + v
    return trades, {"funnel": funnel, "formations": []}


def live(key: str, today: date | None = None) -> dict:
    from backend.research import journal
    p = rcfg.strategy(key)
    trades, extra = _run(key, p, _live_from(), None)
    synced = journal.sync(key, trades, rcfg.live_start())
    return {"status": "ok", **synced, "pairs_now": (extra.get("formations") or [{}])[-1].get("pairs")}


def _live_from() -> str:
    """Formations from about a month before the research start (the journal keeps entries from it)."""
    from datetime import timedelta
    return (date.fromisoformat(rcfg.live_start()) - timedelta(days=35)).isoformat()


def status(key: str) -> dict:
    p = rcfg.strategy(key)
    trades, extra = _run(key, p, _live_from(), None)
    last = (extra.get("formations") or [{}])[-1]
    states = {}
    for t in trades:
        if t["status"] == "open":
            states[t["symbol"]] = {"signal": t["direction"], "since": t["entry_date"], "q_entry": t["extra"]["q_entry"],
                                   "q_now": t["extra"].get("q_now"), "R_now": t["pnl_r"]}
    return {"settings": p, "formation": last, "funnel": extra.get("funnel"), "states": states}


def backtest(key: str, params: dict, progress=None) -> dict:
    base = {**rcfg.strategy(key), **(params.get("overrides") or {})}
    trades, extra = _run(key, base, params.get("start_date"), params.get("end_date"))
    return {"trades": trades, "extra": extra, "settings": base}


def entry(key: str) -> dict:
    return {"key": key, "page": "pairs", "kind": "trades", "live_after": "mcx" if key == "pairs_metals" else "nse",
            "live": lambda today=None: live(key, today),
            "backtest": lambda params, progress=None: backtest(key, params, progress),
            "status": lambda: status(key),
            "limitations": LIMITS_METALS if key == "pairs_metals" else LIMITS_STOCKS}
