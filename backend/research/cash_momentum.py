"""Cash momentum (research, shadow) — Andreas Clenow's "Stocks on the Move", as quoted by
Berzal & García ("Beyond Trend Following", 2024).

Once a week (first session on/after ``rebalance_weekday``), on the cash universe (F&O + NIFTY 500):
  * score = annualised slope of an exponential regression over ``regression_days`` × its R²;
  * a stock qualifies when it is above its ``ma_filter_days`` SMA and had no gap over
    ``max_gap_pct`` in the regression window, with enough turnover;
  * holdings that drop out of the top ``top_pct`` % or stop qualifying are sold;
  * new buys only while NIFTY is above its ``index_ma_days`` SMA, best score first, while cash
    lasts: shares = capital × ``risk_factor_bps`` / 10,000 ÷ ATR(``atr_period``), capped at
    ``max_position_pct`` of capital.
Decisions on the rebalance day's close; orders at the next session's open. No stop-loss (as in
the book); every position is one research trade with R = ₹ P&L ÷ (1% of capital).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np

from backend.research import config as rcfg
from backend.research.trend import is_rebalance
from backend.signals.indicators import atr as wilder_atr

LIMITS = [
    "Universe = today's F&O + NIFTY 500 lists (survivorship): past members that left are missing.",
    "Weekly decisions on the close, fills at the next open; no intraday stops (as in the book).",
    "The book's twice-monthly position-size rebalance is not modelled; positions keep their entry size.",
    "Costs: research cost model for delivery (STT both sides + charges + slippage).",
]


def regression_score(closes: list[float]) -> float | None:
    """Annualised exponential-regression slope × R² (Clenow)."""
    if len(closes) < 10 or min(closes) <= 0:
        return None
    y = np.log(np.array(closes, dtype=float))
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fit = intercept + slope * x
    ss_res = float(((y - fit) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return (math.exp(slope * 250) - 1) * 100 * r2


def _universe_bars(warm_from: str | None) -> tuple[dict[str, list[dict]], list[dict]]:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.cash_universe import cash_universe
    from backend.research.data import daily_bars
    from backend.research.data import ensure_tables
    ensure_tables()
    stocks = {}
    for s in cash_universe():
        if not s.get("spot_token"):
            continue
        bars = get_candles(s["spot_token"], "day", limit=4000)
        if warm_from:
            bars = [b for b in bars if b["ts"][:10] >= warm_from]
        if len(bars) >= 120:
            stocks[s["symbol"]] = bars
    return stocks, daily_bars("NIFTY")


def simulate(stocks: dict[str, list[dict]], nifty: list[dict], p: dict,
             start: str | None = None, end: str | None = None) -> tuple[list[dict], dict]:
    capital = rcfg.capital()
    one_r = rcfg.one_r_rupees()
    cost = rcfg.cost_bps("cash") / 1e4
    reg, ma_n, idx_n = int(p["regression_days"]), int(p["ma_filter_days"]), int(p["index_ma_days"])
    gap_max = float(p["max_gap_pct"]) / 100
    min_tv = float(p.get("min_turnover_cr", 20)) * 1e7
    risk_cash = capital * float(p["risk_factor_bps"]) / 1e4
    cap_pos = capital * float(p["max_position_pct"]) / 100
    # per-stock lookups
    data = {}
    for sym, bars in stocks.items():
        dates = [b["ts"][:10] for b in bars]
        data[sym] = {"dates": dates, "pos": {d: i for i, d in enumerate(dates)}, "bars": bars,
                     "close": [b["close"] for b in bars], "open": [b["open"] for b in bars],
                     "atr": wilder_atr(bars, int(p["atr_period"]))}
    cal = [b["ts"][:10] for b in nifty]
    n_close = [b["close"] for b in nifty]
    if start:
        first = next((i for i, d in enumerate(cal) if d >= start), len(cal))
    else:
        first = int(p.get("warmup_bars", 210))
    cash = capital
    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    pending: list[tuple[str, str, int | None]] = []     # (action, symbol, shares) at next open
    equity_curve: list[tuple[str, float]] = []
    funnel = {"rebalances": 0, "index_filter_off": 0, "buys": 0, "sells": 0, "no_cash": 0}
    wd = int(p.get("rebalance_weekday", 2))
    buy_info: dict[str, dict] = {}

    def price(sym: str, d: str, field: str) -> float | None:
        i = data[sym]["pos"].get(d)
        return data[sym][field][i] if i is not None else None

    for i in range(max(first, idx_n), len(cal)):
        d = cal[i]
        # fills at today's open
        todo, pending = pending, []
        for action, sym, shares in todo:
            px = price(sym, d, "open")
            if px is None:
                pending.append((action, sym, shares))
                continue
            if action == "sell" and sym in holdings:
                h = holdings.pop(sym)
                gross = h["shares"] * (px - h["entry"])
                costs = h["shares"] * (px + h["entry"]) * cost
                cash += h["shares"] * px - h["shares"] * px * cost
                pnl = gross - costs
                funnel["sells"] += 1
                trades.append({"symbol": sym, "direction": "long", "entry_date": h["date"], "entry_price": round(h["entry"], 2),
                               "stop": None, "exit_date": d, "exit_price": round(px, 2), "exit_reason": h.get("why", "rebalance"),
                               "status": "closed", "pnl_r": round(pnl / one_r, 3), "pnl_rupees": round(pnl, 0),
                               "lots": h["shares"], "extra": {"score_at_entry": h["score"], "rank_at_entry": h["rank"],
                                                              "return_pct": round((px / h["entry"] - 1) * 100, 2)}})
            elif action == "buy" and sym not in holdings and shares:
                spend = shares * px * (1 + cost)
                if spend > cash:
                    shares = int(cash / (px * (1 + cost)))
                    spend = shares * px * (1 + cost)
                if shares <= 0:
                    funnel["no_cash"] += 1
                    continue
                cash -= spend
                info = buy_info.pop(sym, {})
                holdings[sym] = {"shares": shares, "entry": px, "date": d, **info}
                funnel["buys"] += 1
        # mark to market
        eq = cash + sum(h["shares"] * (price(s, d, "close") or h.get("last", h["entry"])) for s, h in holdings.items())
        for s, h in holdings.items():
            c = price(s, d, "close")
            if c is not None:
                h["last"] = c
        equity_curve.append((d, eq))
        if end and d > end:
            break
        if not is_rebalance(cal, i, wd) or i + 1 >= len(cal):
            continue
        funnel["rebalances"] += 1
        ranked = []
        qualifies = {}
        for sym, dd in data.items():
            j = dd["pos"].get(d)
            if j is None or j < max(reg, ma_n) or dd["atr"][j] is None:
                continue
            closes = dd["close"][j - reg + 1:j + 1]
            tv = sum(b["close"] * (b.get("volume") or 0) for b in dd["bars"][j - 19:j + 1]) / 20
            if tv < min_tv:
                continue
            score = regression_score(closes)
            if score is None:
                continue
            sma = sum(dd["close"][j - ma_n + 1:j + 1]) / ma_n
            gaps = max(abs(dd["open"][k] / dd["close"][k - 1] - 1) for k in range(j - reg + 2, j + 1))
            qualifies[sym] = dd["close"][j] > sma and gaps <= gap_max
            ranked.append((score, sym, dd["atr"][j]))
        ranked.sort(reverse=True)
        top_n = max(1, math.ceil(len(ranked) * float(p["top_pct"]) / 100))
        top = {sym for _, sym, _ in ranked[:top_n]}
        for sym, h in holdings.items():
            if sym not in top or not qualifies.get(sym, False):
                h["why"] = "left_top" if sym not in top else "below_ma_or_gap"
                pending.append(("sell", sym, None))
        idx_sma = sum(n_close[i - idx_n + 1:i + 1]) / idx_n
        if n_close[i] <= idx_sma:
            funnel["index_filter_off"] += 1
            continue
        selling = {s for a, s, _ in pending if a == "sell"}
        free_cash = cash + sum(holdings[s]["shares"] * holdings[s].get("last", holdings[s]["entry"]) for s in selling)
        for rank, (score, sym, a) in enumerate(ranked[:top_n], 1):
            if sym in holdings or not qualifies.get(sym) or not a:
                continue
            last = data[sym]["close"][data[sym]["pos"][d]]
            shares = int(min(risk_cash / a, cap_pos / last))
            if shares <= 0 or shares * last > free_cash:
                if shares > 0:
                    funnel["no_cash"] += 1
                continue
            free_cash -= shares * last
            buy_info[sym] = {"score": round(score, 1), "rank": rank}
            pending.append(("buy", sym, shares))
    last_d = cal[-1] if cal else None
    for sym, h in holdings.items():
        last = h.get("last", h["entry"])
        pnl = h["shares"] * (last - h["entry"]) - h["shares"] * h["entry"] * cost
        trades.append({"symbol": sym, "direction": "long", "entry_date": h["date"], "entry_price": round(h["entry"], 2),
                       "stop": None, "exit_date": None, "exit_price": None, "exit_reason": None, "status": "open",
                       "pnl_r": round(pnl / one_r, 3), "pnl_rupees": round(pnl, 0), "lots": h["shares"],
                       "extra": {"score_at_entry": h.get("score"), "rank_at_entry": h.get("rank"), "last_close": last,
                                 "last_date": last_d}})
    book = _book_stats(equity_curve, capital)
    if nifty and equity_curve:
        n0 = next((b["close"] for b in nifty if b["ts"][:10] >= equity_curve[0][0]), None)
        book["nifty_return_pct"] = round((nifty[-1]["close"] / n0 - 1) * 100, 1) if n0 else None
    return trades, {"funnel": funnel, "book": book, "holdings": len(holdings), "cash": round(cash, 0)}



def _book_stats(curve: list[tuple[str, float]], capital: float) -> dict:
    if len(curve) < 2:
        return {}
    eq = [v for _, v in curve]
    rets = [eq[k] / eq[k - 1] - 1 for k in range(1, len(eq)) if eq[k - 1] > 0]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak)
    years = max((date.fromisoformat(curve[-1][0]) - date.fromisoformat(curve[0][0])).days, 1) / 365.25
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / max(len(rets) - 1, 1))
    return {"return_pct": round((eq[-1] / capital - 1) * 100, 1),
            "cagr_pct": round(((eq[-1] / capital) ** (1 / years) - 1) * 100, 1) if eq[-1] > 0 else None,
            "max_drawdown_pct": round(dd * 100, 1),
            "sharpe_daily": round(m / sd * math.sqrt(252), 2) if sd > 0 else None,
            "days": len(curve)}


def live(today: date | None = None) -> dict:
    from backend.research import journal
    p = rcfg.strategy("cash_momentum")
    warm = (date.fromisoformat(rcfg.live_start()) - timedelta(days=500)).isoformat()
    stocks, nifty = _universe_bars(warm)
    trades, extra = simulate(stocks, nifty, p, start=rcfg.live_start())
    synced = journal.sync("cash_momentum", trades, rcfg.live_start())
    return {"status": "ok", **synced, "holdings": extra["holdings"]}


def status() -> dict:
    p = rcfg.strategy("cash_momentum")
    warm = (date.fromisoformat(rcfg.live_start()) - timedelta(days=500)).isoformat()
    stocks, nifty = _universe_bars(warm)
    trades, extra = simulate(stocks, nifty, p, start=rcfg.live_start())
    above = None
    if len(nifty) >= int(p["index_ma_days"]):
        closes = [b["close"] for b in nifty]
        above = closes[-1] > sum(closes[-int(p["index_ma_days"]):]) / int(p["index_ma_days"])
    return {"settings": p, "universe": len(stocks), "index_filter_on": above, "book": extra.get("book"),
            "cash": extra.get("cash"), "funnel": extra.get("funnel")}


def backtest(params: dict, progress=None) -> dict:
    p = {**rcfg.strategy("cash_momentum"), **(params.get("overrides") or {})}
    start = params.get("start_date")
    warm = (date.fromisoformat(start) - timedelta(days=500)).isoformat() if start else None
    stocks, nifty = _universe_bars(warm)
    trades, extra = simulate(stocks, nifty, p, start, params.get("end_date"))
    return {"trades": trades, "extra": extra, "settings": p}


def entry() -> dict:
    return {"key": "cash_momentum", "page": "cash-momentum", "kind": "trades", "live_after": "nse",
            "live": lambda today=None: live(today), "backtest": lambda params, progress=None: backtest(params, progress),
            "status": status, "limitations": LIMITS}
