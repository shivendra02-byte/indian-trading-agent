"""Risk switch (research, information only) — a rules version of the drawdown-risk indicator
in Berzal & García, "Beyond Trend Following" (2024).

A daily score from -1 (high risk of a NIFTY fall) to +1, the weighted average of components
each scaled to -1 … +1 (weights in research.yaml; a missing input is dropped and the rest
re-weighted):
  trend         NIFTY above/below its 50- and 200-day SMAs (±0.5 each)
  momentum      NIFTY 20-day return ÷ 5%
  breadth       share of F&O stocks above their 50-day SMA, centred on 50%
  volatility    India VIX: 1 − 2 × its 1-year percentile, and minus its 5-day change ÷ 25%
  realised_vol  (1.2 − NIFTY 20-day ÷ 120-day realised volatility) ÷ 0.5
  crude         minus the CRUDEOILM 20-day change ÷ 15%
Hysteresis (paper): DEFENSIVE when the score falls below ``off_below``; back to NORMAL only
above ``on_above``, so small wobbles don't flip it.

It gates nothing. The study measures whether DEFENSIVE days were followed by a NIFTY fall of
``drawdown_pct`` within ``horizon_days`` more often than NORMAL days.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from datetime import date

from backend.db import get_db
from backend.research import config as rcfg


def _clip(v: float) -> float:
    return max(-1.0, min(1.0, v))


def _sma_at(closes: list[float], i: int, n: int) -> float | None:
    return sum(closes[i - n + 1:i + 1]) / n if i + 1 >= n else None


def _rv(closes: list[float], i: int, n: int) -> float | None:
    if i < n:
        return None
    r = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    m = sum(r) / n
    return math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))


def compute(nifty: list[dict], stocks: dict[str, list[dict]], vix: dict[str, float],
            crude: list[dict], p: dict) -> list[dict]:
    weights = p.get("weights") or {}
    dates = [b["ts"][:10] for b in nifty]
    closes = [b["close"] for b in nifty]
    # breadth: per date, share of stocks above their 50-day SMA
    above: dict[str, list[int]] = {}
    for bars in stocks.values():
        c = [b["close"] for b in bars]
        for j in range(49, len(bars)):
            d = bars[j]["ts"][:10]
            sma = sum(c[j - 49:j + 1]) / 50
            above.setdefault(d, [0, 0])
            above[d][0] += c[j] > sma
            above[d][1] += 1
    vix_dates = sorted(vix)
    crude_d = [b["ts"][:10] for b in crude]
    crude_c = [b["close"] for b in crude]
    out, state = [], "NORMAL"
    for i in range(len(nifty)):
        if i < 200:
            continue
        comp: dict[str, float] = {}
        s50, s200 = _sma_at(closes, i, 50), _sma_at(closes, i, 200)
        comp["trend"] = (0.5 if closes[i] > s50 else -0.5) + (0.5 if closes[i] > s200 else -0.5)
        comp["momentum"] = _clip((closes[i] / closes[i - 20] - 1) / 0.05)
        b = above.get(dates[i])
        if b and b[1] >= 20:
            comp["breadth"] = _clip((b[0] / b[1] - 0.5) / 0.5)
        k = bisect_right(vix_dates, dates[i])
        if k >= 60:
            window = [vix[d] for d in vix_dates[max(0, k - 252):k]]
            now = window[-1]
            pct = sum(1 for v in window if v <= now) / len(window)
            prev = vix[vix_dates[k - 6]] if k >= 6 else now
            comp["volatility"] = (_clip(1 - 2 * pct) + _clip(-(now / prev - 1) / 0.25)) / 2
        rv20, rv120 = _rv(closes, i, 20), _rv(closes, i, 120)
        if rv20 and rv120:
            comp["realised_vol"] = _clip((1.2 - rv20 / rv120) / 0.5)
        j = bisect_right(crude_d, dates[i]) - 1
        if j >= 20:
            comp["crude"] = _clip(-(crude_c[j] / crude_c[j - 20] - 1) / 0.15)
        w = {k2: float(weights.get(k2, 0)) for k2 in comp if float(weights.get(k2, 0)) > 0}
        total = sum(w.values())
        if total <= 0:
            continue
        score = sum(comp[k2] * w[k2] for k2 in w) / total
        if state == "NORMAL" and score < float(p["off_below"]):
            state = "DEFENSIVE"
        elif state == "DEFENSIVE" and score > float(p["on_above"]):
            state = "NORMAL"
        out.append({"date": dates[i], "score": round(score, 3), "state": state, "close": closes[i],
                    "components": {k2: round(v, 3) for k2, v in comp.items()}})
    return out


def study(rows: list[dict], p: dict, start: str | None = None, end: str | None = None) -> dict:
    """Was DEFENSIVE followed by a NIFTY fall of drawdown_pct within horizon_days more often?"""
    h, dd = int(p["horizon_days"]), float(p["drawdown_pct"]) / 100
    closes = [r["close"] for r in rows]
    groups: dict[str, dict] = {}
    buckets: dict[str, dict] = {}
    switches = 0
    for i, r in enumerate(rows):
        if (start and r["date"] < start) or (end and r["date"] > end) or i + h >= len(rows):
            continue
        future = closes[i + 1:i + h + 1]
        fell = min(future) / closes[i] - 1 <= -dd
        ret = closes[i + h] / closes[i] - 1
        g = groups.setdefault(r["state"], {"days": 0, "falls": 0, "ret_sum": 0.0})
        g["days"] += 1
        g["falls"] += fell
        g["ret_sum"] += ret
        band = "< -0.3" if r["score"] < -0.3 else "-0.3 … 0" if r["score"] < 0 else "0 … 0.3" if r["score"] < 0.3 else "≥ 0.3"
        bb = buckets.setdefault(band, {"days": 0, "falls": 0, "ret_sum": 0.0})
        bb["days"] += 1
        bb["falls"] += fell
        bb["ret_sum"] += ret
        if i and rows[i - 1]["state"] != r["state"]:
            switches += 1

    def fmt(x):
        return {k: {"days": v["days"], "fall_rate_pct": round(100 * v["falls"] / v["days"], 1) if v["days"] else None,
                    "avg_fwd_return_pct": round(100 * v["ret_sum"] / v["days"], 2) if v["days"] else None}
                for k, v in x.items()}
    total = sum(g["days"] for g in groups.values())
    falls = sum(g["falls"] for g in groups.values())
    years = total / 250 if total else 0
    return {"by_state": fmt(groups), "by_score": fmt(buckets), "days": total,
            "base_fall_rate_pct": round(100 * falls / total, 1) if total else None,
            "switches_per_year": round(switches / years, 1) if years else None,
            "horizon_days": h, "drawdown_pct": p["drawdown_pct"]}


def _inputs(limit: int = 4000) -> tuple[list[dict], dict, dict, list[dict]]:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe
    from backend.research.data import daily_bars
    nifty = daily_bars("NIFTY", limit=limit)
    stocks = {}
    for s in fno_universe():
        if s["spot_token"]:
            bars = get_candles(s["spot_token"], "day", limit=limit)
            if len(bars) >= 60:
                stocks[s["symbol"]] = bars
    vix = {}
    try:
        with get_db() as conn:
            vix = {r["trade_date"]: float(r["close"]) for r in conn.execute("SELECT trade_date, close FROM india_vix")
                   if r["close"]}
    except Exception:
        vix = {}
    return nifty, stocks, vix, daily_bars("CRUDEOILM", limit=limit)


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS risk_switch_daily (
                            trade_date TEXT PRIMARY KEY, score REAL, state TEXT, nifty_close REAL, components TEXT,
                            updated_at TEXT DEFAULT (datetime('now', 'localtime')))""")


def _save(rows: list[dict]) -> None:
    ensure_table()
    with get_db() as conn:
        conn.executemany("INSERT OR REPLACE INTO risk_switch_daily (trade_date, score, state, nifty_close, components) "
                         "VALUES (?, ?, ?, ?, ?)",
                         [(r["date"], r["score"], r["state"], r["close"], json.dumps(r["components"])) for r in rows])


def live(today: date | None = None) -> dict:
    p = rcfg.strategy("risk_switch")
    rows = compute(*_inputs(), p)
    _save(rows[-400:])
    last = rows[-1] if rows else None
    return {"status": "ok" if last else "no data", "date": last and last["date"], "score": last and last["score"],
            "state": last and last["state"]}


def history(start: str, end: str) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        rows = conn.execute("SELECT trade_date, score, state FROM risk_switch_daily WHERE trade_date BETWEEN ? AND ? "
                            "ORDER BY trade_date", (start, end)).fetchall()
    return [dict(r) for r in rows]


def current(days: int = 120) -> dict:
    ensure_table()
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM risk_switch_daily ORDER BY trade_date DESC LIMIT ?", (days,))]
    for r in rows:
        r["components"] = json.loads(r["components"] or "{}")
    rows.reverse()
    return {"latest": rows[-1] if rows else None, "rows": rows, "settings": rcfg.strategy("risk_switch")}


def run_study(start: str | None = None, end: str | None = None) -> dict:
    p = rcfg.strategy("risk_switch")
    rows = compute(*_inputs(), p)
    out = study(rows, p, start, end)
    out["first"] = rows[0]["date"] if rows else None
    out["last"] = rows[-1]["date"] if rows else None
    return out


def entry() -> dict:
    return {"key": "risk_switch", "page": "risk-switch", "kind": "indicator", "live_after": "nse",
            "live": lambda today=None: live(today),
            "backtest": lambda params, progress=None: {"trades": [], "extra": run_study(params.get("start_date"),
                                                                                       params.get("end_date"))},
            "status": lambda: current(),
            "limitations": ["Breadth uses today's F&O list; India VIX history only as far back as the option data "
                            "download stored it (missing days drop the component)."]}
