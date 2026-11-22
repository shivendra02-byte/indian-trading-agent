"""Trendline, channel and triangle patterns on daily bars (information-only alerts).

From the chart ideas shared on 22 Nov 2026 (ASTERDM, INDUSINDBK, BANKINDIA,
UNOMINDA, Polycab, the NIFTY 200-DMA post):

- ``trendline_bounce``: long off a rising support line through at least
  ``min_touches`` confirmed swing lows (short off a falling resistance line).
  The signal bar touches the line and closes back on the right side with a
  strong close. When the swing highs form a parallel line it is a channel and
  the target is the other line (if it is at least ``min_channel_rr`` R away).
- ``trendline_break``: long when the close breaks above a falling resistance
  line (short: below a rising support line) on volume; stop under the last swing.
- ``triangle_breakout``: ascending triangle (flat swing highs + rising swing
  lows) closing above the flat side on volume; descending triangle mirrored.

Point in time: lines and triangles use only swings confirmed by the signal bar.
Alerts are tracked with the divergence outcome model (-1R at the invalid level,
the target, else the close after ``track_bars``) and studied over the stored
history before any pattern is promoted to a trade setup.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Sequence

from backend.db import get_db
from backend.decision.config import get_config
from backend.signals import divergence
from backend.signals.indicators import atr, sma, swing_points

log = logging.getLogger(__name__)
TIMEFRAME = "day"
LABELS = {"trendline_bounce": "Trendline bounce", "trendline_break": "Trendline break",
          "triangle_breakout": "Triangle breakout"}

_study_lock = threading.Lock()
_study_progress: dict = {"status": "idle"}


def settings():
    return get_config().global_.chart_patterns


_day = divergence._day


class _Series:
    """Indicators and swings computed once per series."""

    def __init__(self, bars: Sequence[dict], cfg):
        self.bars = bars
        self.closes = [b["close"] for b in bars]
        self.atr = atr(bars, 14)
        self.sma = sma(self.closes, cfg.near_sma)
        vols = [float(b.get("volume") or 0) for b in bars]
        n = cfg.volume_lookback
        self.vol_avg: list[float | None] = [None] * len(bars)
        run = 0.0
        for i, v in enumerate(vols):
            run += v
            if i >= n:
                run -= vols[i - n]
            if i >= n:                               # average of the n bars before i
                self.vol_avg[i] = (run - v) / n
        self.vols = vols
        self.highs, self.lows = swing_points(bars, cfg.swing_bars)
        self.high_idx = [i for i, _ in self.highs]
        self.low_idx = [i for i, _ in self.lows]

    def confirmed(self, kind: str, t: int, k: int) -> list[tuple[int, float]]:
        idx, pts = (self.low_idx, self.lows) if kind == "low" else (self.high_idx, self.highs)
        return pts[:bisect_right(idx, t - k)]


def _strong_close(bar: dict, up: bool) -> bool:
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return False
    if up:
        return bar["close"] > bar["open"] and bar["close"] - bar["low"] >= 0.5 * rng
    return bar["close"] < bar["open"] and bar["high"] - bar["close"] >= 0.5 * rng


def _best_line(points, t: int, a: float, rising: bool, support: bool, ser: _Series, cfg):
    """Best unbroken line through recent swing points: (value_at, touches, first, last, slope)."""
    recent = points[-cfg.max_swings:]
    tol, brk = cfg.touch_atr * a, cfg.break_atr * a
    best = None
    for x in range(len(recent)):
        p = recent[x]
        if not (cfg.min_span <= t - p[0] <= cfg.max_span):
            continue
        for y in range(x + 1, len(recent)):
            q = recent[y]
            slope = (q[1] - p[1]) / (q[0] - p[0])
            if (slope > 0) != rising or slope == 0:
                continue

            def at(i, p=p, slope=slope):
                return p[1] + slope * (i - p[0])
            touches = sum(1 for r in recent if r[0] >= p[0] and abs(r[1] - at(r[0])) <= tol)
            if touches < cfg.min_touches:
                continue
            if best and (touches, q[0]) <= (best[1], best[3]):
                continue
            closes = ser.closes[p[0]:t]
            broken = (any(c < at(p[0] + j) - brk for j, c in enumerate(closes)) if support
                      else any(c > at(p[0] + j) + brk for j, c in enumerate(closes)))
            if not broken:
                best = (at, touches, p[0], q[0], slope)
    return best


def _volume_ok(ser: _Series, t: int, cfg) -> tuple[bool, float | None]:
    avg = ser.vol_avg[t]
    if not cfg.volume_mult or not avg:            # indices / missing volume: not tested
        return True, None
    ratio = ser.vols[t] / avg
    return ratio >= cfg.volume_mult, round(ratio, 2)


def _signal(ser: _Series, t: int, pattern: str, long: bool, stop: float, cfg, target_r: float | None = None,
            **detail) -> dict | None:
    close, a = ser.closes[t], ser.atr[t]
    risk = (close - stop) if long else (stop - close)
    if risk <= 0 or risk > cfg.max_stop_atr * a:
        return None
    sma_t = ser.sma[t]
    near = bool(sma_t) and abs(close - sma_t) / close * 100 <= cfg.near_sma_pct
    return {"pattern": pattern, "direction": "long" if long else "short",
            "kind": "bullish" if long else "bearish", "s": t, "signal_date": _day(ser.bars[t]),
            "close": close, "stop": stop, "risk": risk, "atr": a, "target_r": round(target_r or cfg.target_r, 2),
            "detail": {**detail, "near_sma": near}}


def _bounce(ser: _Series, t: int, long: bool, cfg) -> dict | None:
    bar, a, k = ser.bars[t], ser.atr[t], cfg.swing_bars
    if not _strong_close(bar, long):
        return None
    points = ser.confirmed("low" if long else "high", t, k)
    line = _best_line(points, t, a, rising=long, support=long, ser=ser, cfg=cfg)
    if not line:
        return None
    at, touches, first, last, slope = line
    lv, tol = at(t), cfg.touch_atr * a
    if long and not (bar["low"] <= lv + tol and bar["close"] > lv):
        return None
    if not long and not (bar["high"] >= lv - tol and bar["close"] < lv):
        return None
    stop = (min(bar["low"], lv) - cfg.stop_atr * a) if long else (max(bar["high"], lv) + cfg.stop_atr * a)
    # channel: the other swings parallel to the line
    others = [pt for pt in ser.confirmed("high" if long else "low", t, k) if pt[0] >= first]
    channel, target_r, channel_rr = False, None, None
    if others:
        offsets = [pt[1] - at(pt[0]) for pt in others]
        width = max(offsets) if long else min(offsets)
        on_line = sum(1 for o in offsets if abs(o - width) <= tol)
        if on_line >= 2 and (width > 0 if long else width < 0):
            channel = True
            target = lv + width
            risk = (ser.closes[t] - stop) if long else (stop - ser.closes[t])
            channel_rr = round(((target - ser.closes[t]) if long else (ser.closes[t] - target)) / risk, 2) if risk > 0 else None
            if channel_rr and channel_rr >= cfg.min_channel_rr:
                target_r = channel_rr
    return _signal(ser, t, "trendline_bounce", long, stop, cfg, target_r,
                   line=round(lv, 2), touches=touches, first_anchor=_day(ser.bars[first]),
                   last_anchor=_day(ser.bars[last]), slope_per_day=round(slope, 4), channel=channel,
                   channel_rr=channel_rr)


def _last_swing(ser: _Series, t: int, kind: str, k: int) -> float | None:
    pts = ser.confirmed(kind, t, k)
    return pts[-1][1] if pts else None


def _trend_break(ser: _Series, t: int, long: bool, cfg) -> dict | None:
    bar, a, k = ser.bars[t], ser.atr[t], cfg.swing_bars
    if (bar["close"] <= bar["open"]) if long else (bar["close"] >= bar["open"]):
        return None
    # long: break above a falling resistance line; short: break below a rising support line
    points = ser.confirmed("high" if long else "low", t, k)
    line = _best_line(points, t, a, rising=not long, support=not long, ser=ser, cfg=cfg)
    if not line:
        return None
    at, touches, first, last, slope = line
    lv, brk = at(t), cfg.break_atr * a
    if long and not (bar["close"] > lv + brk and ser.closes[t - 1] <= at(t - 1) + brk):
        return None
    if not long and not (bar["close"] < lv - brk and ser.closes[t - 1] >= at(t - 1) - brk):
        return None
    vol_ok, ratio = _volume_ok(ser, t, cfg)
    if not vol_ok:
        return None
    swing = _last_swing(ser, t, "low" if long else "high", k)
    if swing is None:
        return None
    stop = swing - cfg.stop_atr * a if long else swing + cfg.stop_atr * a
    return _signal(ser, t, "trendline_break", long, stop, cfg, line=round(lv, 2), touches=touches,
                   first_anchor=_day(ser.bars[first]), last_anchor=_day(ser.bars[last]),
                   slope_per_day=round(slope, 4), volume_ratio=ratio)


def _triangle(ser: _Series, t: int, long: bool, cfg) -> dict | None:
    bar, a, k = ser.bars[t], ser.atr[t], cfg.swing_bars
    lo = t - cfg.triangle_lookback
    flat = [pt for pt in ser.confirmed("high" if long else "low", t, k) if pt[0] >= lo]
    if len(flat) < 2:
        return None
    level = max(v for _, v in flat) if long else min(v for _, v in flat)
    tol = level * cfg.flat_tol_pct / 100
    cluster = [pt for pt in flat if abs(pt[1] - level) <= tol]
    if len(cluster) < 2 or cluster[-1][0] - cluster[0][0] < 10:
        return None
    side = [pt for pt in ser.confirmed("low" if long else "high", t, k) if pt[0] > cluster[0][0]]
    if len(side) < 2 or not ((side[-1][1] > side[-2][1]) if long else (side[-1][1] < side[-2][1])):
        return None
    if long and not (bar["close"] > level and ser.closes[t - 1] <= level):
        return None
    if not long and not (bar["close"] < level and ser.closes[t - 1] >= level):
        return None
    vol_ok, ratio = _volume_ok(ser, t, cfg)
    if not vol_ok:
        return None
    stop = side[-1][1] - cfg.stop_atr * a if long else side[-1][1] + cfg.stop_atr * a
    return _signal(ser, t, "triangle_breakout", long, stop, cfg, level=round(level, 2),
                   flat_touches=len(cluster), since=_day(ser.bars[cluster[0][0]]), volume_ratio=ratio)


DETECTORS = {"trendline_bounce": _bounce, "trendline_break": _trend_break, "triangle_breakout": _triangle}


def find_patterns(bars: Sequence[dict], cfg=None, start: int = 0) -> list[dict]:
    """Every pattern signal from bar ``start`` on, with a per pattern+direction cooldown."""
    cfg = cfg or settings()
    if len(bars) < max(60, cfg.min_span + 10):
        return []
    ser = _Series(bars, cfg)
    out, last_at = [], {}
    for t in range(max(start, cfg.min_span + 5, 20), len(bars)):
        if ser.atr[t] is None:
            continue
        for pattern in cfg.patterns:
            for long in (True, False):
                key = (pattern, long)
                if key in last_at and t - last_at[key] < cfg.cooldown_bars:
                    continue
                sig = DETECTORS[pattern](ser, t, long, cfg)
                if sig:
                    out.append(sig)
                    last_at[key] = t
    return out


# --- storage -------------------------------------------------------------------------------

COLUMNS = ("symbol", "segment", "token", "contract", "pattern", "direction", "signal_date", "close", "stop",
           "target_r", "risk", "detail")


def ensure_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pattern_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, segment TEXT NOT NULL, token INTEGER NOT NULL, contract TEXT,
                pattern TEXT NOT NULL, direction TEXT NOT NULL, signal_date TEXT NOT NULL,
                close REAL, stop REAL, target_r REAL, risk REAL, detail TEXT,
                status TEXT NOT NULL DEFAULT 'open', exit_reason TEXT, exit_date TEXT, result_r REAL,
                hit_1r INTEGER, hit_2r INTEGER, mfe_r REAL, mae_r REAL, ret_5 REAL, ret_10 REAL, ret_20 REAL,
                alert_status TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE (symbol, pattern, direction, signal_date)
            );
            CREATE INDEX IF NOT EXISTS idx_pattern_signals_date ON pattern_signals (signal_date);
            CREATE TABLE IF NOT EXISTS pattern_studies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                summary TEXT NOT NULL
            );
        """)


def _insert(inst: dict, sig: dict) -> int | None:
    row = {**sig, "symbol": inst["symbol"], "segment": inst["segment"], "token": inst["token"],
           "contract": inst.get("contract"), "detail": json.dumps(sig.get("detail") or {})}
    with get_db() as conn:
        cur = conn.execute(f"INSERT OR IGNORE INTO pattern_signals ({', '.join(COLUMNS)}) "
                           f"VALUES ({', '.join('?' for _ in COLUMNS)})", [row.get(c) for c in COLUMNS])
        return cur.lastrowid if cur.rowcount else None


def _save_outcome(row_id: int, result: dict) -> None:
    values = {c: result.get(c) for c in divergence.OUTCOME_COLUMNS}
    for flag in ("hit_1r", "hit_2r"):
        if values[flag] is not None:
            values[flag] = int(bool(values[flag]))
    with get_db() as conn:
        conn.execute(f"UPDATE pattern_signals SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                     [*values.values(), row_id])


def list_signals(limit: int = 200, status: str | None = None) -> list[dict]:
    ensure_tables()
    sql, args = "SELECT * FROM pattern_signals", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY signal_date DESC, id DESC LIMIT ?", [*args, limit])]
    for r in rows:
        r["detail"] = json.loads(r["detail"] or "{}")
    return rows


def update_outcomes() -> int:
    from backend.marketdata.candles import get_candles
    ensure_tables()
    cfg = settings()
    closed = 0
    for row in list_signals(limit=1000, status="open"):
        bars = get_candles(row["token"], TIMEFRAME, limit=800)
        s = next((i for i, b in enumerate(bars) if _day(b) == row["signal_date"]), None)
        if s is None:
            continue
        sig = {"s": s, "kind": "bullish" if row["direction"] == "long" else "bearish", "close": row["close"],
               "stop": row["stop"], "target_r": row["target_r"]}
        result = divergence.outcome(bars, sig, cfg.track_bars)
        _save_outcome(row["id"], result)
        closed += result["status"] == "closed"
    return closed


# --- live scan -----------------------------------------------------------------------------

def _instruments(group: str, today: date) -> list[dict]:
    cfg = settings()
    rows = divergence.instruments(group, today)
    keep = {"stock": cfg.include_stocks, "index": cfg.include_indices, "mcx": cfg.include_mcx}
    return [r for r in rows if keep.get(r["segment"], False)]


def _liquid(inst: dict, bars: list[dict]) -> bool:
    if inst["segment"] != "stock":
        return True
    from backend.signals.cash_swing import avg_turnover
    return avg_turnover(bars) >= settings().min_turnover_cr * 1e7


def run_scan(group: str = "nse", now: datetime | None = None, notify=None) -> dict:
    """Patterns confirmed on the latest daily bars; one Telegram digest for the new ones."""
    from backend.marketdata.candles import get_candles
    from backend.system_state import is_paused
    from tradingagents.utils.market_calendar import IST

    cfg = settings()
    if not cfg.enabled:
        return {"status": "skipped", "reason": "chart pattern alerts disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    ensure_tables()
    now = now or datetime.now(IST).replace(tzinfo=None)
    updated = update_outcomes()
    fresh_after = (now.date() - timedelta(days=5)).isoformat()
    new, checked = [], 0
    for inst in _instruments(group, now.date()):
        bars = get_candles(inst["token"], TIMEFRAME, limit=400)
        if len(bars) < 80 or _day(bars[-1]) < fresh_after or not _liquid(inst, bars):
            continue
        checked += 1
        for sig in find_patterns(bars, cfg, start=max(0, len(bars) - 3 - cfg.cooldown_bars)):
            if sig["s"] < len(bars) - 3:
                continue
            row_id = _insert(inst, sig)
            if row_id:
                new.append({**sig, **inst, "id": row_id})
    alert = "none"
    if new and cfg.notify_telegram:
        alert = (notify or send_alert)(new, group)
        with get_db() as conn:
            conn.executemany("UPDATE pattern_signals SET alert_status = ? WHERE id = ?", [(alert, d["id"]) for d in new])
    by_pattern: dict[str, int] = {}
    for d in new:
        by_pattern[d["pattern"]] = by_pattern.get(d["pattern"], 0) + 1
    return {"status": "ok", "group": group, "instruments": checked, "new": len(new), "by_pattern": by_pattern,
            "outcomes_closed": updated, "alert": alert}


def build_alert(new: list[dict], group: str, history: str | None = None) -> str:
    cfg = settings()
    day = max(d["signal_date"] for d in new)
    lines = [f"<b>Chart patterns ({'MCX' if group == 'mcx' else 'NSE'}, daily) {html.escape(day)}</b>",
             "<i>Information only: not a trade signal, not reviewed by AI.</i>", ""]
    for d in sorted(new, key=lambda x: (x["pattern"], x["direction"], x["symbol"]))[:cfg.max_alerts_per_message]:
        long = d["direction"] == "long"
        det = d.get("detail") or {}
        name = html.escape(d.get("contract") or d["symbol"])
        extra = []
        if det.get("channel"):
            extra.append(f"channel, target {d['target_r']:.1f}R")
        if det.get("touches"):
            extra.append(f"{det['touches']} touches since {det.get('first_anchor', '')[5:]}")
        if det.get("level"):
            extra.append(f"level {divergence._fmt(det['level'])}")
        if det.get("volume_ratio"):
            extra.append(f"volume {det['volume_ratio']}x")
        if det.get("near_sma"):
            extra.append(f"near {cfg.near_sma}-day SMA")
        lines.append(f"{'🟢' if long else '🔴'} {LABELS[d['pattern']]} {'long' if long else 'short'} <b>{name}</b> "
                     f"close {divergence._fmt(d['close'])}, invalid {'below' if long else 'above'} "
                     f"{divergence._fmt(d['stop'])}" + (f" ({'; '.join(extra)})" if extra else ""))
    if len(new) > cfg.max_alerts_per_message:
        lines.append(f"… and {len(new) - cfg.max_alerts_per_message} more on the Chart Patterns page.")
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
        log.warning("chart pattern alert failed: %s", exc)
        return f"failed: {exc}"


# --- history study ---------------------------------------------------------------------------

def study(series: list[tuple[dict, list[dict]]], cfg=None, progress=None) -> dict:
    """Every pattern over the stored history, by pattern and direction, channel vs plain
    line, near the 200-day SMA or not, instrument type and year, plus the baseline."""
    cfg = cfg or settings()
    rows, base = [], []
    for n, (inst, bars) in enumerate(series, 1):
        for sig in find_patterns(bars, cfg):
            res = divergence.outcome(bars, sig, cfg.track_bars)
            det = sig["detail"]
            rows.append({**res, "pattern": sig["pattern"], "direction": sig["direction"], "segment": inst["segment"],
                         "year": sig["signal_date"][:4], "channel": bool(det.get("channel")),
                         "near_sma": bool(det.get("near_sma"))})
        closes = [b["close"] for b in bars]
        base += [(closes[i + 10] - closes[i]) / closes[i] * 100 for i in range(len(closes) - 10)]
        if progress:
            progress(n, len(series), inst["symbol"])
    groups: dict[str, list[dict]] = {}
    for r in rows:
        head = f"{r['pattern']} {r['direction']}"
        keys = [head, f"{head} | {r['segment']}", f"{head} | {r['year']}",
                f"{head} | {'near' if r['near_sma'] else 'away from'} {cfg.near_sma} SMA"]
        if r["pattern"] == "trendline_bounce":
            keys.append(f"{head} | {'channel' if r['channel'] else 'line only'}")
        for key in keys:
            groups.setdefault(key, []).append(r)
    b = sum(base) / len(base) if base else None
    return {
        "instruments": len(series), "signals": len(rows),
        "first_date": min((s[1][0]["ts"] for s in series if s[1]), default=None),
        "groups": {k: divergence._summary(v) for k, v in sorted(groups.items())},
        "baseline_10d": {"long": round(b, 2) if b is not None else None, "short": round(-b, 2) if b is not None else None},
        "rule": settings().model_dump(),
    }


def run_study(today: date | None = None) -> dict:
    from backend.marketdata.candles import get_candles
    ensure_tables()
    today = today or date.today()
    series = []
    for group in ("nse", "mcx"):
        for inst in _instruments(group, today):
            bars = get_candles(inst["token"], TIMEFRAME, limit=1000)
            if len(bars) >= 120 and _liquid(inst, bars):
                series.append((inst, bars))
    _study_progress.update(status="running", done=0, total=len(series), current=None)
    result = study(series, progress=lambda n, total, sym: _study_progress.update(done=n, current=sym))
    result["created_at"] = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute("INSERT INTO pattern_studies (summary) VALUES (?)", (json.dumps(result),))
    return result


def start_study() -> dict:
    """The study takes a minute or two over ~200 stocks, so it runs in the background."""
    if not _study_lock.acquire(blocking=False):
        return {"status": "busy"}

    def work():
        try:
            run_study()
            _study_progress.update(status="done", current=None)
        except Exception as exc:
            _study_progress.update(status="error", message=f"{type(exc).__name__}: {exc}"[:200])
        finally:
            _study_lock.release()

    _study_progress.update(status="starting", done=0, total=0, message=None)
    threading.Thread(target=work, daemon=True, name="pattern-study").start()
    return {"status": "started"}


def study_progress() -> dict:
    return dict(_study_progress)


def latest_study() -> dict | None:
    ensure_tables()
    with get_db() as conn:
        row = conn.execute("SELECT summary FROM pattern_studies ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row["summary"]) if row else None


def latest_study_line() -> str | None:
    s = latest_study()
    if not s:
        return None
    parts = []
    for key, g in s["groups"].items():
        if "|" not in key and g.get("n", 0) >= 30:
            parts.append(f"{key.replace('_', ' ')} {g['avg_r']:+.2f}R over {g['n']}")
    return ("History: " + "; ".join(parts)) if parts else None
