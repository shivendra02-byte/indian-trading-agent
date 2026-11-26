"""Minervini volatility contraction pattern (VCP) breakout — cash, long only, daily bars.

Agreed 25 Nov 2026, shadow-only (journal-tracked, no AI review, no Telegram) until it shows an
edge. Pure functions plus the daily scan (``run_scan``, after the cup & handle scan). All numbers
come from ``instruments.vcp`` in config/decision_engine.yaml.

* **Trend template** passes (``stage.trend_template``: Stage 2, SMA200 rising ≥ 1 month,
  50 > 150 > 200, ≥ 25% off the 52-week low, within 25% of the high).
* **Base**: the highest confirmed swing high of the last ``base_max_days`` bars starts the base
  (at least ``base_min_days`` before the signal). From it, alternating confirmed swings give the
  contractions (swing high → next swing low, in %): at least ``min_contractions``, the first at
  most ``max_first_depth_pct``, each at most ``contraction_ratio`` × the one before, the last at
  most ``max_last_depth_pct``; the lows rise and no swing high tops the base high.
* **Volume dry-up**: average volume of the last contraction ≤ ``dryup_ratio`` × the
  ``volume_avg_days`` average before the base (``require_dryup``).
* **Breakout**: the first close above the pivot (the last contraction's high), at most
  ``max_extended_pct`` above it, on ≥ ``breakout_volume_ratio`` × average volume.
* **Trade**: entry at the signal close (limit, next session); stop under the last contraction low
  (``stop_buffer_atr`` ATR), at most ``max_stop_pct``; target ``target_pct`` above, at least
  ``min_rr`` R; breakeven then trail (the book's ``exit_management``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Sequence

from backend.decision.config import get_config, instrument_or_live
from backend.signals import stage as stage_mod
from backend.signals.indicators import atr as atr_series
from backend.signals.setups import stage
from backend.signals.wolfe import collapse, raw_swings

SEGMENT = "vcp"
SETUP = "vcp"


def settings() -> dict:
    inst = instrument_or_live(SEGMENT)
    x = inst.model_extra or {}
    g = lambda k, d: float(x.get(k, d))  # noqa: E731
    return {
        "inst": inst, "enabled": inst.enabled, "hold_days": inst.hold_trading_days or 20,
        "shortlist": inst.shortlist_per_scan, "min_bars": inst.min_primary_bars,
        "swing_bars": int(g("swing_bars", 2)), "base_min_days": int(g("base_min_days", 15)),
        "base_max_days": int(g("base_max_days", 65)), "min_contractions": int(g("min_contractions", 2)),
        "max_first_depth": g("max_first_depth_pct", 35) / 100, "max_last_depth": g("max_last_depth_pct", 10) / 100,
        "contraction_ratio": g("contraction_ratio", 0.8), "require_dryup": bool(x.get("require_dryup", True)),
        "dryup_ratio": g("dryup_ratio", 0.8), "volume_avg_days": int(g("volume_avg_days", 50)),
        "breakout_volume_ratio": g("breakout_volume_ratio", 1.5), "max_extended": g("max_extended_pct", 5) / 100,
        "stop_buffer_atr": g("stop_buffer_atr", 0.25), "max_stop_pct": g("max_stop_pct", 8) / 100,
        "target_pct": g("target_pct", 20) / 100, "min_rr": g("min_rr", 2.0),
        "min_turnover": g("min_turnover_cr", 50) * 1e7, "event_block_days": int(g("event_block_days", 5)),
        "entry_window": x.get("entry_window", ["09:15", "15:15"]),
        # optional NIFTY-regime filter (26 Nov 2026, as cash_reversal; tested in a rule set first): NIFTY's
        # daily regime must be one of these; None = no filter (live)
        "nifty_regimes": list(x["nifty_regimes"]) if x.get("nifty_regimes") else None,
    }


def _avg(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def detect(bars: Sequence[dict], cfg: dict | None = None, stage_info: dict | None = None) -> dict:
    """A VCP breakout on the last bar, or why not."""
    cfg = cfg or settings()
    t = len(bars) - 1
    info = stage_info or stage_mod.classify(bars)
    if not info.get("trend_template", {}).get("passed"):
        failed = [k for k, v in info.get("trend_template", {}).get("checks", {}).items() if not v]
        return {"found": False, "reason": "trend template not met" + (f" ({', '.join(failed)})" if failed else
                                                                       f" ({info.get('reason')})")}
    k = cfg["swing_bars"]
    start = t - cfg["base_max_days"]
    pts = [p for p in collapse([p for p in raw_swings(bars, k) if p[0] <= t - k]) if p[0] >= start]
    highs = [p for p in pts if p[2] == "H"]
    if not highs:
        return {"found": False, "reason": "no swing high in the base window"}
    base = max(highs, key=lambda p: p[1])
    if t - base[0] < cfg["base_min_days"]:
        return {"found": False, "reason": f"base only {t - base[0]} days old (min {cfg['base_min_days']})"}
    seq = [p for p in pts if p[0] >= base[0]]
    contractions = []
    i = 0
    while i + 1 < len(seq):
        h, l = seq[i], seq[i + 1]
        if h[2] == "H" and l[2] == "L":
            contractions.append({"high_index": h[0], "high": round(h[1], 2), "low_index": l[0], "low": round(l[1], 2),
                                 "depth_pct": round(100 * (h[1] - l[1]) / h[1], 1)})
        i += 2 if h[2] == "H" else 1
    if seq and seq[-1][2] == "H" and seq[-1][0] < t - 1 and (not contractions or seq[-1][0] > contractions[-1]["low_index"]):
        # the latest contraction: from the last swing high, its low not yet a confirmed swing
        h = seq[-1]
        j = min(range(h[0] + 1, t), key=lambda x: bars[x]["low"])
        contractions.append({"high_index": h[0], "high": round(h[1], 2), "low_index": j,
                             "low": round(bars[j]["low"], 2),
                             "depth_pct": round(100 * (h[1] - bars[j]["low"]) / h[1], 1)})
    n = len(contractions)
    if n < cfg["min_contractions"]:
        return {"found": False, "reason": f"{n} contraction(s) (min {cfg['min_contractions']})"}
    depths = [c["depth_pct"] / 100 for c in contractions]
    if depths[0] > cfg["max_first_depth"]:
        return {"found": False, "reason": f"first contraction {100 * depths[0]:.0f}% deep "
                                          f"(max {100 * cfg['max_first_depth']:.0f}%)"}
    for a, b in zip(depths, depths[1:]):
        if b > a * cfg["contraction_ratio"] + 1e-9:
            return {"found": False, "reason": f"contractions not shrinking ({100 * a:.1f}% then {100 * b:.1f}%)"}
    if depths[-1] > cfg["max_last_depth"]:
        return {"found": False, "reason": f"last contraction {100 * depths[-1]:.1f}% deep "
                                          f"(max {100 * cfg['max_last_depth']:.0f}%)"}
    lows = [c["low"] for c in contractions]
    if any(b <= a for a, b in zip(lows, lows[1:])):
        return {"found": False, "reason": "contraction lows not rising"}
    if any(c["high"] > base[1] * 1.005 for c in contractions[1:]):
        return {"found": False, "reason": "a later swing high topped the base high"}
    last = contractions[-1]
    pivot = last["high"]
    vols = [b.get("volume") or 0 for b in bars]
    before = _avg(vols[max(0, base[0] - cfg["volume_avg_days"]):base[0]])
    last_vol = _avg(vols[last["high_index"]:t])
    dryup = last_vol / before if before else None
    if cfg["require_dryup"] and (dryup is None or dryup > cfg["dryup_ratio"]):
        return {"found": False, "reason": "no volume dry-up in the last contraction"
                + (f" ({dryup:.2f}× the earlier average)" if dryup is not None else "")}
    close, prev = bars[t]["close"], bars[t - 1]["close"]
    if not (close > pivot >= prev):
        return {"found": False, "reason": f"no first close above the pivot {pivot:.2f}"}
    if close > pivot * (1 + cfg["max_extended"]):
        return {"found": False, "reason": f"close {100 * (close / pivot - 1):.1f}% above the pivot (extended)"}
    avg50 = _avg(vols[max(0, t - cfg["volume_avg_days"]):t])
    vol_ratio = vols[t] / avg50 if avg50 else 0.0
    if vol_ratio < cfg["breakout_volume_ratio"]:
        return {"found": False, "reason": f"breakout volume {vol_ratio:.1f}× (min {cfg['breakout_volume_ratio']:g}×)"}
    day = lambda i: str(bars[i]["ts"])[:10]  # noqa: E731
    for c in contractions:
        c["high_date"], c["low_date"] = day(c.pop("high_index")), day(c.pop("low_index"))
    return {"found": True, "kind": "vcp", "pivot": round(pivot, 2), "contractions": contractions,
            "base_days": t - base[0], "base_high": round(base[1], 2), "dryup_ratio": round(dryup, 2) if dryup else None,
            "breakout_volume": round(vol_ratio, 2), "last_low": last["low"],
            "pct_below_high": info.get("pct_below_high"), "daily_stage": info.get("label")}


def score(p: dict) -> dict:
    """0–100: contractions 20, last tightness 20, volume dry-up 20, breakout volume 20, near the 52-week high 20."""
    n, last = len(p["contractions"]), p["contractions"][-1]["depth_pct"]
    dry = p["dryup_ratio"] if p["dryup_ratio"] is not None else 1.0
    factors = {
        "contractions": 20 if n >= 3 else 12,
        "tightness": 20 if last <= 5 else 14 if last <= 8 else 8,
        "dryup": 20 if dry <= 0.6 else 12 if dry <= 0.8 else 0,
        "breakout_volume": 20 if p["breakout_volume"] >= 2.0 else 14,
        "near_high": 20 if (p["pct_below_high"] or 99) <= 10 else 10,
    }
    return {"factors": factors, "score": float(sum(factors.values()))}


def candidate(bars: Sequence[dict], cfg: dict | None = None, regime: str | None = None,
              nifty: Sequence[dict] | None = None, stage_info: dict | None = None) -> dict:
    cfg = cfg or settings()
    info = stage_info or stage_mod.classify(bars)
    p = detect(bars, cfg, info)
    base = {"setup": SETUP, "direction": "long", "valid": False, "trackable": False, "regime": regime,
            "daily_stage": info.get("stage")}
    if not p["found"]:
        return {**base, "reject_reason": p["reason"], "stages": [stage("setup", "fail", p["reason"])]}
    a = atr_series(bars, 14)[-1]
    entry = bars[-1]["close"]
    stop = p["last_low"] - cfg["stop_buffer_atr"] * (a or 0)
    risk = entry - stop
    if risk <= 0 or risk / entry > cfg["max_stop_pct"]:
        why = f"stop {100 * risk / entry:.1f}% away (max {100 * cfg['max_stop_pct']:.0f}%)"
        return {**base, "reject_reason": why, "stages": [stage("setup", "pass"), stage("construction", "fail", why)]}
    target = entry * (1 + cfg["target_pct"])
    rr = (target - entry) / risk
    if rr < cfg["min_rr"]:
        target, rr = entry + cfg["min_rr"] * risk, cfg["min_rr"]
    sc = score(p)
    min_score = get_config().global_.scoring.min_shortlist_score
    return {**base, "valid": True, "trackable": True, "reject_reason": None, "entry": round(entry, 2),
            "stop": round(stop, 2), "target": round(target, 2), "risk_per_unit": round(risk, 2), "rr": round(rr, 2),
            "atr": round(a, 4) if a else None, "signal_close": round(entry, 2), "score": sc["score"],
            "factors": sc["factors"], "pattern": p, "vol_ratio": p["breakout_volume"],
            "soft_checks": {"volume_dryup": (p["dryup_ratio"] or 9) <= cfg["dryup_ratio"]},
            "rule_version": get_config().rule_version,
            "stages": [stage("setup", "pass", pivot=p["pivot"], contractions=len(p["contractions"])),
                       stage("construction", "pass", entry=round(entry, 2), stop=round(stop, 2),
                             target=round(target, 2), rr=round(rr, 2)),
                       stage("scoring", "pass" if sc["score"] >= min_score else "below_threshold", score=sc["score"])]}


def find_all(bars: Sequence[dict], cfg: dict | None = None, start: int = 0,
             on_reject: Callable[[str], None] | None = None) -> list[dict]:
    """Every trackable VCP breakout by signal bar, point in time — for backtests. ``on_reject``
    receives the reason of every checked bar that failed (the backtest drop-out funnel)."""
    cfg = cfg or settings()
    out = []
    for t in range(max(start, 250), len(bars)):
        if bars[t]["close"] <= bars[t - 1]["close"]:      # a breakout bar closes up
            if on_reject:
                on_reject("no up close")
            continue
        c = candidate(bars[:t + 1], cfg)
        if c.get("trackable"):
            c["s"] = t
            out.append(c)
        elif on_reject:
            on_reject(c.get("reject_reason") or "rejected")
    return out


def run_scan(now: datetime | None = None, force: bool = False,
             results_fetch: Callable[[str], date | None] | None = None) -> dict:
    """Daily VCP scan on every F&O stock (after the cup & handle scan)."""
    from backend.signals.daily_cash_scan import run_daily_scan
    cfg = settings()
    return run_daily_scan(SEGMENT, SETUP, cfg, scan_candidate, "VCP scan", now=now, force=force,
                          results_fetch=results_fetch)


def scan_candidate(bars: Sequence[dict], cfg: dict, regime: str | None, nifty: Sequence[dict] | None = None) -> dict:
    """The live scan's row: the NIFTY-regime filter (when configured), then ``candidate()``.
    Backtests apply the same filter in ``engine._run_daily_pattern`` (``nifty_regimes``)."""
    allowed = cfg.get("nifty_regimes")
    if allowed and regime not in allowed:
        return {"setup": SETUP, "direction": "long", "valid": False, "trackable": False, "regime": regime,
                "reject_reason": f"NIFTY regime {regime} not in {', '.join(allowed)}"}
    return candidate(bars, cfg, regime, nifty)
