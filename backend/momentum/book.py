"""Momentum book — live paper positions on NSE stock futures (advisory only).

Weekly ranking on spot daily bars (stored in ``momentum_ranks``); entries on the
active future's hourly bars after each NSE hourly sync; exits checked on every
hourly bar (stop / trail) and after the close (EMA reversal, leaving the buffer
group at a re-rank, time stop). Every entry and exit is sent to Telegram.
Positions live in ``momentum_positions``; they are paper records, never orders.
The long and the short side switch on separately in ``instruments.momentum``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from backend.db import get_db
from backend.decision.config import get_config
from backend.momentum import rules
from backend.signals.indicators import atr

log = logging.getLogger(__name__)
SIDES = ("long", "short")


def settings() -> dict:
    cfg = get_config()
    try:
        inst = cfg.instrument("momentum")
    except KeyError:
        return {**rules.defaults(), "enabled": False}
    out = {**rules.defaults(), **(inst.model_extra or {}), "enabled": inst.enabled}
    out.update({f"{side}_enabled": on for side, on in side_overrides().items()})
    out.setdefault("risk_per_trade_pct", cfg.global_.risk.risk_per_trade_pct)
    out.setdefault("capital", cfg.global_.capital)
    return out


SIDE_SETTING = "momentum_side_switches"


def side_overrides() -> dict:
    """Long/short switches set on the /momentum page (asked 24 Nov 2026); they override the
    YAML defaults ``long_enabled`` / ``short_enabled`` until reset."""
    from backend.db import get_setting
    try:
        raw = json.loads(get_setting(SIDE_SETTING) or "{}")
    except (ValueError, TypeError):
        return {}
    return {s: bool(raw[s]) for s in SIDES if isinstance(raw, dict) and s in raw}


def set_side(side: str, enabled: bool | None) -> dict:
    """Switch one side on/off; ``None`` goes back to the YAML default. Open positions are
    still followed to their exit either way."""
    from backend.db import set_setting
    if side not in SIDES:
        raise ValueError(f"side must be one of {SIDES}")
    current = side_overrides()
    if enabled is None:
        current.pop(side, None)
    else:
        current[side] = bool(enabled)
    set_setting(SIDE_SETTING, json.dumps(current) if current else None)
    cfg = settings()
    return {"long_enabled": cfg["long_enabled"], "short_enabled": cfg["short_enabled"], "overrides": current}


def ensure_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS momentum_ranks (
            week TEXT NOT NULL, symbol TEXT NOT NULL, as_of TEXT NOT NULL, rank INTEGER, rank_from_bottom INTEGER,
            score REAL, side TEXT, extra TEXT, PRIMARY KEY (week, symbol));
        CREATE TABLE IF NOT EXISTS momentum_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL,
            tradingsymbol TEXT, token INTEGER, lot_size INTEGER, lots INTEGER,
            entry_ts TEXT, entry REAL, stop REAL, initial_stop REAL, atr REAL, best REAL, last_ts TEXT,
            status TEXT NOT NULL DEFAULT 'open', exit_ts TEXT, exit_price REAL, exit_reason TEXT,
            pnl_r REAL, pnl_inr REAL, week TEXT, extra TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')));
        """)


def week_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


# --- ranking ------------------------------------------------------------------------------

def compute_ranking(universe: list[dict], daily_for, nifty_daily: list[dict], as_of: date, cfg: dict) -> list[dict]:
    """Rank eligible stocks on bars up to ``as_of`` (``daily_for(stock)`` → daily bars)."""
    from backend.signals.cash_swing import avg_turnover

    bench = {b["ts"][:10]: b["close"] for b in nifty_daily if b["ts"][:10] <= as_of.isoformat()}
    if not bench:
        return []
    last_day = max(bench)
    rows = []
    for stock in universe:
        bars = [b for b in daily_for(stock) if b["ts"][:10] <= as_of.isoformat()]
        if not bars or bars[-1]["ts"][:10] != last_day:
            continue                       # stale data: never rank on an old close
        if avg_turnover(bars) < cfg["min_turnover_cr"] * 1e7:
            continue
        aligned = [b for b in bars if b["ts"][:10] in bench]
        sc = rules.momentum_score([b["close"] for b in aligned], [bench[b["ts"][:10]] for b in aligned], cfg)
        if sc:
            rows.append({"symbol": stock["symbol"], "score": sc["score"], **sc["parts"]})
    return rules.rank(rows, cfg)


def latest_ranking() -> tuple[str | None, dict[str, dict]]:
    ensure_tables()
    with get_db() as conn:
        week = conn.execute("SELECT MAX(week) FROM momentum_ranks").fetchone()[0]
        if not week:
            return None, {}
        rows = conn.execute("SELECT * FROM momentum_ranks WHERE week = ? ORDER BY rank", (week,)).fetchall()
    out = {}
    for r in rows:
        item = dict(r)
        item.update(json.loads(item.pop("extra") or "{}"))
        out[item["symbol"]] = item
    return week, out


def run_ranking(now: datetime | None = None, force: bool = False) -> dict:
    """Store this week's ranking once (after the first close of the week, or on demand)."""
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe, spot_instrument
    from backend.signals.cash_swing import expected_last_daily_bar

    ensure_tables()
    now = now or datetime.now()
    cfg = settings()
    as_of = expected_last_daily_bar(now)
    week = week_of(as_of)
    current, _ = latest_ranking()
    if current == week and not force:
        return {"status": "skipped", "reason": f"ranking for week {week} already stored"}
    nifty = spot_instrument("NIFTY")
    nifty_bars = get_candles(nifty["instrument_token"], "day", limit=400) if nifty else []
    if not nifty_bars or nifty_bars[-1]["ts"][:10] != as_of.isoformat():
        return {"status": "blocked", "reason": "NIFTY daily bar for the ranking day missing"}
    universe = [u for u in fno_universe(now.date()) if u.get("spot_token")]
    ranked = compute_ranking(universe, lambda u: get_candles(u["spot_token"], "day", limit=400), nifty_bars,
                             as_of, cfg)
    if not ranked:
        return {"status": "blocked", "reason": "no stock had fresh enough data to rank"}
    with get_db() as conn:
        conn.execute("DELETE FROM momentum_ranks WHERE week = ?", (week,))
        conn.executemany(
            "INSERT INTO momentum_ranks (week, symbol, as_of, rank, rank_from_bottom, score, side, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(week, r["symbol"], as_of.isoformat(), r["rank"], r["rank_from_bottom"], r["score"], r["side"],
              json.dumps({k: v for k, v in r.items() if k.startswith("rs_")})) for r in ranked])
    return {"status": "ok", "week": week, "as_of": as_of.isoformat(), "ranked": len(ranked),
            "long": [r["symbol"] for r in ranked if r["side"] == "long"],
            "short": [r["symbol"] for r in ranked if r["side"] == "short"]}


# --- positions ------------------------------------------------------------------------------

def positions(status: str | None = None) -> list[dict]:
    ensure_tables()
    q, args = "SELECT * FROM momentum_positions", ()
    if status:
        q, args = q + " WHERE status = ?", (status,)
    with get_db() as conn:
        rows = conn.execute(q + " ORDER BY id DESC", args).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["extra"] = json.loads(item["extra"] or "{}")
        out.append(item)
    return out


def _close(pos: dict, ts: str, price: float, reason: str) -> dict:
    r = rules.pnl_r(pos, price)
    s = 1 if pos["side"] == "long" else -1
    inr = round(s * (price - pos["entry"]) * (pos["lots"] or 0) * (pos["lot_size"] or 0), 2)
    with get_db() as conn:
        conn.execute("UPDATE momentum_positions SET status='closed', exit_ts=?, exit_price=?, exit_reason=?, "
                     "pnl_r=?, pnl_inr=? WHERE id=?", (ts, round(price, 2), reason, r, inr, pos["id"]))
    return {**pos, "status": "closed", "exit_ts": ts, "exit_price": round(price, 2), "exit_reason": reason,
            "pnl_r": r, "pnl_inr": inr}


def _alert(text: str) -> str:
    if get_config().global_.is_shadow("momentum"):     # shadow book: tracked, no Telegram (23 Nov 2026)
        return "off: shadow strategy"
    try:
        from backend.notifications.telegram import send_message
        send_message(text)
        return "sent"
    except Exception as exc:   # not configured / offline: never fail the job
        return f"not sent: {type(exc).__name__}"


def entry_alert(p: dict) -> str:
    arrow = "LONG" if p["side"] == "long" else "SHORT"
    return (f"📈 Momentum book — {arrow} {p['tradingsymbol']} (paper, rules only)\n"
            f"Entry ≈ {p['entry']:.2f} · stop {p['stop']:.2f} · {p['lots']} lot(s) × {p['lot_size']}\n"
            f"Trail {settings()['trail_atr']:g} ATR behind the best close; exits on EMA reversal, leaving the "
            f"top-{settings()['buffer_n']} group or after {settings()['max_hold_days']} days.\n"
            f"Rank #{p['extra'].get('rank')} · score {p['extra'].get('score')}")


def exit_alert(p: dict) -> str:
    return (f"🔚 Momentum book — exit {p['side']} {p['tradingsymbol']} at {p['exit_price']:.2f} "
            f"({p['exit_reason'].replace('_', ' ')}) · {p['pnl_r']:+.2f}R · ₹{p['pnl_inr']:,.0f}")


def hourly_run(now: datetime | None = None) -> dict:
    """After each NSE hourly sync: follow open positions on their future's hourly bars,
    then look for new entries in the ranked groups."""
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import fno_universe
    from backend.signals import events
    from backend.system_state import is_paused

    ensure_tables()
    now = now or datetime.now()
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "momentum book disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    closed, opened, skipped = [], [], {}
    for pos in positions("open"):
        upd = rules.follow(pos, get_candles(pos["token"], "60minute", limit=200), cfg)
        with get_db() as conn:
            conn.execute("UPDATE momentum_positions SET stop=?, best=?, last_ts=? WHERE id=?",
                         (round(upd["stop"], 2), upd["best"], upd["last_ts"], pos["id"]))
        if upd["exit"]:
            done = _close({**pos, **upd}, upd["exit"]["ts"], upd["exit"]["price"], upd["exit"]["reason"])
            done["alert"] = _alert(exit_alert(done))
            closed.append(done["symbol"])
    week, ranking = latest_ranking()
    if not ranking:
        return {"status": "ok", "closed": closed, "reason": "no ranking yet"}
    held = {p["symbol"] for p in positions("open")}
    universe = {u["symbol"]: u for u in fno_universe(now.date())}
    today = now.date()
    for side in SIDES:
        if not cfg[f"{side}_enabled"]:
            continue
        s = 1 if side == "long" else -1
        room = cfg["max_positions_per_side"] - len([p for p in positions("open") if p["side"] == side])
        group = sorted((r for r in ranking.values() if r["side"] == side),
                       key=lambda r: r["rank"] if side == "long" else r["rank_from_bottom"])
        for row in group:
            if room <= 0:
                break
            sym = row["symbol"]
            u = universe.get(sym)
            if sym in held or not u or not u.get("future_token"):
                continue
            if u.get("ban") is not False:
                skipped[sym] = "F&O ban (or ban list unknown)"
                continue
            daily = get_candles(u["spot_token"], "day", limit=300)
            ok, why = rules.trend_ok(daily, s, cfg)
            if not ok:
                skipped[sym] = why
                continue
            hourly = get_candles(u["future_token"], "60minute", limit=300)
            if not hourly or hourly[-1]["ts"][:10] != today.isoformat():
                skipped[sym] = "no hourly bar today"
                continue
            if cfg["min_oi_value_cr"] and (hourly[-1].get("oi") or 0) * hourly[-1]["close"] < cfg["min_oi_value_cr"] * 1e7:
                skipped[sym] = "open interest too low"
                continue
            trig = rules.entry_trigger(hourly, s, cfg)
            if not trig:
                continue
            window = events.trading_days_after(today, cfg["results_block_days"])
            clear, why = events.results_clear(sym, today, window[-1], today)
            if not clear:
                skipped[sym] = why
                continue
            a = atr(daily, 14)[-1]
            stop = trig["entry"] - s * cfg["stop_atr"] * a
            lots, why = rules.size_lots(cfg["capital"], cfg["risk_per_trade_pct"], trig["entry"], stop, u.get("lot_size"))
            if not lots:
                skipped[sym] = why
                continue
            extra = {"rank": row["rank"] if side == "long" else row["rank_from_bottom"], "score": row["score"],
                     "structure_level": trig["structure_level"], "pullback_extreme": trig["pullback_extreme"]}
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO momentum_positions (symbol, side, tradingsymbol, token, lot_size, lots, entry_ts, "
                    "entry, stop, initial_stop, atr, best, last_ts, week, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, side, u["future_symbol"], u["future_token"], u["lot_size"], lots, trig["ts"],
                     round(trig["entry"], 2), round(stop, 2), round(stop, 2), round(a, 4), trig["entry"],
                     trig["ts"], week, json.dumps(extra)))
                pid = cur.lastrowid
            p = {"id": pid, "symbol": sym, "side": side, "tradingsymbol": u["future_symbol"], "lots": lots,
                 "lot_size": u["lot_size"], "entry": trig["entry"], "stop": stop, "extra": extra}
            _alert(entry_alert(p))
            opened.append(sym)
            held.add(sym)
            room -= 1
    return {"status": "ok", "week": week, "new": opened, "closed": closed, "skipped": skipped}


def daily_run(now: datetime | None = None) -> dict:
    """After the close: weekly ranking (first close of the week), then the end-of-day
    exits: EMA reversal, out of the buffer group, time stop."""
    from backend.marketdata.candles import get_candles
    from backend.system_state import is_paused

    ensure_tables()
    now = now or datetime.now()
    cfg = settings()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "momentum book disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    ranking_result = run_ranking(now)
    _, ranking = latest_ranking()
    closed = []
    for pos in positions("open"):
        hourly = get_candles(pos["token"], "60minute", limit=5)
        if not hourly:
            continue
        price, ts = hourly[-1]["close"], hourly[-1]["ts"]
        s = 1 if pos["side"] == "long" else -1
        reason = None
        spot = _spot_token(pos["symbol"])
        daily = get_candles(spot, "day", limit=300) if spot else []
        if daily and rules.ema_reversed(daily, s, cfg):
            reason = "ema_reversal"
        elif ranking and not rules.still_in_buffer(ranking.get(pos["symbol"]), pos["side"], cfg):
            reason = "rank_exit"
        else:
            held_days = _trading_days_since(pos["entry_ts"][:10], now.date())
            if held_days >= cfg["max_hold_days"]:
                reason = "time_stop"
        if reason:
            done = _close(pos, ts, price, reason)
            done["alert"] = _alert(exit_alert(done))
            closed.append(pos["symbol"])
    return {"status": "ok", "ranking": ranking_result.get("status"), "closed": closed}


def _spot_token(symbol: str) -> int | None:
    from backend.marketdata.instruments import spot_instrument
    inst = spot_instrument(symbol)
    return inst["instrument_token"] if inst else None


def _trading_days_since(start_iso: str, today: date) -> int:
    from tradingagents.utils.market_calendar import is_trading_day
    d, n = date.fromisoformat(start_iso), 0
    while d < today:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def status() -> dict:
    week, ranking = latest_ranking()
    cfg = settings()
    rows = sorted(ranking.values(), key=lambda r: r["rank"])
    closed = positions("closed")
    rs = [p["pnl_r"] for p in closed if p["pnl_r"] is not None]
    return {
        "enabled": cfg["enabled"], "long_enabled": cfg["long_enabled"], "short_enabled": cfg["short_enabled"],
        "side_overrides": side_overrides(),
        "settings": {k: cfg[k] for k in ("top_n", "buffer_n", "max_positions_per_side", "stop_atr", "trail_atr",
                                         "max_hold_days", "lookbacks_days", "skip_days", "min_turnover_cr")},
        "week": week, "long_group": [r for r in rows if r["side"] == "long"],
        "short_group": [r for r in rows if r["side"] == "short"][::-1], "ranked": len(rows),
        "open": positions("open"), "closed": closed[:50],
        "record": {"closed": len(rs), "wins": len([r for r in rs if r > 0]),
                   "total_r": round(sum(rs), 2), "avg_r": round(sum(rs) / len(rs), 3) if rs else None},
    }
