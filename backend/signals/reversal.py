"""Trend reversal setup: stretch → exhaustion → structure break, filtered by the daily stage.

Agreed 25 Nov 2026; replaces the Wolfe Wave scan. Shadow-only (journal-tracked, no AI review,
no Telegram) until backtests and the journal show an edge. Pure functions here; the cash
scan lives in ``run_scan()`` at the bottom, futures / MCX call ``candidates()`` from the
hourly scan (``fno.run_hourly_scan``) on 1H bars of the traded contract.

A bottom (long) is described; a top (short) is the mirror image.

1. **Context**: at the reversal extreme the close was below the ``context_ema`` (200) EMA of the
   trigger timeframe — a real move down, not a dip in the middle of a range.
2. **Stretch**: within the last ``stretch_bars`` bars a close was at least ``z_stretch`` (2.5)
   standard deviations below the ``mean_ema`` (20) EMA (z-score of the close, ``z_lookback`` bars).
3. **Exhaustion**, at least one of
   * *RSI divergence*: the extreme low is below an earlier confirmed swing low
     (``div_min_apart``–``div_max_apart`` bars before it) while RSI is higher by
     ``min_rsi_gap``, and the earlier RSI low was oversold (≤ ``rsi_oversold``);
   * *swing failure (SFP / Wyckoff spring)*: the extreme bar took out a confirmed swing low of
     the last ``sfp_lookback`` bars and closed back above it.
   *Effort vs result* (Wyckoff): extreme-bar volume ≥ ``effort_volume_mult`` × average —
   scored, not required (index spot has no volume).
4. **Daily stage** (Minervini / Weinstein, ``backend.signals.stage``): longs only in
   ``long_stages`` (1, 2), shorts only in ``short_stages`` (3, 4) — trade against the trend of the
   trigger timeframe, never against the daily stage. No falling knives in Stage 4.
5. **ADX**: no trade while ADX is at or above ``adx_strong`` (30) and still rising (runaway trend).
6. **Trigger** (change of character): the first close above the extreme bar's high
   (``trigger: candle_high``) or above the last confirmed swing high before the extreme
   (``trigger: swing_high``), also above the ``trigger_ema`` (9) EMA, within ``trigger_bars``
   bars of the extreme. One signal per extreme.
7. **Trade**: stop = extreme − ``stop_buffer_atr`` ATR (at least ``min_stop_atr`` ATR, more than
   ``max_stop_atr`` ATR or ``max_stop_pct`` % rejects); target = ``target_r`` R; the instrument's
   managed exit (breakeven at 1R, then the ATR trail) applies. The mean (``mean_ema``) is recorded
   as the first objective.

Only completed, confirmed swings are used: a swing needs ``swing_bars`` bars on each side.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Callable, Sequence

from backend.decision.config import get_config, instrument_or_live
from backend.signals import stage as stage_mod
from backend.signals.indicators import adx as adx_series
from backend.signals.indicators import atr as atr_series
from backend.signals.indicators import ema, rsi as rsi_series, swing_points
from backend.signals.setups import stage

SETUP = "reversal"
CASH_SEGMENT = "cash_reversal"

DEFAULTS = {
    "context_ema": 200, "mean_ema": 20, "z_lookback": 20, "z_stretch": 2.5, "stretch_bars": 10,
    "rsi_period": 14, "swing_bars": 2, "div_min_apart": 5, "div_max_apart": 40, "rsi_oversold": 30,
    "rsi_overbought": 70, "min_rsi_gap": 2.0, "sfp_lookback": 50, "volume_lookback": 20,
    "effort_volume_mult": 1.5, "adx_period": 14, "adx_strong": 30, "trigger_bars": 8,
    "trigger": "candle_high", "trigger_ema": 9, "stop_buffer_atr": 0.5, "min_stop_atr": 1.0,
    "max_stop_atr": 3.0, "max_stop_pct": None, "target_r": 2.0, "long_stages": [1, 2], "short_stages": [3, 4],
    "require_stage": True,
}


def params(extra: dict | None) -> dict:
    """DEFAULTS overlaid with a config block (``instruments.<x>.reversal`` or ``instruments.cash_reversal``)."""
    p = {**DEFAULTS, **{k: v for k, v in (extra or {}).items() if k in DEFAULTS}}
    if p["trigger"] not in ("candle_high", "swing_high"):
        raise ValueError(f"reversal trigger must be candle_high or swing_high, not {p['trigger']!r}")
    return p


def config_for(instrument_extra: dict | None) -> dict | None:
    """``instruments.<futures/mcx>.reversal`` when enabled, else None."""
    block = (instrument_extra or {}).get("reversal")
    return params(block) if block and block.get("enabled") else None


def _zscores(closes: Sequence[float], mean: Sequence[float | None], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        if mean[i] is None:
            continue
        w = closes[i - n + 1:i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        out[i] = (closes[i] - mean[i]) / sd if sd > 0 else 0.0
    return out


class Series:
    """Indicators of one bar list, computed once (backtests reuse them for every signal bar)."""

    def __init__(self, bars: Sequence[dict], p: dict):
        closes = [b["close"] for b in bars]
        self.closes = closes
        self.ctx = ema(closes, p["context_ema"])
        self.mean = ema(closes, p["mean_ema"])
        self.trig = ema(closes, p["trigger_ema"])
        self.z = _zscores(closes, self.mean, p["z_lookback"])
        self.rsi = rsi_series(closes, p["rsi_period"])
        self.atr = atr_series(bars, 14)
        self.adx = adx_series(bars, p["adx_period"])
        self.k = p["swing_bars"]
        self.highs, self.lows = swing_points(bars, self.k)
        vols = [b.get("volume") or 0 for b in bars]
        self.vols = vols
        self.has_volume = any(vols[-p["volume_lookback"] * 3:])


def check(bars: Sequence[dict], d: str, p: dict, stage_info: dict | None = None,
          series: Series | None = None, t: int | None = None) -> dict:
    """Every step for one direction on bar ``t`` (default: the last). ``ok`` with the extreme,
    the evidence and the steps when all agree; else ``reason`` names the first failure."""
    t = len(bars) - 1 if t is None else t
    sr = series or Series(bars, p)
    s = 1 if d == "long" else -1
    out = {"direction": d, "ok": False, "steps": []}

    def fail(step: str, why: str) -> dict:
        out["steps"].append({"stage": step, "decision": "fail", "reason": why})
        out["reason"] = why
        return out

    def ok(step: str, note: str) -> None:
        out["steps"].append({"stage": step, "decision": "pass", "reason": note})

    need = p["context_ema"] + p["stretch_bars"] + 2
    if t + 1 < need:
        return fail("setup", f"only {t + 1} bars (need {need})")
    k = p["swing_bars"]
    lo = max(0, t - p["stretch_bars"])
    window = range(lo, t)                                   # bars before the trigger bar
    ext_val = lambda i: bars[i]["low"] if s > 0 else bars[i]["high"]   # noqa: E731
    e = min(window, key=ext_val) if s > 0 else max(window, key=ext_val)
    extreme = ext_val(e)
    if s * (ext_val(t) - extreme) < 0:
        return fail("setup", "new extreme on this bar — no turn yet")
    if t - e > p["trigger_bars"]:
        return fail("trigger", f"extreme {t - e} bars ago (max {p['trigger_bars']})")

    # 1. context: the extreme came in a real move (close beyond the 200 EMA)
    if sr.ctx[e] is None or s * (sr.closes[e] - sr.ctx[e]) >= 0:
        side = "below" if s > 0 else "above"
        return fail("context", f"close not {side} the {p['context_ema']} EMA at the extreme")
    ok("context", f"{'below' if s > 0 else 'above'} the {p['context_ema']} EMA")

    # 2. stretch
    zs = [sr.z[i] for i in range(lo, e + 1) if sr.z[i] is not None]
    deepest = (min(zs) if s > 0 else max(zs)) if zs else None
    if deepest is None or s * deepest > -p["z_stretch"]:
        return fail("stretch", f"no close {p['z_stretch']} SD {'below' if s > 0 else 'above'} the "
                               f"{p['mean_ema']} EMA (deepest {deepest if deepest is None else round(deepest, 2)})")
    ok("stretch", f"z-score {deepest:.2f}")

    # 3. exhaustion: RSI divergence and/or swing failure
    swings = sr.lows if s > 0 else sr.highs
    confirmed_before = [(i, v) for i, v in swings if i <= e - k]
    div = None
    for i, v in reversed(confirmed_before):
        if not p["div_min_apart"] <= e - i <= p["div_max_apart"]:
            if e - i > p["div_max_apart"]:
                break
            continue
        r0, r1 = sr.rsi[i], sr.rsi[e]
        if r0 is None or r1 is None:
            continue
        price_beyond = s * (v - extreme) > 0                       # lower low (long) / higher high (short)
        rsi_better = s * (r1 - r0) >= p["min_rsi_gap"]
        stretched = r0 <= p["rsi_oversold"] if s > 0 else r0 >= p["rsi_overbought"]
        if price_beyond and rsi_better and stretched:
            div = {"first_index": i, "first_price": round(v, 2), "first_rsi": round(r0, 1), "rsi": round(r1, 1)}
            break
    sfp = None
    for i, v in reversed(confirmed_before):
        if e - i > p["sfp_lookback"]:
            break
        if s * (v - extreme) > 0 and s * (sr.closes[e] - v) > 0:   # took it out, closed back inside
            sfp = {"swing_index": i, "swing_price": round(v, 2)}
            break
    if not div and not sfp:
        return fail("exhaustion", "no RSI divergence and no swing failure at the extreme")
    ok("exhaustion", " + ".join(x for x in ("RSI divergence" if div else "", "swing failure" if sfp else "") if x))
    avg_vol = sum(sr.vols[max(0, e - p["volume_lookback"]):e]) / max(1, min(e, p["volume_lookback"]))
    effort = (sr.vols[e] / avg_vol) if sr.has_volume and avg_vol else None

    # 4. daily stage
    if p["require_stage"]:
        allowed, why = stage_mod.allows(stage_info, d, p["long_stages"], p["short_stages"])
        if not allowed:
            return fail("stage", why)
        ok("stage", why)

    # 5. ADX: no runaway trend
    a_now, a_prev = sr.adx[t], sr.adx[t - 1]
    if a_now is not None and a_prev is not None and a_now >= p["adx_strong"] and a_now >= a_prev:
        return fail("adx", f"ADX {a_now:.0f} rising above {p['adx_strong']} (runaway trend)")
    ok("adx", f"ADX {a_now:.0f}" if a_now is not None else "ADX not ready")

    # 6. trigger
    if p["trigger"] == "candle_high":
        level = bars[e]["high"] if s > 0 else bars[e]["low"]
        name = "the reversal bar's " + ("high" if s > 0 else "low")
    else:
        opp = sr.highs if s > 0 else sr.lows
        prior = [v for i, v in opp if i <= e - k]
        if not prior:
            return fail("trigger", "no swing before the extreme to break")
        level, name = prior[-1], "the last swing " + ("high" if s > 0 else "low")

    def fires(i: int) -> bool:
        return sr.trig[i] is not None and s * (sr.closes[i] - level) > 0 and s * (sr.closes[i] - sr.trig[i]) > 0

    if not fires(t):
        return fail("trigger", f"waiting for a close beyond {name} {level:.2f}")
    if any(fires(i) for i in range(e + 1, t)):
        return fail("trigger", "already triggered for this extreme")
    ok("trigger", f"close {sr.closes[t]:.2f} beyond {name} {level:.2f}")
    out.update(ok=True, reason="", extreme=round(extreme, 2), extreme_index=e,
               extreme_ts=bars[e]["ts"], z=round(deepest, 2), divergence=div, sfp=sfp,
               effort=round(effort, 2) if effort is not None else None, trigger_level=round(level, 2),
               adx=round(a_now, 1) if a_now is not None else None,
               adx_hook=bool(a_now is not None and a_prev is not None and a_now < a_prev),
               mean_level=round(sr.mean[t], 2) if sr.mean[t] is not None else None)
    return out


def score(res: dict, stage_info: dict | None, close_pos: float, p: dict) -> dict:
    """0–100: stretch 20, exhaustion 25, effort (volume) 15, daily stage 20, ADX 10, trigger bar 10.
    Without volume the other factors are scaled up from 85."""
    s = 1 if res["direction"] == "long" else -1
    z = abs(res["z"])
    st = (stage_info or {}).get("stage")
    best = 2 if s > 0 else 4
    second = 1 if s > 0 else 3
    factors = {
        "stretch": 20 if z >= 3.5 else 15 if z >= 3.0 else 10,
        "exhaustion": 25 if res["divergence"] and res["sfp"] else 18 if res["divergence"] else 15,
        "daily_stage": 20 if st == best else 12 if st == second else 0,
        "adx": 10 if res["adx_hook"] or (res["adx"] is not None and res["adx"] < 20) else 5,
        "trigger_bar": 10 if close_pos >= 0.7 else 6 if close_pos >= 0.5 else 2,
    }
    if res["effort"] is not None:
        factors["effort"] = 15 if res["effort"] >= p["effort_volume_mult"] else 8 if res["effort"] >= 1.0 else 0
        total = float(sum(factors.values()))
    else:
        factors["effort"] = None
        total = round(sum(v for v in factors.values() if v is not None) * 100 / 85, 1)
    return {"factors": factors, "score": total}


def _close_pos(bar: dict, s: int) -> float:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return 0.5
    pos = (bar["close"] - bar["low"]) / rng
    return pos if s > 0 else 1 - pos


def build(bars: Sequence[dict], d: str, p: dict, stage_info: dict | None = None, regime: str | None = None,
          series: Series | None = None, t: int | None = None) -> dict:
    """One direction → a row shaped like ``setups.evaluate()`` (valid/trackable, entry/stop/target,
    score, stages), or a rejection with its reason."""
    t = len(bars) - 1 if t is None else t
    s = 1 if d == "long" else -1
    sr = series or Series(bars, p)
    res = check(bars, d, p, stage_info, sr, t)
    stages = [stage(x["stage"], x["decision"], x["reason"]) for x in res["steps"]]
    row = {"setup": SETUP, "direction": d, "valid": False, "trackable": False, "regime": regime,
           "daily_stage": (stage_info or {}).get("stage"), "stages": stages,
           "rule_version": get_config().rule_version}
    if not res["ok"]:
        row["reject_reason"] = res["reason"]
        return row
    a = sr.atr[t]
    if not a:
        row["reject_reason"] = "ATR not ready"
        return row
    entry = bars[t]["close"]
    stop = res["extreme"] - s * p["stop_buffer_atr"] * a
    if s * (entry - stop) < p["min_stop_atr"] * a:
        stop = entry - s * p["min_stop_atr"] * a
    risk = s * (entry - stop)
    why = None
    if risk > p["max_stop_atr"] * a + 1e-9:
        why = f"stop too wide ({risk / a:.1f} ATR > {p['max_stop_atr']:g})"
    elif p["max_stop_pct"] and risk / entry > p["max_stop_pct"] / 100:
        why = f"stop too wide ({100 * risk / entry:.1f}% > {p['max_stop_pct']:g}%)"
    if why:
        row["reject_reason"] = why
        stages.append(stage("construction", "fail", why))
        return row
    target = entry + s * p["target_r"] * risk
    stages.append(stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2),
                        target=round(target, 2), rr=p["target_r"]))
    sc = score(res, stage_info, _close_pos(bars[t], s), p)
    min_shortlist = get_config().global_.scoring.min_shortlist_score
    stages.append(stage("scoring", "pass" if sc["score"] >= min_shortlist else "below_threshold", score=sc["score"]))
    row.update({
        "valid": True, "trackable": True, "reject_reason": None, "entry": round(entry, 2), "stop": round(stop, 2),
        "target": round(target, 2), "risk_per_unit": round(risk, 2), "rr": round(p["target_r"], 2),
        "atr": round(a, 4), "stop_atr": round(risk / a, 2), "score": sc["score"], "factors": sc["factors"],
        "signal_close": round(entry, 2), "next_level": res["mean_level"],
        "vol_ratio": res["effort"], "htf_trend": None, "risk_mult": 1.0,
        "soft_checks": {"divergence": bool(res["divergence"]), "swing_failure": bool(res["sfp"]),
                        "effort_volume": None if res["effort"] is None else res["effort"] >= p["effort_volume_mult"]},
        "pattern": {"kind": "reversal", "extreme": res["extreme"], "extreme_ts": res["extreme_ts"],
                    "z": res["z"], "divergence": res["divergence"], "sfp": res["sfp"], "effort": res["effort"],
                    "trigger_level": res["trigger_level"], "mean_level": res["mean_level"],
                    "daily_stage": (stage_info or {}).get("label")},
    })
    return row


def candidates(bars: Sequence[dict], daily: Sequence[dict], p: dict, *, directions=("long", "short"),
               regime: str | None = None, stage_info: dict | None = None) -> list[dict]:
    """Rows for every direction (the hourly futures / MCX scan and the cash scan)."""
    info = stage_info if stage_info is not None else (stage_mod.classify(daily) if daily else None)
    sr = Series(bars, p)
    return [build(bars, d, p, info, regime, sr) for d in directions]


def find_all(bars: Sequence[dict], p: dict, daily_stages: Callable[[int], dict | None],
             directions=("long",), start: int = 0, on_reject: Callable[[str], None] | None = None) -> list[dict]:
    """Every trackable signal by bar, point in time — for backtests. ``daily_stages(t)`` gives the
    stage info valid at bar ``t``. Indicators are causal, so they are computed once. ``on_reject``
    receives the reason of every bar that was checked and failed (the backtest drop-out funnel)."""
    sr = Series(bars, p)
    out = []
    for t in range(max(start, p["context_ema"] + p["stretch_bars"] + 2), len(bars)):
        for d in directions:
            sv = sr.trig[t]
            if sv is None:
                continue
            s = 1 if d == "long" else -1
            if s * (sr.closes[t] - sv) <= 0:           # cheap pre-filter: the trigger needs a close beyond EMA9
                if on_reject:
                    on_reject(f"no close beyond the {p['trigger_ema']} EMA")
                continue
            row = build(bars[:t + 1], d, p, daily_stages(t), None, _Prefix(sr, t), t)
            if row.get("trackable"):
                row["s"] = t
                out.append(row)
            elif on_reject:
                on_reject(row.get("reject_reason") or "rejected")
    return out


class _Prefix(Series):
    """A view of a ``Series`` as of bar ``t``: swings confirmed by ``t`` only."""

    def __init__(self, sr: Series, t: int):   # noqa: D401 — no recomputation
        self.__dict__.update(sr.__dict__)
        k = sr.k
        self.highs = [(i, v) for i, v in sr.highs if i <= t - k]
        self.lows = [(i, v) for i, v in sr.lows if i <= t - k]


# --- cash scan (daily, long only; replaces the Wolfe Wave scan) ---------------------------------

def cash_settings() -> dict:
    inst = instrument_or_live(CASH_SEGMENT)
    x = inst.model_extra or {}
    return {
        "inst": inst, "enabled": inst.enabled, "hold_days": inst.hold_trading_days or 20,
        "shortlist": inst.shortlist_per_scan, "min_bars": inst.min_primary_bars,
        "min_turnover": float(x.get("min_turnover_cr", 50)) * 1e7,
        "event_block_days": int(x.get("event_block_days", 5)),
        "entry_window": x.get("entry_window", ["09:15", "15:15"]),
        "params": params(x), "directions": tuple(d for d in inst.directions if d in ("long", "short")) or ("long",),
        # optional NIFTY-regime filter (26 Nov 2026, tested in a rule set first): NIFTY's daily regime must be one
        # of these; None = no filter (live)
        "nifty_regimes": list(x["nifty_regimes"]) if x.get("nifty_regimes") else None,
    }


def cash_candidate(bars: Sequence[dict], cfg: dict, regime: str | None, nifty: Sequence[dict] | None = None) -> dict:
    """The cash scan's row for one stock: the best valid direction, else the long rejection."""
    allowed = cfg.get("nifty_regimes")
    if allowed and regime not in allowed:
        return {"setup": SETUP, "direction": cfg["directions"][0], "valid": False, "trackable": False,
                "regime": regime, "reject_reason": f"NIFTY regime {regime} not in {', '.join(allowed)}"}
    rows = candidates(bars, bars, cfg["params"], directions=cfg["directions"], regime=regime)
    valid = [r for r in rows if r.get("trackable")]
    if valid:
        return max(valid, key=lambda r: r["score"])
    return rows[0]


def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    """Daily cash reversal scan on every F&O stock (after the cup & handle scan)."""
    from backend.signals.daily_cash_scan import run_daily_scan
    cfg = cash_settings()
    return run_daily_scan(CASH_SEGMENT, SETUP, cfg, cash_candidate, "Reversal scan", now=now, force=force,
                          results_fetch=results_fetch)
