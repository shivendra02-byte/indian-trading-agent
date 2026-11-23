"""Momentum book — portfolio backtest over stored spot history (point in time).

Every week (first trading day) the ranking is rebuilt from closes up to the day
before. Each day: open positions follow that day's hourly bars (stop, trail);
then each group's free slots look for an entry (daily trend on bars up to the
day before, hourly pullback + break of structure during the day — or the daily
bar when hourly history doesn't reach back, marked ``daily_only``); after the
close: EMA reversal, leaving the buffer group (re-rank days), time stop.

Results are in R (each trade risks the same 1R) with a daily mark-to-market
equity curve, so Sharpe / Sortino / Calmar describe the whole book, not single
trades. Limitations: today's F&O list (survivorship), spot prices for futures,
no lot sizes, no ban list / results dates / OI history, no costs.
"""

from __future__ import annotations

import bisect
import json
import math
import threading
from datetime import date
from typing import Callable

from backend.db import get_db
from backend.momentum import rules
from backend.momentum.book import compute_ranking, settings
from backend.signals.indicators import atr

LIMITATIONS = [
    "Survivorship: today's F&O stock list is used for the whole period.",
    "Spot prices stand in for futures (no rollover or basis).",
    "No lot sizes: every trade risks the same 1R; the live book skips stocks where one lot risks more than 1%.",
    "No historical F&O ban list, results dates or open interest; no costs.",
]


def _metrics(daily_r: list[tuple[str, float]], risk_pct: float) -> dict:
    """Book metrics from the daily equity curve in R."""
    if len(daily_r) < 2:
        return {}
    rets = [r * risk_pct / 100 for _, r in daily_r]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
    down = [min(0.0, x) for x in rets]
    dsd = math.sqrt(sum(x * x for x in down) / len(down))
    eq = peak = dd = 0.0
    for _, r in daily_r:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    years = len(daily_r) / 252
    total = sum(r for _, r in daily_r)
    return {"sharpe_annual": round(mean / sd * math.sqrt(252), 2) if sd else None,
            "sortino_annual": round(mean / dsd * math.sqrt(252), 2) if dsd else None,
            "max_drawdown_r": round(dd, 2), "r_per_year": round(total / years, 2) if years else None,
            "calmar": round(total / years / dd, 2) if dd and years else None,
            "return_pct": round(total * risk_pct, 2), "days": len(daily_r)}


def _trade_stats(trades: list[dict]) -> dict:
    rs = [t["pnl_r"] for t in trades]
    if not rs:
        return {"trades": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs), 1), "avg_r": round(sum(rs) / len(rs), 3),
            "total_r": round(sum(rs), 2),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1)}


def run(start: date, end: date, universe: list[dict], nifty: list[dict],
        daily_for: Callable[[dict], list[dict]], hourly_for: Callable[[dict], list[dict]],
        cfg: dict | None = None, sides: tuple[str, ...] | None = None, daily_only: bool = False,
        split_date: date | None = None, progress: Callable[[str], None] = lambda m: None) -> dict:
    cfg = cfg or settings()
    sides = sides or tuple(s for s in ("long", "short") if cfg[f"{s}_enabled"])
    days = [b["ts"][:10] for b in nifty if start.isoformat() <= b["ts"][:10] <= end.isoformat()]
    if not days:
        return {"status": "error", "reason": "no NIFTY bars in the period"}
    daily = {u["symbol"]: daily_for(u) for u in universe}
    d_index = {sym: [b["ts"][:10] for b in bars] for sym, bars in daily.items()}
    hourly = {} if daily_only else {u["symbol"]: hourly_for(u) for u in universe}
    h_index = {sym: [b["ts"][:10] for b in bars] for sym, bars in hourly.items()}
    by_sym = {u["symbol"]: u for u in universe}

    ranking: dict[str, dict] = {}
    open_pos: list[dict] = []
    trades: list[dict] = []
    daily_r: list[tuple[str, float]] = []
    marks: dict[int, float] = {}
    data_modes = {"hourly": 0, "daily_only": 0}
    pid = 0
    prev_week = None

    def close(pos, day, ts, price, reason):
        r = rules.pnl_r(pos, price)
        trades.append({"symbol": pos["symbol"], "side": pos["side"], "entry_date": pos["entry_ts"][:10],
                       "exit_date": day, "exit_ts": ts, "entry": round(pos["entry"], 2), "exit": round(price, 2),
                       "exit_reason": reason, "pnl_r": r, "days": pos["days"], "data": pos["data"],
                       "rank": pos["rank"]})
        open_pos.remove(pos)
        return r

    for n_day, day in enumerate(days):
        d = date.fromisoformat(day)
        realised = 0.0
        week = d.isocalendar()[:2]
        reranked = False
        if week != prev_week and n_day > 0:
            as_of = date.fromisoformat(days[n_day - 1])
            ranked = compute_ranking(universe, lambda u: daily[u["symbol"]], nifty, as_of, cfg)
            ranking = {r["symbol"]: r for r in ranked}
            reranked = True
        prev_week = week

        def day_bars(sym):
            i = bisect.bisect_left(d_index[sym], day)
            if i >= len(d_index[sym]) or d_index[sym][i] != day:
                return None, i, []
            hb = []
            if sym in hourly:
                lo, hi = bisect.bisect_left(h_index[sym], day), bisect.bisect_right(h_index[sym], day)
                hb = hourly[sym][lo:hi]
            return daily[sym][i], i, hb

        # 1. open positions through the day
        for pos in list(open_pos):
            bar, i, hb = day_bars(pos["symbol"])
            if bar is None:
                continue
            pos["days"] += 1
            upd = rules.follow(pos, hb or [{**bar, "ts": day + "T15:30:00"}], cfg)
            pos.update(stop=upd["stop"], best=upd["best"], last_ts=upd["last_ts"])
            if upd["exit"]:
                realised += close(pos, day, upd["exit"]["ts"], upd["exit"]["price"], upd["exit"]["reason"]) \
                    - marks.pop(pos["id"], 0.0)
        # 2. entries
        if ranking:
            held = {p["symbol"] for p in open_pos}
            for side in sides:
                s = 1 if side == "long" else -1
                room = cfg["max_positions_per_side"] - len([p for p in open_pos if p["side"] == side])
                group = sorted((r for r in ranking.values() if r["side"] == side),
                               key=lambda r: r["rank"] if side == "long" else r["rank_from_bottom"])
                for row in group:
                    if room <= 0:
                        break
                    sym = row["symbol"]
                    if sym in held or sym not in by_sym:
                        continue
                    bar, i, hb = day_bars(sym)
                    if bar is None or i < cfg["ema_slow"] + 61:
                        continue
                    ok, _ = rules.trend_ok(daily[sym][:i], s, cfg)
                    if not ok:
                        continue
                    a = atr(daily[sym][:i], 14)[-1]
                    if not a:
                        continue
                    trig, mode, rest = None, "hourly", []
                    if hb:
                        start_h = bisect.bisect_left(h_index[sym], day)
                        for j in range(len(hb)):
                            trig = rules.entry_trigger(hourly[sym][max(0, start_h + j - 300):start_h + j + 1], s, cfg)
                            if trig:
                                rest = hb[j + 1:]
                                break
                    else:
                        mode = "daily_only"
                        trig = rules.entry_trigger(daily[sym][max(0, i - 300):i + 1], s, cfg)
                        if trig:
                            trig["ts"] = day + "T15:30:00"
                    if not trig:
                        continue
                    pid += 1
                    stop = trig["entry"] - s * cfg["stop_atr"] * a
                    pos = {"id": pid, "symbol": sym, "side": side, "entry": trig["entry"], "stop": stop,
                           "initial_stop": stop, "atr": a, "best": trig["entry"], "last_ts": trig["ts"],
                           "entry_ts": trig["ts"], "days": 0, "data": mode,
                           "rank": row["rank"] if side == "long" else row["rank_from_bottom"]}
                    data_modes[mode] += 1
                    open_pos.append(pos)
                    held.add(sym)
                    room -= 1
                    if rest:
                        upd = rules.follow(pos, rest, cfg)
                        pos.update(stop=upd["stop"], best=upd["best"], last_ts=upd["last_ts"])
                        if upd["exit"]:
                            realised += close(pos, day, upd["exit"]["ts"], upd["exit"]["price"], upd["exit"]["reason"])
        # 3. after the close
        for pos in list(open_pos):
            bar, i, _ = day_bars(pos["symbol"])
            if bar is None:
                continue
            s = 1 if pos["side"] == "long" else -1
            reason = None
            if rules.ema_reversed(daily[pos["symbol"]][:i + 1], s, cfg):
                reason = "ema_reversal"
            elif reranked and not rules.still_in_buffer(ranking.get(pos["symbol"]), pos["side"], cfg):
                reason = "rank_exit"
            elif pos["days"] >= cfg["max_hold_days"]:
                reason = "time_stop"
            if reason:
                realised += close(pos, day, day + "T15:30:00", bar["close"], reason) - marks.pop(pos["id"], 0.0)
        # 4. mark to market
        unreal_change = 0.0
        for pos in open_pos:
            bar, _, _ = day_bars(pos["symbol"])
            if bar is None:
                continue
            now_r = rules.pnl_r(pos, bar["close"])
            unreal_change += now_r - marks.get(pos["id"], 0.0)
            marks[pos["id"]] = now_r
        daily_r.append((day, realised + unreal_change))
        if n_day % 20 == 0:
            progress(f"{day}: {len(trades)} trades, {len(open_pos)} open")

    for pos in list(open_pos):           # still open at the end: marked at the last close, listed apart
        bar_list = daily[pos["symbol"]]
        i = bisect.bisect_right(d_index[pos["symbol"]], days[-1]) - 1
        trades.append({"symbol": pos["symbol"], "side": pos["side"], "entry_date": pos["entry_ts"][:10],
                       "exit_date": None, "exit_ts": None, "entry": round(pos["entry"], 2),
                       "exit": round(bar_list[i]["close"], 2), "exit_reason": "open_at_end",
                       "pnl_r": rules.pnl_r(pos, bar_list[i]["close"]), "days": pos["days"], "data": pos["data"],
                       "rank": pos["rank"]})
    closed = [t for t in trades if t["exit_reason"] != "open_at_end"]
    risk_pct = cfg.get("risk_per_trade_pct", 1.0)
    summary = {"book": {**_trade_stats(closed), **_metrics(daily_r, risk_pct)},
               "by_side": {s: _trade_stats([t for t in closed if t["side"] == s]) for s in sides},
               "by_exit": {k: _trade_stats([t for t in closed if t["exit_reason"] == k])
                           for k in sorted({t["exit_reason"] for t in closed})},
               "by_year": {y: _trade_stats([t for t in closed if t["exit_date"][:4] == y])
                           for y in sorted({t["exit_date"][:4] for t in closed})},
               "open_at_end": len(trades) - len(closed), "data": data_modes, "sides": list(sides),
               "limitations": LIMITATIONS}
    if split_date:
        cut = split_date.isoformat()
        summary["by_period"] = {
            "train": {**_trade_stats([t for t in closed if t["entry_date"] < cut]),
                      **_metrics([x for x in daily_r if x[0] < cut], risk_pct)},
            "test": {**_trade_stats([t for t in closed if t["entry_date"] >= cut]),
                     **_metrics([x for x in daily_r if x[0] >= cut], risk_pct)}}
    return {"status": "ok", "summary": summary, "trades": trades,
            "equity": [[day, round(r, 3)] for day, r in daily_r]}


# --- stored runs ---------------------------------------------------------------------------

_lock = threading.Lock()
_running: dict = {}


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS momentum_backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT (datetime('now', 'localtime')),
            status TEXT, params TEXT, summary TEXT, trades TEXT, equity TEXT, progress TEXT)""")
        conn.execute("UPDATE momentum_backtests SET status = 'interrupted' WHERE status = 'running' AND id NOT IN "
                     f"({','.join(str(i) for i in _running) or '0'})")


def start(params: dict) -> dict:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe, spot_instrument

    ensure_table()
    if not _lock.acquire(blocking=False):
        return {"status": "busy", "reason": "a momentum backtest is already running"}
    with get_db() as conn:
        run_id = conn.execute("INSERT INTO momentum_backtests (status, params) VALUES ('running', ?)",
                              (json.dumps(params),)).lastrowid
    _running[run_id] = True

    def work():
        try:
            nifty = spot_instrument("NIFTY")
            nifty_bars = get_candles(nifty["instrument_token"], "day", limit=5000) if nifty else []
            universe = [u for u in fno_universe(date.today()) if u.get("spot_token")]
            if params.get("max_symbols"):
                universe = universe[:int(params["max_symbols"])]

            def progress(msg):
                with get_db() as conn:
                    conn.execute("UPDATE momentum_backtests SET progress = ? WHERE id = ?", (msg, run_id))
            cfg = settings()
            sides = tuple(params.get("sides") or ("long", "short"))
            result = run(date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"]), universe,
                         nifty_bars, lambda u: get_candles(u["spot_token"], "day", limit=5000),
                         lambda u: get_candles(u["spot_token"], "60minute", limit=20000), cfg=cfg, sides=sides,
                         daily_only=bool(params.get("daily_only")),
                         split_date=date.fromisoformat(params["split_date"]) if params.get("split_date") else None,
                         progress=progress)
            with get_db() as conn:
                conn.execute("UPDATE momentum_backtests SET status=?, summary=?, trades=?, equity=?, progress=? "
                             "WHERE id=?", (result["status"], json.dumps(result.get("summary") or
                                                                         {"reason": result.get("reason")}),
                                            json.dumps(result.get("trades", [])), json.dumps(result.get("equity", [])),
                                            "done", run_id))
        except Exception as exc:
            with get_db() as conn:
                conn.execute("UPDATE momentum_backtests SET status='error', progress=? WHERE id=?",
                             (f"{type(exc).__name__}: {exc}"[:300], run_id))
        finally:
            _running.pop(run_id, None)
            _lock.release()

    threading.Thread(target=work, name=f"momentum-backtest-{run_id}", daemon=True).start()
    return {"status": "started", "id": run_id}


def list_runs() -> list[dict]:
    ensure_table()
    with get_db() as conn:
        rows = conn.execute("SELECT id, created_at, status, params, summary, progress FROM momentum_backtests "
                            "ORDER BY id DESC LIMIT 30").fetchall()
    return [{**dict(r), "params": json.loads(r["params"] or "{}"), "summary": json.loads(r["summary"] or "{}")}
            for r in rows]


def get_run(run_id: int, with_trades: bool = False) -> dict | None:
    ensure_table()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM momentum_backtests WHERE id = ?", (run_id,)).fetchone()
    if not r:
        return None
    out = {**dict(r), "params": json.loads(r["params"] or "{}"), "summary": json.loads(r["summary"] or "{}"),
           "equity": json.loads(r["equity"] or "[]")}
    out["trades"] = json.loads(r["trades"] or "[]") if with_trades else None
    return out


def delete_run(run_id: int) -> bool:
    ensure_table()
    if run_id in _running:
        return False
    with get_db() as conn:
        conn.execute("DELETE FROM momentum_backtests WHERE id = ?", (run_id,))
    return True
