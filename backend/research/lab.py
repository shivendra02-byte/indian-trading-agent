"""Research Lab: quick studies that are not trade books.

``trend_diagnostic()`` — which markets trend (Sepp & Lucic 2026): a trend system's profit
comes from positive autocorrelation of volatility-scaled daily returns at 1–3 month lags
(plus drift for slow spans). For each instrument it reports the autocorrelation by lag band
and the realised Sharpe of their continuous ("European") EWMA trend signal at several spans,
gross and after the research cost model. Nothing is stored or traded.
"""

from __future__ import annotations

import math
from statistics import median

from backend.research import config as rcfg
from backend.research.data import daily_bars, kind_of, research_symbols
from backend.research.trend import ewma_vol

SPANS = (20, 60, 120, 250)
BANDS = ((1, 5), (6, 21), (22, 63), (64, 252))
VOL_SPAN = 33
TRADING_DAYS = 252


def scaled_returns(closes: list[float], vol_span: int = VOL_SPAN) -> tuple[list[float], list[float]]:
    """(z, sigma): daily return ÷ the previous day's EWMA volatility; sigma aligned with z."""
    rets = [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    vol = ewma_vol(rets, vol_span)
    z, sig = [], []
    for i in range(2, len(closes)):
        if rets[i] is not None and vol[i - 1]:
            z.append(rets[i] / vol[i - 1])
            sig.append(vol[i - 1])
    return z, sig


def acf(x: list[float], max_lag: int) -> list[float]:
    n = len(x)
    if n < max_lag + 10:
        return []
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x)
    if var <= 0:
        return []
    return [sum((x[i] - m) * (x[i - k] - m) for i in range(k, n)) / var for k in range(1, max_lag + 1)]


def european_sharpe(z: list[float], sigma: list[float], span: int, cost_bps: float) -> dict:
    """Variance-preserving EWMA signal of z, traded as notional S/sigma; gross and net Sharpe."""
    nu = 1 - 2 / (span + 1)
    scale = math.sqrt(1 - nu * nu)
    s, prev_w = 0.0, None
    gross, net = [], []
    for t in range(1, len(z)):
        s = nu * s + z[t - 1]
        sig_t = s * scale                       # signal from returns up to t-1
        f = sig_t * z[t]                        # = notional (sig_t / sigma) × return
        w = sig_t / sigma[t]
        cost = cost_bps / 1e4 * abs(w - prev_w) if prev_w is not None else 0.0
        prev_w = w
        if t > span:                             # warm-up
            gross.append(f)
            net.append(f - cost)                 # both in return units of the notional
    def sharpe(v):
        if len(v) < 60:
            return None
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
        return round(m / sd * math.sqrt(TRADING_DAYS), 2) if sd > 0 else None
    return {"gross": sharpe(gross), "net": sharpe(net)}


def diagnose(symbol: str, bars: list[dict]) -> dict:
    closes = [b["close"] for b in bars]
    z, sigma = scaled_returns(closes)
    if len(z) < 300:
        return {"symbol": symbol, "bars": len(bars), "note": "fewer than ~300 daily bars — download research history"}
    rho = acf(z, 252)
    bands = {f"{a}-{b}": round(sum(rho[a - 1:b]) / (b - a + 1), 4) for a, b in BANDS} if rho else {}
    mean = sum(z) / len(z)
    sd = math.sqrt(sum((v - mean) ** 2 for v in z) / (len(z) - 1))
    cost = rcfg.cost_bps(kind_of(symbol) if symbol in research_symbols() else "stock_futures")
    spans = {str(sp): european_sharpe(z, sigma, sp, cost) for sp in SPANS}
    best_net = max((v["net"] for v in spans.values() if v["net"] is not None), default=None)
    verdict = ("too little data" if best_net is None else "trends" if best_net >= 0.3
               else "weak trend" if best_net > 0 else "does not trend")
    return {"symbol": symbol, "bars": len(bars), "first": bars[0]["ts"][:10], "last": bars[-1]["ts"][:10],
            "drift_sharpe": round(mean / sd * math.sqrt(TRADING_DAYS), 2) if sd else None,
            "acf_bands": bands, "sepp_sharpe": spans, "verdict": verdict}


def trend_diagnostic(include_stocks: bool = True, max_stocks: int = 80) -> dict:
    rows = [diagnose(s, daily_bars(s)) for s in research_symbols()]
    stocks = None
    if include_stocks:
        from backend.marketdata.candles import get_candles
        from backend.marketdata.instruments import fno_universe
        per = []
        for s in fno_universe()[:max_stocks * 3]:
            if not s["spot_token"]:
                continue
            bars = get_candles(s["spot_token"], "day", limit=4000)
            if len(bars) >= 500:
                per.append(diagnose(s["symbol"], bars))
            if len(per) >= max_stocks:
                break
        if per:
            def med(span, key):
                vals = [r["sepp_sharpe"][span][key] for r in per if r.get("sepp_sharpe") and
                        r["sepp_sharpe"][span][key] is not None]
                return round(median(vals), 2) if vals else None
            stocks = {"stocks": len(per),
                      "median_sepp_sharpe": {sp: {"gross": med(str(sp), "gross"), "net": med(str(sp), "net")}
                                             for sp in SPANS},
                      "share_trending": round(100 * sum(1 for r in per if r.get("verdict") == "trends") / len(per), 0),
                      "best": sorted((r for r in per if r.get("sepp_sharpe")),
                                     key=lambda r: -(r["sepp_sharpe"]["120"]["net"] or -9))[:10]}
    return {"spans": list(SPANS), "bands": [f"{a}-{b}" for a, b in BANDS], "instruments": rows,
            "fno_stocks": stocks,
            "notes": ["Sharpe of a continuous EWMA trend signal on volatility-scaled returns (Sepp & Lucic's "
                      "European system), annualised; 'net' after the research cost model.",
                      "Autocorrelation bands are average lag correlations of volatility-scaled returns; trend "
                      "following needs them positive at 1-3 month lags (22-63).",
                      "In-sample description of history, not a forecast."]}
