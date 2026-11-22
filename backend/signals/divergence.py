"""Regular RSI divergence on daily bars: a trend-reversal warning, alerted on
Telegram whether or not any trade is shortlisted.

Bullish: price makes a lower swing low while RSI makes a higher low (first RSI
low at or below ``bullish_rsi_max``), after a downtrend (close below the trend
EMA). Bearish: the mirror image. Swing points are confirmed 5-bar fractals, so
a divergence is known ``swing_bars`` bars after the second swing: that bar is
the signal bar, and nothing later is used to find it.

Alerts are information only: no AI review, no paper trade, no portfolio limits.
Each one is stored and its outcome tracked (risk = beyond the second swing by
``stop_atr`` ATR; exit at -1R, at +2R or at the end of ``track_bars``), and
``run_study()`` measures the same rule over the stored history so the record
can be judged before trusting it.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timedelta
from typing import Sequence

from backend.db import get_db
from backend.decision.config import get_config
from backend.signals.indicators import atr, ema, rsi, swing_points

log = logging.getLogger(__name__)
TIMEFRAME = "day"
HORIZONS = (5, 10, 20)
TARGET_R = 2.0


def settings():
    return get_config().global_.divergence


def _day(bar: dict) -> str:
    return str(bar["ts"])[:10]


# --- detection -----------------------------------------------------------------------------

def find_divergences(bars: Sequence[dict], cfg=None, require_trend: bool | None = None) -> list[dict]:
    """Every divergence in ``bars`` with its signal bar index ``s`` (the bar that
    confirms the second swing). ``trend_ok`` says whether the prior-trend
    condition held; with ``require_trend`` the others are dropped."""
    cfg = cfg or settings()
    require_trend = cfg.require_trend if require_trend is None else require_trend
    k = cfg.swing_bars
    if len(bars) < max(cfg.rsi_period, cfg.trend_ema) + 2 * k + 5:
        return []
    closes = [b["close"] for b in bars]
    r = rsi(closes, cfg.rsi_period)
    trend = ema(closes, cfg.trend_ema)
    a = atr(bars, 14)
    highs, lows = swing_points(bars, k)
    found = []
    for kind, points in (("bullish", lows), ("bearish", highs)):
        for idx2 in range(1, len(points)):
            i2, p2 = points[idx2]
            s = i2 + k
            if s >= len(bars) or r[i2] is None or a[s] is None or trend[i2] is None:
                continue
            for idx1 in range(idx2 - 1, -1, -1):
                i1, p1 = points[idx1]
                gap = i2 - i1
                if gap < cfg.min_bars_apart:
                    continue
                if gap > cfg.max_bars_apart or r[i1] is None:
                    break
                between = bars[i1 + 1:i2]
                if kind == "bullish":
                    ok = (p2 < p1 and r[i2] - r[i1] >= cfg.min_rsi_gap and r[i1] <= cfg.bullish_rsi_max
                          and all(b["low"] > p2 for b in between))
                    trend_ok = closes[i2] < trend[i2]
                    stop = p2 - cfg.stop_atr * a[s]
                else:
                    ok = (p2 > p1 and r[i1] - r[i2] >= cfg.min_rsi_gap and r[i1] >= cfg.bearish_rsi_min
                          and all(b["high"] < p2 for b in between))
                    trend_ok = closes[i2] > trend[i2]
                    stop = p2 + cfg.stop_atr * a[s]
                if not ok:
                    continue
                if require_trend and not trend_ok:
                    break
                found.append({
                    "kind": kind, "i1": i1, "i2": i2, "s": s,
                    "pivot1_date": _day(bars[i1]), "pivot1_price": p1, "pivot1_rsi": round(r[i1], 1),
                    "pivot2_date": _day(bars[i2]), "pivot2_price": p2, "pivot2_rsi": round(r[i2], 1),
                    "signal_date": _day(bars[s]), "close": closes[s], "atr": a[s], "stop": stop,
                    "trend_ok": trend_ok,
                })
                break   # nearest qualifying first swing only
    return sorted(found, key=lambda d: d["s"])


def outcome(bars: Sequence[dict], sig: dict, track_bars: int) -> dict:
    """Bar by bar after the signal close: -1R at the stop (stop first when a bar
    touches both), the target (``sig["target_r"]``, default 2R), else the close at
    the end of the window. Used by the chart patterns too."""
    s, entry, stop = sig["s"], sig["close"], sig["stop"]
    long = sig["kind"] == "bullish"
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        return {"status": "closed", "exit_reason": "invalid", "result_r": None}
    target_r = sig.get("target_r") or TARGET_R
    target = entry + target_r * risk if long else entry - target_r * risk
    one_r = entry + risk if long else entry - risk
    mfe = mae = 0.0
    hit_1r = False
    rets = {}
    for h in HORIZONS:   # plain move in the signal's favour after h bars, whatever the exit
        if s + h < len(bars):
            move = (bars[s + h]["close"] - entry) / entry * 100
            rets[f"ret_{h}"] = round(move if long else -move, 2)
    last = min(len(bars) - 1, s + track_bars)
    for t in range(s + 1, last + 1):
        b = bars[t]
        fav = (b["high"] - entry) if long else (entry - b["low"])
        adv = (entry - b["low"]) if long else (b["high"] - entry)
        stopped = b["low"] <= stop if long else b["high"] >= stop
        if stopped:
            return {"status": "closed", "exit_reason": "stop", "exit_date": _day(b), "result_r": -1.0,
                    "hit_1r": hit_1r, "hit_2r": False, "mfe_r": round(max(mfe, fav / risk), 2),
                    "mae_r": 1.0, **rets}
        mfe, mae = max(mfe, fav / risk), max(mae, adv / risk)
        if (b["high"] >= one_r) if long else (b["low"] <= one_r):
            hit_1r = True
        if (b["high"] >= target) if long else (b["low"] <= target):
            return {"status": "closed", "exit_reason": "target", "exit_date": _day(b), "result_r": round(target_r, 2),
                    "hit_1r": True, "hit_2r": True, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), **rets}
    if last - s < track_bars:
        return {"status": "open", "hit_1r": hit_1r, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), **rets}
    close = bars[last]["close"]
    result = ((close - entry) if long else (entry - close)) / risk
    return {"status": "closed", "exit_reason": "window_end", "exit_date": _day(bars[last]),
            "result_r": round(result, 2), "hit_1r": hit_1r, "hit_2r": False,
            "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), **rets}


# --- universe --------------------------------------------------------------------------------

def instruments(group: str, today: date) -> list[dict]:
    """[{symbol, segment, token}] for "nse" (F&O stocks + indices) or "mcx" (active mini contracts)."""
    from backend.marketdata.instruments import active_future, fno_universe, spot_instrument
    from backend.marketdata.store import MCX_UNDERLYINGS, SCANNED_INDICES
    cfg = settings()
    out = []
    if group == "nse":
        if cfg.include_indices:
            for name in SCANNED_INDICES:
                spot = spot_instrument(name)
                if spot:
                    out.append({"symbol": name, "segment": "index", "token": spot["instrument_token"]})
        if cfg.include_stocks:
            for stock in fno_universe(today):
                if stock.get("spot_token"):
                    out.append({"symbol": stock["symbol"], "segment": "stock", "token": stock["spot_token"]})
    elif group == "mcx" and cfg.include_mcx:
        for name in MCX_UNDERLYINGS:
            fut = active_future(name, segment="MCX-FUT", today=today)
            if fut:
                out.append({"symbol": name, "segment": "mcx", "token": fut["instrument_token"],
                            "contract": fut["tradingsymbol"]})
    return out


def _liquid(inst: dict, bars: list[dict]) -> bool:
    if inst["segment"] != "stock":
        return True
    from backend.signals.cash_swing import avg_turnover
    return avg_turnover(bars) >= settings().min_turnover_cr * 1e7


# --- storage ---------------------------------------------------------------------------------

COLUMNS = ("symbol", "segment", "token", "contract", "timeframe", "kind", "pivot1_date", "pivot1_price",
           "pivot1_rsi", "pivot2_date", "pivot2_price", "pivot2_rsi", "signal_date", "close", "stop", "atr",
           "trend_ok")
OUTCOME_COLUMNS = ("status", "exit_reason", "exit_date", "result_r", "hit_1r", "hit_2r", "mfe_r", "mae_r",
                   "ret_5", "ret_10", "ret_20")


def ensure_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS divergence_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, segment TEXT NOT NULL, token INTEGER NOT NULL, contract TEXT,
                timeframe TEXT NOT NULL, kind TEXT NOT NULL,
                pivot1_date TEXT, pivot1_price REAL, pivot1_rsi REAL,
                pivot2_date TEXT NOT NULL, pivot2_price REAL, pivot2_rsi REAL,
                signal_date TEXT NOT NULL, close REAL, stop REAL, atr REAL, trend_ok INTEGER,
                status TEXT NOT NULL DEFAULT 'open', exit_reason TEXT, exit_date TEXT, result_r REAL,
                hit_1r INTEGER, hit_2r INTEGER, mfe_r REAL, mae_r REAL, ret_5 REAL, ret_10 REAL, ret_20 REAL,
                alert_status TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE (symbol, timeframe, kind, pivot2_date)
            );
            CREATE INDEX IF NOT EXISTS idx_divergence_signal_date ON divergence_signals (signal_date);
            CREATE TABLE IF NOT EXISTS divergence_studies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                summary TEXT NOT NULL
            );
        """)


def _insert(inst: dict, sig: dict) -> int | None:
    row = {**sig, "symbol": inst["symbol"], "segment": inst["segment"], "token": inst["token"],
           "contract": inst.get("contract"), "timeframe": TIMEFRAME, "trend_ok": int(bool(sig["trend_ok"]))}
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO divergence_signals ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in COLUMNS)})", [row.get(c) for c in COLUMNS])
        return cur.lastrowid if cur.rowcount else None


def _save_outcome(row_id: int, result: dict) -> None:
    values = {c: result.get(c) for c in OUTCOME_COLUMNS}
    for flag in ("hit_1r", "hit_2r"):
        if values[flag] is not None:
            values[flag] = int(bool(values[flag]))
    with get_db() as conn:
        conn.execute(f"UPDATE divergence_signals SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                     [*values.values(), row_id])


def list_signals(limit: int = 200, status: str | None = None) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM divergence_signals"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY signal_date DESC, id DESC LIMIT ?",
                                              [*args, limit]).fetchall()]


# --- live scan -------------------------------------------------------------------------------

def update_outcomes() -> int:
    """Re-simulate every open alert from stored daily candles (stateless)."""
    from backend.marketdata.candles import get_candles
    ensure_tables()
    cfg = settings()
    closed = 0
    for row in list_signals(limit=1000, status="open"):
        bars = get_candles(row["token"], TIMEFRAME, limit=800)
        s = next((i for i, b in enumerate(bars) if _day(b) == row["signal_date"]), None)
        if s is None:
            continue
        sig = {"s": s, "kind": row["kind"], "close": row["close"], "stop": row["stop"]}
        result = outcome(bars, sig, cfg.track_bars)
        _save_outcome(row["id"], result)
        closed += result["status"] == "closed"
    return closed


def run_scan(group: str = "nse", now: datetime | None = None, notify=None) -> dict:
    """Find divergences confirmed on the latest daily bars and alert the new ones."""
    from backend.marketdata.candles import get_candles
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import IST

    cfg = settings()
    if not cfg.enabled:
        return {"status": "skipped", "reason": "divergence alerts disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    ensure_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    updated = update_outcomes()
    fresh_after = (now.date() - timedelta(days=5)).isoformat()
    new = []
    checked = 0
    for inst in instruments(group, now.date()):
        bars = get_candles(inst["token"], TIMEFRAME, limit=400)
        if len(bars) < 80 or _day(bars[-1]) < fresh_after or not _liquid(inst, bars):
            continue
        checked += 1
        for sig in find_divergences(bars, cfg):
            if sig["s"] < len(bars) - 3:          # confirmed in the last 3 bars only (catch-up after a missed day)
                continue
            row_id = _insert(inst, sig)
            if row_id:
                new.append({**sig, **inst, "id": row_id})
    alert = "none"
    if new and cfg.notify_telegram:
        alert = (notify or send_alert)(new, group)
        with get_db() as conn:
            conn.executemany("UPDATE divergence_signals SET alert_status = ? WHERE id = ?",
                             [(alert, d["id"]) for d in new])
    return {"status": "ok", "group": group, "instruments": checked, "new": len(new),
            "bullish": sum(d["kind"] == "bullish" for d in new), "bearish": sum(d["kind"] == "bearish" for d in new),
            "outcomes_closed": updated, "alert": alert}


def _fmt(x: float) -> str:
    return f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"


def build_alert(new: list[dict], group: str, history: dict | None = None) -> str:
    cfg = settings()
    day = max(d["signal_date"] for d in new)
    lines = [f"<b>RSI divergence ({'MCX' if group == 'mcx' else 'NSE'}, daily) {html.escape(day)}</b>",
             "<i>Information only: a reversal warning, not a trade signal. Not reviewed by AI.</i>", ""]
    for d in sorted(new, key=lambda x: (x["kind"], x["symbol"]))[:cfg.max_alerts_per_message]:
        bull = d["kind"] == "bullish"
        word = "low" if bull else "high"
        cmp_price, cmp_rsi = ("&lt;", "&gt;") if bull else ("&gt;", "&lt;")
        name = html.escape(d.get("contract") or d["symbol"])
        lines.append(
            f"{'🟢 Bullish' if bull else '🔴 Bearish'} <b>{name}</b> close {_fmt(d['close'])}: "
            f"price {word} {_fmt(d['pivot2_price'])} {cmp_price} {_fmt(d['pivot1_price'])} ({d['pivot1_date'][5:]}), "
            f"RSI {d['pivot2_rsi']:.0f} {cmp_rsi} {d['pivot1_rsi']:.0f}; "
            f"invalid {'below' if bull else 'above'} {_fmt(d['stop'])}")
    if len(new) > cfg.max_alerts_per_message:
        lines.append(f"… and {len(new) - cfg.max_alerts_per_message} more on the Divergence page.")
    history = history if history is not None else latest_study_line()
    if history:
        lines += ["", f"<i>{html.escape(history)}</i>"]
    return "\n".join(lines)


def send_alert(new: list[dict], group: str) -> str:
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(build_alert(new, group))
        return "sent"
    except TelegramConfigError as exc:
        return f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("divergence alert failed: %s", exc)
        return f"failed: {exc}"


# --- history study ---------------------------------------------------------------------------

def _summary(rows: list[dict]) -> dict:
    closed = [r for r in rows if r.get("result_r") is not None]
    n = len(closed)
    if not n:
        return {"n": 0}
    wins = [r["result_r"] for r in closed if r["result_r"] > 0]
    losses = [-r["result_r"] for r in closed if r["result_r"] <= 0]
    out = {
        "n": n, "win_rate": round(len(wins) / n * 100, 1),
        "hit_1r": round(sum(1 for r in closed if r.get("hit_1r")) / n * 100, 1),
        "hit_2r": round(sum(1 for r in closed if r.get("hit_2r")) / n * 100, 1),
        "avg_r": round(sum(r["result_r"] for r in closed) / n, 3),
        "pf": round(sum(wins) / sum(losses), 2) if sum(losses) else None,
        "small_sample": n < 30,
    }
    for h in HORIZONS:
        vals = [r[f"ret_{h}"] for r in rows if r.get(f"ret_{h}") is not None]
        out[f"avg_ret_{h}"] = round(sum(vals) / len(vals), 2) if vals else None
    return out


def study(series: list[tuple[dict, list[dict]]], cfg=None) -> dict:
    """Measure the rule over stored history. ``series`` = [(instrument, daily bars)].
    Reports with-trend vs against-trend separately and a baseline: the average
    10-day move in the same direction from every day, so you can see whether a
    divergence adds anything over just buying (or shorting) any day."""
    cfg = cfg or settings()
    rows: list[dict] = []
    base_up: list[float] = []
    for inst, bars in series:
        for sig in find_divergences(bars, cfg, require_trend=False):
            res = outcome(bars, sig, cfg.track_bars)
            rows.append({**res, "kind": sig["kind"], "trend_ok": sig["trend_ok"], "segment": inst["segment"],
                         "year": sig["signal_date"][:4]})
        closes = [b["close"] for b in bars]
        base_up += [(closes[i + 10] - closes[i]) / closes[i] * 100 for i in range(len(closes) - 10)]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        trend = "with trend filter" if r["trend_ok"] else "against the filter"
        for key in (r["kind"], f"{r['kind']} | {trend}", f"{r['kind']} | {r['segment']}", f"{r['kind']} | {r['year']}"):
            groups.setdefault(key, []).append(r)
    base = sum(base_up) / len(base_up) if base_up else None
    return {
        "instruments": len(series), "signals": len(rows),
        "first_date": min((b[0]["ts"] for _, b in series if b), default=None),
        "groups": {k: _summary(v) for k, v in sorted(groups.items())},
        "baseline_10d": {"bullish": round(base, 2) if base is not None else None,
                         "bearish": round(-base, 2) if base is not None else None},
        "rule": {"rsi_period": cfg.rsi_period, "bullish_rsi_max": cfg.bullish_rsi_max,
                 "bearish_rsi_min": cfg.bearish_rsi_min, "min_rsi_gap": cfg.min_rsi_gap,
                 "bars_apart": [cfg.min_bars_apart, cfg.max_bars_apart], "trend_ema": cfg.trend_ema,
                 "stop_atr": cfg.stop_atr, "track_bars": cfg.track_bars, "target_r": TARGET_R},
    }


def run_study(today: date | None = None) -> dict:
    """Study over every stored daily series (NSE stocks + indices + MCX contracts) and save it."""
    from backend.marketdata.candles import get_candles
    ensure_tables()
    today = today or date.today()
    series = []
    for group in ("nse", "mcx"):
        for inst in instruments(group, today):
            bars = get_candles(inst["token"], TIMEFRAME, limit=1000)
            if len(bars) >= 120 and _liquid(inst, bars):
                series.append((inst, bars))
    result = study(series)
    result["created_at"] = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute("INSERT INTO divergence_studies (summary) VALUES (?)", (json.dumps(result),))
    return result


def latest_study() -> dict | None:
    ensure_tables()
    with get_db() as conn:
        row = conn.execute("SELECT summary FROM divergence_studies ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["summary"]) if row else None


def latest_study_line() -> str | None:
    s = latest_study()
    if not s:
        return None
    parts = []
    for kind in ("bullish", "bearish"):
        g = s["groups"].get(f"{kind} | with trend filter") or {}
        if g.get("n"):
            parts.append(f"{kind} {g['n']} past signals, {g['win_rate']}% ended positive, avg {g['avg_r']:+.2f}R")
    return ("History (stored data): " + "; ".join(parts)) if parts else None
