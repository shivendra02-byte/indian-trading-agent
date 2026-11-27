"""MCX trend pullback — 4H trend, 2H pullback, 1H trigger (agreed 25 Nov 2026). Pure functions.

Asked after the November silver fall ($54 → $40 on the 2h chart): the 1h pullback rule looked
for the pullback on the 1h chart, where a 2–3 day bounce already reads as an uptrend (1h EMA20
above EMA50, RSI 60–70, 2H "against the trade"), so the lower-high entries were never taken.
This setup reads the trend and the pullback where you see them and only times the entry on 1H.
Shorts are described; longs are the mirror image.

* **Regime (daily, the commodity's own)**: the trade direction must be allowed — shorts in
  ``short_regimes`` (STRONG_DOWN, MODERATE_DOWN, HIGH_VOL), longs in ``long_regimes``
  (STRONG_UP, MODERATE_UP, HIGH_VOL). HIGH_VOL counts only with ``regime["trend"]`` in the trade's direction
  (``regime.current_regime(high_vol_trend=True)``). The daily trend must not be up.
* **4H trend**: EMA20 < EMA50, the 4H close below EMA50 (a bounce may lift it above EMA20 —
  that is the pullback) and the last confirmed 4H swing high below the highest high of the
  ``structure_bars`` (30, about a week) 4H bars before it — a lower high on the 4H scale.
* **2H pullback**: within the last ``pullback_bars`` 2H bars one bar's high reached the 2H
  EMA20 (within ``pullback_touch_atr`` 2H ATR), and the pullback high stayed below the highest
  4H high of the ``structure_bars`` 4H bars before the pullback (a lower high on the 4H scale;
  25 Nov 2026: comparing with the last *2H* swing rejected the 24 Nov silver bounce to ~44.2,
  which topped a small 2H swing at ~42.8 but was well under the ~48 start of the leg). ``hold_2h_ema_slow`` (off) would also demand
  every 2H close below the 2H EMA50 — too tight in a fast fall, where a normal bounce closes
  around the slow average (the 24 Nov silver bounce to ~44).
* **1H trigger**: the latest completed 1H bar is bearish, closes below the lowest low of the
  previous ``trigger_lookback`` 1H bars and (``trigger_ema``) below the 1H EMA20; one signal
  per pullback (no earlier trigger since the 2H touch).
* **Trade**: stop = pullback high + ``global.geometry.stop_buffer_atr`` 1H ATR (at least 1 ATR;
  wider than ``cap_stop_atr`` (3) → placed at 3 ATR from the entry, as for breakdown shorts),
  target ≥ ``min_rr`` R before the next 1H swing level (``setups.geometry``); the instrument's
  exit rule (breakeven at 1R, then the 2 ATR trail) applies as for every MCX trade.
  No RSI band and no volume requirement (volume is recorded and scored only).
"""

from __future__ import annotations

from typing import Sequence

from backend.decision.config import get_config
from backend.signals.indicators import atr, ema, swing_points
from backend.signals.regime import HIGH_VOL, direction as regime_direction
from backend.signals.setups import _close_position, _context, geometry, htf_trend, score, stage

SETUP = "mcx_trend"
DEFAULTS = {
    "ema_fast": 20, "ema_slow": 50, "h4_structure": True, "pullback_bars": 6, "pullback_touch_atr": 0.25,
    "hold_2h_ema_slow": False, "cap_stop_atr": 3.0, "structure_bars": 30,
    "trigger_lookback": 3, "trigger_ema": True,
    "short_regimes": ["STRONG_DOWN", "MODERATE_DOWN", "HIGH_VOL"], "long_regimes": ["STRONG_UP", "MODERATE_UP", "HIGH_VOL"],
}


def _s(d: str) -> int:
    return 1 if d == "long" else -1


def regime_allows(regime: dict, d: str, cfg: dict) -> tuple[bool, str]:
    name = regime.get("regime")
    allowed = cfg["long_regimes"] if d == "long" else cfg["short_regimes"]
    want = "up" if d == "long" else "down"
    if name not in allowed:
        return False, f"{d} not taken in a {name} regime"
    trend = regime.get("trend") if name == HIGH_VOL else regime_direction(name)
    if trend != want:
        return False, f"{name} regime trend is {trend or 'unclear'}, not {want}"
    return True, f"{name} ({trend})"


def trend_4h(h4: Sequence[dict], d: str, cfg: dict) -> tuple[bool, str]:
    if len(h4) < cfg["ema_slow"] + 1:
        return False, f"only {len(h4)} 4H bars"
    s = _s(d)
    closes = [b["close"] for b in h4]
    e20, e50 = ema(closes, cfg["ema_fast"])[-1], ema(closes, cfg["ema_slow"])[-1]
    side = "below" if s < 0 else "above"
    if not s * (e20 - e50) > 0:
        return False, f"4H EMA{cfg['ema_fast']} {e20:.2f} not {side} EMA{cfg['ema_slow']} {e50:.2f}"
    if not s * (closes[-1] - e50) > 0:
        return False, f"4H close {closes[-1]:.2f} not {side} EMA{cfg['ema_slow']} {e50:.2f}"
    if cfg["h4_structure"]:
        highs, lows = swing_points(h4, 2)
        pts = highs if s < 0 else lows
        if pts:
            i, v = pts[-1]
            before = h4[max(0, i - cfg["structure_bars"]):i]
            if before:
                ref = max(b["high"] for b in before) if s < 0 else min(b["low"] for b in before)
                if s * (ref - v) >= 0:
                    what = "lower high" if s < 0 else "higher low"
                    return False, (f"4H structure: last swing {v:.2f} beyond the {cfg['structure_bars']}-bar "
                                   f"{'high' if s < 0 else 'low'} {ref:.2f} before it (no {what})")
    return True, "4H trend agrees"


def lower_high_4h(h4: Sequence[dict], d: str, extreme: float, window_start: str, cfg: dict) -> tuple[bool, str]:
    """The pullback extreme must stay inside the 4H range of the ``structure_bars`` bars before it."""
    s = _s(d)
    before = [b for b in h4 if b["ts"] < window_start][:-1][-cfg["structure_bars"]:]   # last one may overlap
    if not before:
        return True, "no earlier 4H bars"
    ref = max(b["high"] for b in before) if s < 0 else min(b["low"] for b in before)
    if s * (ref - extreme) >= 0:
        what = "lower high" if s < 0 else "higher low"
        return False, (f"pullback {extreme:.2f} beyond the {cfg['structure_bars']}-bar 4H "
                       f"{'high' if s < 0 else 'low'} {ref:.2f} (no {what})")
    return True, f"pullback {extreme:.2f} {'below' if s < 0 else 'above'} the 4H {'high' if s < 0 else 'low'} {ref:.2f}"


def pullback_2h(h2: Sequence[dict], d: str, cfg: dict) -> tuple[dict | None, str]:
    """``{"extreme", "touch_ts"}`` of the 2H pullback, or why not."""
    n = cfg["pullback_bars"]
    if len(h2) < cfg["ema_slow"] + n + 5:
        return None, f"only {len(h2)} 2H bars"
    s = _s(d)
    closes = [b["close"] for b in h2]
    e20, e50, a = ema(closes, cfg["ema_fast"]), ema(closes, cfg["ema_slow"]), atr(h2, 14)
    recent = range(len(h2) - n, len(h2))
    if cfg["hold_2h_ema_slow"] and not all(e50[i] is not None and s * (closes[i] - e50[i]) > 0 for i in recent):
        side = "above" if s < 0 else "below"
        return None, f"a 2H close went {side} the 2H EMA{cfg['ema_slow']} in the last {n} bars (trend broken)"
    tol = cfg["pullback_touch_atr"]
    touched = [i for i in recent if a[i] is not None and e20[i] is not None and
               (h2[i]["high"] >= e20[i] - tol * a[i] if s < 0 else h2[i]["low"] <= e20[i] + tol * a[i])]
    if not touched:
        return None, f"no 2H pullback to EMA{cfg['ema_fast']} in the last {n} bars"
    first = touched[0]
    extreme = max(h2[i]["high"] for i in recent) if s < 0 else min(h2[i]["low"] for i in recent)
    return {"extreme": extreme, "touch_ts": h2[first]["ts"], "window_start": h2[recent[0]]["ts"]}, \
        f"2H pullback to EMA{cfg['ema_fast']}"


def trigger_1h(h1: Sequence[dict], d: str, since_ts: str, cfg: dict) -> tuple[bool, str]:
    k = cfg["trigger_lookback"]
    if len(h1) < max(cfg["ema_fast"], k) + 2:
        return False, f"only {len(h1)} 1H bars"
    s = _s(d)
    closes = [b["close"] for b in h1]
    e20 = ema(closes, cfg["ema_fast"])

    def fires(i: int) -> bool:
        prior = h1[i - k:i]
        edge = min(b["low"] for b in prior) if s < 0 else max(b["high"] for b in prior)
        bar = h1[i]
        ok = s * (bar["close"] - edge) > 0 and s * (bar["close"] - bar["open"]) > 0
        if ok and cfg["trigger_ema"]:
            ok = e20[i] is not None and s * (bar["close"] - e20[i]) > 0
        return ok

    last = len(h1) - 1
    if not fires(last):
        return False, "waiting for the 1H trigger"
    if any(fires(i) for i in range(k, last) if h1[i]["ts"] >= since_ts):
        return False, "1H trigger already fired for this pullback"
    side = "below" if s < 0 else "above"
    return True, f"1H close {closes[-1]:.2f} {side} the last {k} bars"


def check(d: str, regime: dict, daily_trend: float | None, h4: Sequence[dict], h2: Sequence[dict],
          h1: Sequence[dict], cfg: dict | None = None) -> dict:
    """Every stage for one direction. ``ok`` with ``stop_anchor`` when all agree."""
    cfg = {**DEFAULTS, **(cfg or {})}
    s = _s(d)
    out = {"direction": d, "ok": False, "stages": []}

    def fail(stage: str, why: str) -> dict:
        out["stages"].append({"stage": stage, "decision": "fail", "reason": why})
        out["reason"] = why
        return out

    def ok(stage: str, note: str) -> None:
        out["stages"].append({"stage": stage, "decision": "pass", "reason": note})

    allowed, why = regime_allows(regime, d, cfg)
    if not allowed:
        return fail("regime", why)
    ok("regime", why)
    if daily_trend is not None and daily_trend * s < 0:
        return fail("context", "daily trend against the trade")
    good, why = trend_4h(h4, d, cfg)
    if not good:
        return fail("trend_4h", why)
    ok("trend_4h", why)
    pb, why = pullback_2h(h2, d, cfg)
    if not pb:
        return fail("pullback_2h", why)
    good, why2 = lower_high_4h(h4, d, pb["extreme"], pb["window_start"], cfg)
    if not good:
        return fail("pullback_2h", why2)
    ok("pullback_2h", f"{why}; {why2}")
    fired, why = trigger_1h(h1, d, pb["touch_ts"], cfg)
    if not fired:
        return fail("trigger_1h", why)
    ok("trigger_1h", why)
    out.update(ok=True, reason="", stop_anchor=pb["extreme"], pullback_touch=pb["touch_ts"])
    return out


def candidates(hourly: Sequence[dict], h2: Sequence[dict], h4: Sequence[dict], regime: dict,
               daily_trend: float | None, *, directions=("long", "short"), oi: dict | None = None,
               cfg: dict | None = None, min_primary_bars: int = 60, setup_name: str = SETUP) -> list[dict]:
    """Rows in the same shape as ``setups.evaluate()`` for the hourly scan / journal. ``setup_name``
    labels the rows (NSE futures use "trend_pullback", a shadow setup — 25 Nov 2026); the
    stop/target/score parameters always come from ``strategies.mcx_trend``."""
    conf = get_config()
    cfg = {**DEFAULTS, **(cfg or {})}
    out = []
    if len(hourly) < min_primary_bars:
        return [{"setup": setup_name, "direction": None, "valid": False, "trackable": False,
                 "reject_reason": f"only {len(hourly)} bars (need {min_primary_bars})",
                 "stages": [stage("setup", "fail", f"only {len(hourly)} bars")]}]
    base_ctx = _context(hourly, SETUP)
    for d in directions:
        s = _s(d)
        res = check(d, regime, daily_trend, h4, h2, hourly, cfg)
        stages = [stage(x["stage"], x["decision"], x["reason"]) for x in res["stages"]]
        cand = {"setup": setup_name, "direction": d, "valid": False, "trackable": False, "regime": regime.get("regime"),
                "regime_trend": regime.get("trend"), "htf_trend": None, "vol_ratio": round(base_ctx["vol_ratio"], 2),
                "stages": stages, "rule_version": conf.rule_version}
        out.append(cand)
        if not res["ok"]:
            cand["reject_reason"] = res["reason"]
            continue
        ctx = dict(base_ctx)
        a = ctx["atr"][-1]
        if a is None:
            cand["reject_reason"] = "indicators not ready"
            continue
        ctx["stop_anchor"] = res["stop_anchor"]      # geometry adds the stop buffer (0.25 ATR)
        geo = geometry(hourly, ctx, s, cap_stop_atr=cfg["cap_stop_atr"])
        cand.update({k: v for k, v in geo.items() if k not in ("ok", "reason")})
        if not geo["ok"]:
            cand["reject_reason"] = geo["reason"]
            stages.append(stage("construction", "fail", geo["reason"]))
            continue
        stages.append(stage("construction", "pass", entry=geo["entry"], stop=geo["stop"],
                            target=geo["target"], rr=geo["rr"]))
        htf = htf_trend(h4) or 0.0
        scored = score(setup=SETUP, s=s, htf=htf, quality_bonus=0, close_pos=_close_position(hourly[-1], s),
                       vol_ratio=ctx["vol_ratio"], geo=geo, regime_fit=10, is_stock=False, rs_points=0,
                       oi_change_pct=(oi or {}).get("change_pct"), price_change=(oi or {}).get("price_change"))
        min_shortlist = conf.global_.scoring.min_shortlist_score
        stages.append(stage("scoring", "pass" if scored["score"] >= min_shortlist else "below_threshold",
                            score=scored["score"]))
        soft = {"volume": ctx["vol_ratio"] >= 1.0, "regime": True}
        if oi is not None:
            soft["oi_buildup"] = bool((oi.get("change_pct") or 0) >= 1.0 and s * (oi.get("price_change") or 0) > 0)
        cand.update(scored)
        cand.update({"valid": True, "trackable": True, "htf_trend": htf, "soft_checks": soft, "reject_reason": None,
                     "risk_mult": 1.0, "rs_vs_nifty_pct": None, "atr": round(a, 4),
                     "pullback_touch": res["pullback_touch"]})
    return out


def config_for(instrument_extra: dict | None) -> dict | None:
    """``instruments.mcx.trend_pullback`` when enabled, else None."""
    cfg = (instrument_extra or {}).get("trend_pullback")
    return cfg if cfg and cfg.get("enabled") else None
