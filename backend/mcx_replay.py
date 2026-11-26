"""Replay the MCX hourly scan bar by bar over stored candles (asked 25 Nov 2026).

Read-only: uses the stored 1H and daily bars of each MCX mini's active contract, writes nothing,
sends nothing. For every completed 1H bar since ``--since`` it runs the scan rules twice:

* ``before`` — the rules as they were on 24 Nov (1H pullback / breakout / range reversal, the
  plain daily regime, breakout stops over 3 ATR rejected);
* ``new`` — today's rules: + breakdown shorts in HIGH_VOL with the stop capped at 3 ATR, the
  HIGH_VOL trend regime fix and the ``mcx_trend`` setup (4H trend → 2H pullback → 1H trigger).

Each signal that passes all rules with a score ≥ the shortlist score is sized like the live scan
(``before``: 1 lot must risk ≤ 1%; ``new``: also 1 lot up to ``one_lot_max_risk_pct``, 3.5%) and
followed on the later 1H bars with the MCX exit rule (limit fill within 2 bars, stop / target /
breakeven / trailing stop). One open trade per symbol and rule set, as live. Signals blocked by
size, an open trade or a US data window are followed too, as a separate "blocked" book with its
own one-trade-per-symbol rule, so you can see what they would have made. Not tested: the bid-ask
spread (needs a live quote) and the AI review.

Usage on the server (as the app user, from the app folder):

    sudo -u ita env TRADINGAGENTS_HOME=/opt/ita/data /opt/ita/app/venv/bin/python -m backend.mcx_replay --since 2026-10-01
    ... --symbol SILVERM          one contract only
    ... --rejects                 also count why shorts / longs were rejected
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from datetime import date, datetime, time, timedelta

from backend.backtest.engine import mark_eod
from backend.decision.config import exit_rule_for, get_config, lots_within_risk
from backend.journal.simulate import simulate
from backend.marketdata.candles import get_candles, resample_hours
from backend.marketdata.instruments import active_future
from backend.marketdata.store import MCX_UNDERLYINGS
from backend.signals import events, mcx_trend
from backend.signals.fno import (
    breakdown_shorts, hourly_quality, last_session_before, mcx_value_per_point, oi_signal, risk_per_trade,
)
from backend.signals.regime import current_regime
from backend.signals.setups import evaluate, htf_trend, min_score
from tradingagents.utils.market_calendar import MCX, session_windows

RULE_SETS = ("before", "new")


def _bar_end(bar: dict) -> datetime:
    start = datetime.fromisoformat(bar["ts"])
    windows = session_windows(MCX, start.date())
    close = max((w[1] for w in windows), default=time(23, 55))
    return min(start + timedelta(hours=1), datetime.combine(start.date(), close))


def _timing_ok(bar: dict, first_min: int, last_min: int) -> bool:
    end = _bar_end(bar)
    windows = session_windows(MCX, end.date())
    if not windows:
        return False
    open_, close = datetime.combine(end.date(), windows[0][0]), datetime.combine(end.date(), windows[-1][1])
    return open_ + timedelta(minutes=first_min) <= end < close - timedelta(minutes=last_min)


def candidates_at(rule_set: str, name: str, hourly: list[dict], h2: list[dict], h4: list[dict], daily: list[dict],
                  inst) -> tuple[list[dict], dict]:
    extra = inst.model_extra or {}
    new = rule_set == "new"
    regime = current_regime(daily, high_vol_trend=new and bool(extra.get("regime_high_vol_trend")))
    oi = oi_signal(hourly)
    cands = evaluate(hourly, h4, regime["regime"], directions=tuple(inst.directions), is_stock=False, oi=oi,
                     confirmations={"2h": h2}, context={"day": daily}, min_primary_bars=inst.min_primary_bars,
                     stock_regime=current_regime(daily)["regime"],
                     breakdown_shorts=breakdown_shorts("mcx") if new else None)
    trend_cfg = mcx_trend.config_for(extra) if new else None
    if trend_cfg:
        cands += mcx_trend.candidates(hourly, h2, h4, regime, htf_trend(daily), directions=tuple(inst.directions),
                                      oi=oi, cfg=trend_cfg, min_primary_bars=inst.min_primary_bars)
    return cands, regime


def _strip(reason: str) -> str:
    """Numbers out, so the same rejection groups together (as the backtest funnel does)."""
    return re.sub(r"\d+(\.\d+)?", "#", reason)[:90]


def replay(since: date, symbols=None, today: date | None = None) -> dict:
    cfg = get_config()
    inst = cfg.instrument("mcx")
    extra = inst.model_extra or {}
    first_min, last_min = int(extra.get("no_entry_first_minutes", 15)), int(extra.get("no_entry_last_minutes", 30))
    entry_bars = int(extra.get("entry_valid_bars", 2))
    vpp = mcx_value_per_point()
    budget = risk_per_trade()
    capital = get_config().global_.capital
    threshold = min_score()
    today = today or date.today()
    signals: list[dict] = []
    rejects = {rs: Counter() for rs in RULE_SETS}
    notes: list[str] = []
    for name in symbols or MCX_UNDERLYINGS:
        fut = active_future(name, segment="MCX-FUT", today=today)
        if not fut:
            notes.append(f"{name}: no active contract in the instrument master")
            continue
        token, expiry = fut["instrument_token"], date.fromisoformat(fut["expiry"])
        hourly_all = get_candles(token, "60minute", limit=5000)
        daily_all = get_candles(token, "day", limit=2000)
        if not hourly_all:
            notes.append(f"{name}: no stored 1H bars")
            continue
        notes.append(f"{name} ({fut['tradingsymbol']}): 1H bars {hourly_all[0]['ts'][:10]} → {hourly_all[-1]['ts'][:16]}, "
                     f"{len(daily_all)} daily bars")
        busy_until = {k: "" for rs in RULE_SETS for k in (rs, f"{rs}:blocked")}
        exit_by = last_session_before(MCX, expiry, today)
        for j, bar in enumerate(hourly_all):
            day = bar["ts"][:10]
            if day < since.isoformat() or j < 60 or not _timing_ok(bar, first_min, last_min):
                continue
            signal_end = _bar_end(bar)
            blocked, _ = events.mcx_event_block(name, signal_end)
            hourly = hourly_all[max(0, j - 399):j + 1]
            problem = hourly_quality(hourly, datetime.fromisoformat(bar["ts"]), MCX, need_oi=True,
                                     min_bars=inst.min_primary_bars)
            if problem:   # the live scan rejects these bars too (no volume / OI, missing bars)
                for rs in RULE_SETS:
                    rejects[rs][f"data: {_strip(problem)}"] += 1
                continue
            window = hourly_all[max(0, j - 1300):j + 1]
            h2, h4 = resample_hours(window, MCX, 2)[-300:], resample_hours(window, MCX, 4)[-300:]
            daily = [d for d in daily_all if d["ts"][:10] < day][-300:]
            if len(daily) < 200:
                continue
            for rs in RULE_SETS:
                new = rs == "new"
                cands, regime = candidates_at(rs, name, hourly, h2, h4, daily, inst)
                for c in cands:
                    if not (c.get("valid") and c.get("trackable")):
                        if c.get("direction") and c.get("reject_reason"):
                            rejects[rs][f"{c['direction']} {c['setup']}: {_strip(c['reject_reason'])}"] += 1
                        continue
                    if c["score"] < threshold:
                        rejects[rs][f"{c['direction']} {c['setup']}: score {c['score']} < {threshold}"] += 1
                        continue
                    row = {"rule_set": rs, "symbol": name, "signal_ts": bar["ts"][:16], "setup": c["setup"],
                           "direction": c["direction"], "regime": regime["regime"] + (f"/{regime['trend']}" if regime.get("trend") else ""),
                           "entry": c["entry"], "stop": c["stop"], "target": c["target"], "rr": c["rr"],
                           "stop_atr": c.get("stop_atr"), "score": c["score"], "stop_type": c.get("stop_type")}
                    lot_risk = c["risk_per_unit"] * vpp[name]
                    row["lot_risk"] = round(lot_risk)
                    if new:
                        lots, cap_note = lots_within_risk("mcx", lot_risk, name)
                    else:
                        lots = int(budget // lot_risk) if lot_risk else 0
                    if blocked:
                        why = "event window (US data)"
                    elif lots < 1 and new and cap_note:
                        why = cap_note
                    elif lots < 1:
                        why = f"1 lot risks ₹{lot_risk:,.0f} ({100 * lot_risk / capital:.1f}%)"
                    elif bar["ts"] < busy_until[rs]:
                        why = "open trade on this symbol"
                    else:
                        why = None
                    book = rs if why is None else f"{rs}:blocked"
                    if why and bar["ts"] < busy_until.get(book, ""):
                        row["result"] = f"{why} · (blocked book already in a trade)"
                        signals.append(row)
                        continue
                    fill_until = (signal_end + timedelta(hours=entry_bars)).isoformat(timespec="seconds")
                    path = [b for b in hourly_all[j + 1:] if b["ts"][:10] <= exit_by.isoformat()]
                    out = simulate(direction=c["direction"], entry=c["entry"], stop=c["stop"], target=c["target"],
                                   atr=c.get("atr"), bars=mark_eod(path), fill_until=fill_until,
                                   time_stop_date=exit_by.isoformat(), exit_rule=exit_rule_for("mcx"))
                    status = out.get("status")
                    if status == "closed":
                        outcome = f"{out.get('exit_reason')} {out.get('pnl_r'):+.2f}R at {str(out.get('exit_ts'))[:16]}"
                        busy_until[book] = str(out.get("exit_ts"))
                    elif status == "open":
                        outcome = "still open"
                        busy_until[book] = "9999"
                    else:
                        outcome = status + (f" ({out.get('reason')})" if out.get("reason") else "")
                    filled = status in ("closed", "open")
                    if why is None:
                        row.update(lots=max(1, lots), result=outcome, taken=filled, pnl_r=out.get("pnl_r"))
                    else:
                        row.update(result=f"BLOCKED: {why} · would have been: {outcome}", blocked_taken=filled,
                                   blocked_r=out.get("pnl_r"))
                    signals.append(row)
    return {"signals": signals, "rejects": rejects, "notes": notes}


def _print(res: dict, show_rejects: bool) -> None:
    for n in res["notes"]:
        print(n)
    for rs in RULE_SETS:
        rows = [r for r in res["signals"] if r["rule_set"] == rs]
        taken = [r for r in rows if r.get("taken")]
        closed = [r["pnl_r"] for r in taken if r.get("pnl_r") is not None]
        blocked = [r["blocked_r"] for r in rows if r.get("blocked_taken") and r.get("blocked_r") is not None]
        print(f"\n=== {rs.upper()} rules: {len(rows)} valid signals, {len(taken)} would have alerted/traded, "
              f"{len(closed)} closed, total {sum(closed):+.2f}R ===")
        print(f"    blocked signals followed anyway: {len(blocked)} closed, total {sum(blocked):+.2f}R "
              f"(wins {sum(1 for x in blocked if x > 0)}, losses {sum(1 for x in blocked if x <= 0)})")
        for sym in sorted({r["symbol"] for r in rows}):
            t = [r["pnl_r"] for r in taken if r["symbol"] == sym and r.get("pnl_r") is not None]
            b = [r["blocked_r"] for r in rows if r["symbol"] == sym and r.get("blocked_taken") and r.get("blocked_r") is not None]
            print(f"    {sym:<10} traded {len(t)} = {sum(t):+.2f}R · blocked {len(b)} = {sum(b):+.2f}R")
        for r in rows:
            print(f"{r['signal_ts']} {r['symbol']:<10} {r['direction']:<5} {r['setup']:<10} {r['regime']:<16} "
                  f"entry {r['entry']} stop {r['stop']} ({r['stop_atr']} ATR) target {r['target']} RR {r['rr']} "
                  f"score {r['score']} | lot risk ₹{r['lot_risk']:,} | {r['result']}"
                  + (f" | {r['stop_type']}" if r.get("stop_type") else ""))
        if show_rejects:
            print(f"--- {rs}: most common rejections ---")
            for reason, n in res["rejects"][rs].most_common(15):
                print(f"{n:5d}  {reason}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--since", default=(date.today() - timedelta(days=45)).isoformat())
    p.add_argument("--symbol", action="append", help="GOLDM / SILVERM / CRUDEOILM / NATGASMINI (repeatable)")
    p.add_argument("--rejects", action="store_true")
    a = p.parse_args(argv)
    _print(replay(date.fromisoformat(a.since), a.symbol), a.rejects)


if __name__ == "__main__":
    main()
