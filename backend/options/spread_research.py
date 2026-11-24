"""Option-spread research: which strategy, where, with which exit — and does it hold up unseen?

Asked 24 Nov 2026. Built on the spread backtester (``spreads.backtest``) and the stored EOD chains.

* **Entries once, exits many.** Entries come from backtest runs; every exit rule is then re-played
  on the *same* entries (``simulate_exit``), so a change in results is the exit rule's doing.
* **Costs.** Every trade carries slippage per leg and side, brokerage, STT, exchange charges, GST
  and stamp duty (``costs``, rates in ``option_spreads.research.costs`` of the YAML) — R and ₹ are
  after costs unless ``costs=False``.
* **Metrics** (``metrics``): expectancy (R and ₹), win rate, average winner / loser and their
  ratio, profit factor, longest losing streak, maximum drawdown and how long it lasted, consistency
  (share of quarters with a positive result) and Sharpe / Sortino on *daily portfolio returns*:
  every open spread is marked to its closing value each trading day, the day's ₹ change ÷ capital is
  that day's return, annualised with √252. A trade-level Sharpe is kept only as ``sharpe_per_trade``.
* **Experiment A — exits** (entries = the live rules): every exit rule in ``exit_grid`` on the
  same entries; chosen on the train period, judged on the test period.
* **Experiment B — strategy per regime** (exits = the live rules): every strategy the regime allows,
  each with its own book, per index × regime × strategy "cell". A cell is *eligible* when its train
  trades pass the minimums (sample size, expectancy > 0, profit factor, consistency); the best
  eligible cell by ``score`` is chosen per index × regime, else no trade.
* **Experiment C — combined, unseen:** B's choice of strategies with A's chosen exit, judged on the
  test period, plus a walk-forward: for each quarter the choice is made again on all data before it
  and judged on that quarter only. The joined quarters give the out-of-sample profit factor.

Limitations (listed in every run): closing prices only (a stop can be passed during the day),
each strategy keeps its own book in B and C (the live book takes one spread per index at a time),
costs are a model, not fills.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import date, datetime, timedelta

from backend.db import get_db

log = logging.getLogger(__name__)

TRADING_DAYS = 252

DEFAULTS = {
    "min_trades": 10,             # a cell needs this many train trades to be eligible
    "min_profit_factor": 1.1,
    "min_consistency": 0.5,       # share of quarters with a positive total
    "shrink_trades": 20,          # expectancy is shrunk by n / (n + shrink_trades) before ranking
    "drawdown_weight": 0.5,       # score penalty per R of max drawdown per trade
    "exit_grid": {                # Experiment A (credit spreads; debit spreads keep their rule)
        "take_profit_pct": [30, 50, 70, None],
        "stop_multiple": [1.5, 2.0, 2.5, None],
        "trail": [None, {"start_pct": 50, "lock_pct": 25}],
        "partial": [None, {"at_pct": 30, "fraction": 0.5}],
    },
    "costs": {
        "slippage_points_per_leg": {"NIFTY": 0.5, "BANKNIFTY": 1.0, "SENSEX": 1.0},   # each side
        "brokerage_per_order": 20.0,
        "stt_sell_pct": [{"from": "2024-10-01", "pct": 0.1}, {"from": "2026-04-01", "pct": 0.15}],
        "stt_sell_pct_before": 0.0625,
        "exchange_pct": {"NSE": 0.03503, "BSE": 0.0325},
        "gst_pct": 18.0,
        "stamp_buy_pct": 0.003,
        "sebi_per_crore": 10.0,
    },
}


def settings() -> dict:
    from backend.options.spreads import settings as spread_settings
    cfg = spread_settings()
    extra = cfg.get("research") or {}
    out = {**DEFAULTS, **{k: v for k, v in extra.items() if k not in ("costs", "exit_grid")}}
    out["costs"] = {**DEFAULTS["costs"], **(extra.get("costs") or {})}
    out["exit_grid"] = {**DEFAULTS["exit_grid"], **(extra.get("exit_grid") or {})}
    return out


# --- costs ---------------------------------------------------------------------------------------

def _stt_pct(day: str, c: dict) -> float:
    pct = c["stt_sell_pct_before"]
    for step in sorted(c["stt_sell_pct"], key=lambda s: s["from"]):
        if day >= step["from"]:
            pct = step["pct"]
    return pct


def cost_points(trade: dict, exit_prices: dict | None, c: dict) -> float:
    """Round-trip costs of one spread, in index points per unit (multiply by lot size × lots for ₹).
    ``exit_prices`` = {(strike, type): price} on the exit day (None: no exit trade, e.g. expired)."""
    legs = trade["legs"]
    units = max(1, trade["lot_size"] * trade["lots"])
    exchange = "BSE" if trade["symbol"] == "SENSEX" else "NSE"
    slip = c["slippage_points_per_leg"].get(trade["symbol"], 1.0)
    sells = buys = 0.0
    for leg in legs:
        entry = leg["price"]
        exit_ = (exit_prices or {}).get((leg["strike"], leg["type"]))
        if leg["side"] == "short":
            sells += entry
            buys += exit_ or 0.0
        else:
            buys += entry
            sells += exit_ or 0.0
    sides = 2 if exit_prices else 1
    orders = len(legs) * sides
    brokerage = c["brokerage_per_order"] * orders / units
    turnover = sells + buys
    exchange_fee = c["exchange_pct"][exchange] / 100 * turnover
    sebi = c["sebi_per_crore"] / 1e7 * turnover
    gst = c["gst_pct"] / 100 * (brokerage + exchange_fee + sebi)
    stt = _stt_pct(trade["signal_date"], c) / 100 * sells
    stamp = c["stamp_buy_pct"] / 100 * buys
    return round(slip * len(legs) * sides + brokerage + exchange_fee + sebi + gst + stt + stamp, 3)


# --- re-playing exits on fixed entries ----------------------------------------------------------

class Chains:
    """Cached chain prices: {(symbol, day, expiry): {(strike, type): price}}."""

    def __init__(self):
        self.cache: dict = {}

    def prices(self, symbol: str, day: str, expiry: str) -> dict:
        key = (symbol, day, expiry)
        if key not in self.cache:
            from backend.options import store
            from backend.options.spreads import _prices
            self.cache[key] = _prices(store.chain(symbol, day, expiry))
        return self.cache[key]


def _trading_days(start: str, end: str) -> list[str]:
    from tradingagents.utils.market_calendar import is_trading_day
    d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= last:
        if is_trading_day(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def baseline_r_points(entry: dict, scfg: dict) -> float:
    """The live rules' 1R for this entry — the yardstick for every exit rule, so R stays comparable."""
    from backend.options.spreads import one_r_points
    return one_r_points(entry, scfg)


def live_rule(scfg: dict) -> dict:
    return {"take_profit_pct": scfg["take_profit_pct"], "stop_multiple": scfg["stop_multiple"],
            "trail": None, "partial": None}


def rule_name(rule: dict) -> str:
    parts = [f"TP {rule['take_profit_pct']}%" if rule.get("take_profit_pct") else "no TP",
             f"stop {rule['stop_multiple']:g}×" if rule.get("stop_multiple") else "no stop"]
    if rule.get("trail"):
        parts.append(f"trail from {rule['trail']['start_pct']}% lock {rule['trail']['lock_pct']}%")
    if rule.get("partial"):
        parts.append(f"half off at {rule['partial']['at_pct']}%")
    return " · ".join(parts)


def simulate_exit(entry: dict, rule: dict | None, scfg: dict, rcfg: dict, chains: Chains,
                  with_costs: bool = True) -> dict:
    """Follow one entry with an exit rule on daily closes. ``rule`` None = the live rules (debit
    spreads always keep their own rule). Returns the trade with pnl in baseline R and ₹, and
    ``marks`` = [(day, cumulative ₹)] for the daily portfolio curve."""
    from backend.options.spreads import is_debit, value
    debit = is_debit(entry["strategy"])
    rule = rule or live_rule(scfg)
    one_r = baseline_r_points(entry, scfg)
    mult = entry["lot_size"] * entry["lots"]
    credit = entry["credit"]
    days = [d for d in _trading_days(entry["signal_date"], entry["exit_by"]) if d > entry["signal_date"]]
    marks, banked, open_frac = [], 0.0, 1.0       # banked = points already taken off (partial)
    best = 0.0
    exit_day = exit_val = reason = None
    exit_prices = None
    for day in days:
        prices = chains.prices(entry["symbol"], day, entry["expiry"])
        val = value(entry["legs"], prices, debit)
        if val is None:
            continue
        pts = (val - credit) if debit else (credit - val)     # open profit per unit
        marks.append((day, (banked + open_frac * pts) * mult))
        if debit:
            d = scfg["debit"]
            target = min(credit * (1 + d["take_profit_pct"] / 100), 0.9 * entry["width"])
            hit = "take_profit" if val >= target else "stop" if val <= credit * (1 - d["stop_pct"] / 100) else None
        else:
            best = max(best, pts)
            tp, sm = rule.get("take_profit_pct"), rule.get("stop_multiple")
            part, trail = rule.get("partial"), rule.get("trail")
            if part and open_frac == 1.0 and pts >= credit * part["at_pct"] / 100:
                banked += part["fraction"] * pts
                open_frac = 1.0 - part["fraction"]
            hit = None
            if tp and pts >= credit * tp / 100:
                hit = "take_profit"
            elif sm and val >= credit * sm:
                hit = "stop"
            elif trail and best >= credit * trail["start_pct"] / 100 and pts <= credit * trail["lock_pct"] / 100:
                hit = "trail_stop"
        if hit or day >= entry["exit_by"]:
            exit_day, exit_val, reason, exit_prices = day, val, hit or "expiry_exit", prices
            break
    if exit_day is None:
        return {**entry, "status": "open", "marks": marks}
    pts = (exit_val - credit) if debit else (credit - exit_val)
    gross = banked + open_frac * pts
    cost = cost_points(entry, exit_prices, rcfg["costs"]) if with_costs else 0.0
    net = gross - cost
    if marks:
        marks[-1] = (exit_day, net * mult)
    return {**entry, "status": "closed", "exit_date": exit_day, "exit_value": round(exit_val, 2),
            "exit_reason": reason, "pnl_points_gross": round(gross, 2), "cost_points": cost,
            "pnl_points": round(net, 2), "pnl_r": round(net / one_r, 3) if one_r else 0.0,
            "pnl_inr": round(net * mult, 0), "marks": marks}


# --- metrics -------------------------------------------------------------------------------------

def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def daily_returns(trades: list[dict], start: str, end: str, capital: float) -> list[tuple[str, float]]:
    """Daily portfolio returns: each day's change in marked-to-market ₹ of all spreads ÷ capital."""
    change: dict[str, float] = {}
    for t in trades:
        prev = 0.0
        for day, cum in t.get("marks") or []:
            change[day] = change.get(day, 0.0) + (cum - prev)
            prev = cum
    return [(d, change.get(d, 0.0) / capital) for d in _trading_days(start, end)]


def metrics(trades: list[dict], start: str, end: str, capital: float) -> dict:
    closed = sorted((t for t in trades if t.get("status", "closed") == "closed"), key=lambda t: t["exit_date"])
    n = len(closed)
    if not n:
        return {"trades": 0}
    rs = [t["pnl_r"] for t in closed]
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    streak = longest = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        longest = max(longest, streak)
    # drawdown on the daily marked-to-market equity (₹), with its length in calendar days
    rets = daily_returns(closed, start, end, capital)
    eq = peak = 0.0
    peak_day = rets[0][0] if rets else start
    max_dd = 0.0
    dd_days = longest_dd_days = 0
    for day, r in rets:
        eq += r * capital
        if eq >= peak:
            peak, peak_day = eq, day
        else:
            dd_days = (date.fromisoformat(day) - date.fromisoformat(peak_day)).days
            longest_dd_days = max(longest_dd_days, dd_days)
        max_dd = max(max_dd, peak - eq)
    daily = [r for _, r in rets]
    mean_d = sum(daily) / len(daily) if daily else 0.0
    sd_d = _std(daily)
    down = math.sqrt(sum(min(0.0, r) ** 2 for r in daily) / len(daily)) if daily else 0.0
    # R drawdown in exit order (for comparison with other pages)
    e = p = dd_r = 0.0
    for r in rs:
        e += r
        p = max(p, e)
        dd_r = max(dd_r, p - e)
    quarters: dict[str, float] = {}
    for t in closed:
        q = f"{t['exit_date'][:4]}Q{(int(t['exit_date'][5:7]) - 1) // 3 + 1}"
        quarters[q] = quarters.get(q, 0.0) + t["pnl_r"]
    years = max(len(daily) / TRADING_DAYS, 1e-9)
    total_inr = sum(t["pnl_inr"] for t in closed)
    avg_w = sum(wins) / len(wins) if wins else None
    avg_l = sum(losses) / len(losses) if losses else None
    return {
        "trades": n, "win_rate": round(100 * len(wins) / n, 1),
        "expectancy_r": round(sum(rs) / n, 3), "expectancy_inr": round(total_inr / n, 0),
        "total_r": round(sum(rs), 2), "total_inr": round(total_inr, 0),
        "avg_win_r": round(avg_w, 3) if avg_w is not None else None,
        "avg_loss_r": round(avg_l, 3) if avg_l is not None else None,
        "win_loss_ratio": round(avg_w / -avg_l, 2) if avg_w and avg_l and avg_l < 0 else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_losing_streak": longest, "max_drawdown_r": round(dd_r, 2),
        "max_drawdown_inr": round(max_dd, 0), "max_drawdown_days": longest_dd_days,
        "consistency": round(sum(1 for v in quarters.values() if v > 0) / len(quarters), 2) if quarters else None,
        "quarters": len(quarters),
        "sharpe_daily_annual": round(mean_d / sd_d * math.sqrt(TRADING_DAYS), 2) if sd_d > 0 else None,
        "sortino_daily_annual": round(mean_d / down * math.sqrt(TRADING_DAYS), 2) if down > 0 else None,
        "return_pct_annual": round(100 * mean_d * TRADING_DAYS, 2),
        "sharpe_per_trade": round((sum(rs) / n) / _std(rs), 3) if _std(rs) > 0 else None,
        "years": round(years, 2),
    }


def score(m: dict, rcfg: dict) -> float | None:
    """Ranking score: expectancy shrunk towards 0 for small samples, scaled by consistency, minus a
    drawdown-per-trade penalty. Higher is better."""
    n = m.get("trades") or 0
    if not n:
        return None
    shrunk = m["expectancy_r"] * n / (n + rcfg["shrink_trades"])
    cons = m.get("consistency") or 0.0
    return round(shrunk * (0.5 + 0.5 * cons) - rcfg["drawdown_weight"] * m["max_drawdown_r"] / n, 4)


def eligible(m: dict, rcfg: dict) -> tuple[bool, str]:
    if (m.get("trades") or 0) < rcfg["min_trades"]:
        return False, f"{m.get('trades') or 0} trades < {rcfg['min_trades']}"
    if m["expectancy_r"] <= 0:
        return False, "expectancy ≤ 0"
    if (m.get("profit_factor") or 0) < rcfg["min_profit_factor"]:
        return False, f"profit factor < {rcfg['min_profit_factor']}"
    if (m.get("consistency") or 0) < rcfg["min_consistency"]:
        return False, f"consistency < {rcfg['min_consistency']}"
    return True, "eligible"


# --- the experiments -----------------------------------------------------------------------------

def _grid(rcfg: dict) -> list[dict]:
    g = rcfg["exit_grid"]
    out = []
    for tp in g["take_profit_pct"]:
        for sm in g["stop_multiple"]:
            for tr in g["trail"]:
                for pa in g["partial"]:
                    if not tp and not sm and not tr:
                        continue                          # nothing but expiry: not a managed exit
                    out.append({"take_profit_pct": tp, "stop_multiple": sm, "trail": tr, "partial": pa})
    return out


def _entries(result: dict) -> list[dict]:
    keep = ("symbol", "signal_date", "expiry", "strategy", "legs", "credit", "width", "lot_size", "lots",
            "regime", "vix", "exit_by")
    return [{k: t[k] for k in keep} for t in result["trades"]]


def _split(trades: list[dict], cut: str, key: str = "signal_date") -> tuple[list[dict], list[dict]]:
    return [t for t in trades if t[key] < cut], [t for t in trades if t[key] >= cut]


def _choose_cells(trades: list[dict], start: str, end: str, capital: float, rcfg: dict) -> dict:
    """{(symbol, regime): strategy} from the eligible cell with the best score (or none)."""
    groups: dict = {}
    for t in trades:
        groups.setdefault((t["symbol"], t["regime"], t["strategy"]), []).append(t)
    best: dict = {}
    for (sym, reg, strat), rows in groups.items():
        m = metrics(rows, start, end, capital)
        ok, _ = eligible(m, rcfg)
        if ok:
            s = score(m, rcfg)
            if (sym, reg) not in best or s > best[(sym, reg)][1]:
                best[(sym, reg)] = (strat, s)
    return {k: v[0] for k, v in best.items()}


def _choose_exit(entries: list[dict], rules: list[dict], start: str, end: str, capital: float,
                 scfg: dict, rcfg: dict, chains: Chains, costs: bool) -> tuple[dict, list]:
    scored = []
    for rule in rules:
        trades = [simulate_exit(e, rule, scfg, rcfg, chains, costs) for e in entries]
        m = metrics(trades, start, end, capital)
        scored.append((score(m, rcfg) if m.get("trades") else None, rule, m))
    ranked = sorted(scored, key=lambda x: (x[0] is None, -(x[0] or 0)))
    return ranked[0][1], ranked


def _quarter_starts(start: str, end: str) -> list[str]:
    d = date.fromisoformat(start)
    q = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    out = []
    while q.isoformat() <= end:
        out.append(q.isoformat())
        q = date(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1)
    return out


def run(start: date, end: date, split_date: date, symbols: list[str] | None = None,
        min_credit_ratio_single: float | None = None, costs: bool = True, progress=None) -> dict:
    from backend.options import spreads
    say = progress or (lambda *_: None)
    scfg, rcfg = spreads.settings(), settings()
    if min_credit_ratio_single is not None:
        scfg = {**scfg, "min_credit_ratio_single": min_credit_ratio_single}
    capital = float(scfg["capital"])
    s, e, cut = start.isoformat(), end.isoformat(), split_date.isoformat()
    chains = Chains()
    live = live_rule(scfg)

    say("live rules: entries")
    base = spreads.backtest(start, end, symbols, split_date, cfg=scfg)
    base_entries = _entries(base)
    say("all strategies per regime: entries")
    wide = spreads.backtest(start, end, symbols, split_date, cfg=scfg,
                            strategies=list(spreads.STRATEGIES), condor_in_unstable=True)
    wide_entries = _entries(wide)

    # baseline with costs and daily marks
    say("baseline")
    base_trades = [simulate_exit(x, live, scfg, rcfg, chains, costs) for x in base_entries]
    btr, bte = _split(base_trades, cut)
    baseline = {"all": metrics(base_trades, s, e, capital), "train": metrics(btr, s, cut, capital),
                "test": metrics(bte, cut, e, capital)}

    # Experiment A — exits on the live rules' entries
    say("experiment A: exit rules")
    rules = _grid(rcfg)
    a_train, a_test = _split(base_entries, cut)
    chosen_exit, ranked = _choose_exit(a_train, rules, s, cut, capital, scfg, rcfg, chains, costs)
    a_rows = []
    for sc, rule, m_train in ranked:
        test_trades = [simulate_exit(x, rule, scfg, rcfg, chains, costs) for x in a_test]
        a_rows.append({"rule": rule_name(rule), "is_live": rule == live, "score_train": sc,
                       "train": m_train, "test": metrics(test_trades, cut, e, capital)})

    # Experiment B — strategy per regime, live exits
    say("experiment B: strategy per regime")
    wide_trades = [simulate_exit(x, None, scfg, rcfg, chains, costs) for x in wide_entries]
    wtr, wte = _split(wide_trades, cut)
    cells = []
    keys = sorted({(t["symbol"], t["regime"], t["strategy"]) for t in wide_trades})
    for sym, reg, strat in keys:
        tr = [t for t in wtr if (t["symbol"], t["regime"], t["strategy"]) == (sym, reg, strat)]
        te = [t for t in wte if (t["symbol"], t["regime"], t["strategy"]) == (sym, reg, strat)]
        m_tr, m_te = metrics(tr, s, cut, capital), metrics(te, cut, e, capital)
        ok, why = eligible(m_tr, rcfg) if m_tr.get("trades") else (False, "no train trades")
        cells.append({"symbol": sym, "regime": reg, "strategy": strat, "eligible": ok, "why": why,
                      "score_train": score(m_tr, rcfg) if m_tr.get("trades") else None,
                      "train": m_tr, "test": m_te})
    policy = _choose_cells(wtr, s, cut, capital, rcfg)
    b_test = [t for t in wte if policy.get((t["symbol"], t["regime"])) == t["strategy"]]
    exp_b = {"policy": [{"symbol": k[0], "regime": k[1], "strategy": v} for k, v in sorted(policy.items())],
             "cells": cells, "test": metrics(b_test, cut, e, capital)}

    # Experiment C — B's policy + A's exit on unseen data, and a quarterly walk-forward
    say("experiment C: combined, unseen")
    c_entries = [x for x in wide_entries if x["signal_date"] >= cut
                 and policy.get((x["symbol"], x["regime"])) == x["strategy"]]
    c_trades = [simulate_exit(x, chosen_exit, scfg, rcfg, chains, costs) for x in c_entries]
    folds, wf_trades = [], []
    qs = _quarter_starts(s, e)
    for i, q in enumerate(qs):
        if i < 2:                                    # at least two quarters to learn from
            continue
        q_end = (date.fromisoformat(qs[i + 1]) - timedelta(days=1)).isoformat() if i + 1 < len(qs) else e
        train_w = [t for t in wide_trades if t["signal_date"] < q]
        pol = _choose_cells(train_w, s, q, capital, rcfg)
        ex, _ = _choose_exit([x for x in base_entries if x["signal_date"] < q], rules, s, q, capital,
                             scfg, rcfg, chains, costs)
        fold_entries = [x for x in wide_entries if q <= x["signal_date"] <= q_end
                        and pol.get((x["symbol"], x["regime"])) == x["strategy"]]
        ft = [simulate_exit(x, ex, scfg, rcfg, chains, costs) for x in fold_entries]
        wf_trades += ft
        folds.append({"quarter": q[:7], "cells_chosen": len(pol), "exit": rule_name(ex),
                      "result": metrics(ft, q, q_end, capital)})
    exp_c = {"exit": rule_name(chosen_exit), "test": metrics(c_trades, cut, e, capital),
             "walk_forward": {"folds": folds, "all": metrics(wf_trades, qs[2] if len(qs) > 2 else s, e, capital)}}

    def lean(trades):
        return [{k: v for k, v in t.items() if k not in ("marks", "legs")} | {"legs": _legs(t)} for t in trades]

    return {
        "status": "ok", "range": {"start": s, "end": e, "split": cut}, "costs_included": costs,
        "settings": {"live_exit": rule_name(live), "ranking": {k: rcfg[k] for k in (
            "min_trades", "min_profit_factor", "min_consistency", "shrink_trades", "drawdown_weight")},
            "costs": rcfg["costs"], "min_credit_ratio_single": scfg.get("min_credit_ratio_single"),
            "debit_below_vix": scfg.get("debit_below_vix")},
        "funnel": base.get("funnel"),
        "baseline": baseline, "experiment_a": {"chosen_on_train": rule_name(chosen_exit), "rules": a_rows},
        "experiment_b": exp_b, "experiment_c": exp_c,
        "trades": {"baseline": lean(base_trades), "all_strategies": lean(wide_trades)},
        "limitations": [
            "Exits are checked on daily closes only: a stop can be passed during the day.",
            "Costs are a model (slippage per leg, brokerage, STT, exchange, GST, stamp) — check the rates.",
            "Experiments B and C give each strategy its own book; the live book takes one spread per index.",
            "Exit rules are tried on credit spreads; debit spreads keep their own rule.",
            "R is measured against the live rules' 1R for every exit rule, so rows are comparable.",
            "Only days with stored option files are replayed; SENSEX uses the NIFTY regime."],
    }


def _legs(t: dict) -> str:
    return " ".join(f"{'S' if l['side'] == 'short' else 'B'}{l['strike']:g}{l['type']}@{l['price']:.1f}"
                    for l in t.get("legs") or [])


# --- background runs -----------------------------------------------------------------------------

_lock = threading.Lock()
_state: dict = {"running": False}


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS spread_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT (datetime('now', 'localtime')),
            status TEXT, params TEXT, result TEXT, error TEXT)""")
        if not _state.get("running"):          # a run left 'running' by a restart can't finish
            conn.execute("UPDATE spread_research SET status = 'interrupted' WHERE status = 'running'")


def start(params: dict) -> dict:
    ensure_table()
    if not _lock.acquire(blocking=False):
        return {"status": "busy", "progress": _state.get("progress")}
    _state.update(running=True, progress="starting", started=datetime.now().isoformat(timespec="seconds"))
    with get_db() as conn:
        run_id = conn.execute("INSERT INTO spread_research (status, params) VALUES ('running', ?)",
                              (json.dumps(params, default=str),)).lastrowid
    _state["id"] = run_id

    def work():
        try:
            res = run(date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"]),
                      date.fromisoformat(params["split_date"]), params.get("symbols"),
                      params.get("min_credit_ratio_single"), params.get("costs", True),
                      progress=lambda msg: _state.update(progress=msg))
            with get_db() as conn:
                conn.execute("UPDATE spread_research SET status = 'ok', result = ? WHERE id = ?",
                             (json.dumps(res, default=str), run_id))
        except Exception as exc:     # noqa: BLE001
            log.exception("spread research failed")
            with get_db() as conn:
                conn.execute("UPDATE spread_research SET status = 'error', error = ? WHERE id = ?",
                             (f"{type(exc).__name__}: {exc}", run_id))
        finally:
            _state.update(running=False, progress=None)
            _lock.release()

    threading.Thread(target=work, name="spread-research", daemon=True).start()
    return {"status": "started", "id": run_id}


def status() -> dict:
    ensure_table()
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, created_at, status, params, error FROM spread_research ORDER BY id DESC LIMIT 20")]
    for r in rows:
        r["params"] = json.loads(r["params"] or "{}")
    return {"running": _state.get("running", False), "progress": _state.get("progress"), "runs": rows}


def get(run_id: int) -> dict | None:
    ensure_table()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM spread_research WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["params"] = json.loads(out["params"] or "{}")
    out["result"] = json.loads(out["result"]) if out["result"] else None
    return out


CSV_COLUMNS = ["symbol", "strategy", "regime", "vix", "signal_date", "expiry", "exit_by", "exit_date",
               "exit_reason", "legs", "credit", "width", "lot_size", "lots", "exit_value", "pnl_points_gross",
               "cost_points", "pnl_points", "pnl_r", "pnl_inr", "status"]


def trades_csv(run_id: int, which: str = "baseline") -> str | None:
    import csv
    import io
    rec = get(run_id)
    if not rec or not rec.get("result"):
        return None
    rows = rec["result"]["trades"].get(which, [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
