"""Your own futures trades, with a trailing stop worked out by the system.

You enter the contract, direction, lots, entry price and time and your initial
stop (optionally a target). After every synced bar of the contract
(``global.my_trades.interval``) the trade is re-followed from its entry with the
engine's managed-exit rule (``journal.simulate._run_exit``): the stop goes to
entry (+ ``breakeven_lock_r`` R) once a bar has been ``breakeven_at_r`` in your
favour and closed beyond that level, then trails ``trail_atr`` × ATR behind the
best close from ``trail_start_r``. ATR = the contract's ``atr_period`` ATR on
those bars at the entry, as in the engine. The stop never loosens.

Execution stays with you: when a bar touches the stop the trade becomes
``stop_hit`` and Telegram tells you; you then close it with your real exit
price. Each move of the stop is also sent to Telegram.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime

from backend.db import get_db
from backend.decision.config import get_config

log = logging.getLogger(__name__)
EXCHANGES = {"NFO": ("NFO-FUT", "NSE"), "MCX": ("MCX-FUT", "MCX")}     # segment, calendar exchange
FAR = 1e12
COLUMNS = ("symbol", "exchange", "contract", "instrument_token", "expiry", "lot_size", "point_value", "direction",
           "lots", "entry_price", "entry_time", "initial_stop", "target", "interval", "atr_period", "breakeven_at_r",
           "breakeven_lock_r", "trail_start_r", "trail_atr", "note")
STATE = ("status", "current_stop", "stop_stage", "atr", "last_close", "last_bar_ts", "mfe_r", "open_r",
         "open_pnl", "hit_time", "hit_price", "exit_price", "exit_time", "realised_r", "realised_pnl", "updated_at")


def settings():
    return get_config().global_.my_trades


def ensure_table() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS my_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                symbol TEXT NOT NULL, exchange TEXT NOT NULL, contract TEXT NOT NULL,
                instrument_token INTEGER NOT NULL, expiry TEXT, lot_size REAL, point_value REAL,
                direction TEXT NOT NULL, lots INTEGER NOT NULL,
                entry_price REAL NOT NULL, entry_time TEXT NOT NULL, initial_stop REAL NOT NULL, target REAL,
                interval TEXT NOT NULL, atr_period INTEGER NOT NULL, breakeven_at_r REAL NOT NULL,
                breakeven_lock_r REAL NOT NULL, trail_start_r REAL NOT NULL, trail_atr REAL NOT NULL, note TEXT,
                status TEXT NOT NULL DEFAULT 'open',   -- open / stop_hit / target_hit / closed
                current_stop REAL, stop_stage TEXT, atr REAL, last_close REAL, last_bar_ts TEXT,
                mfe_r REAL, open_r REAL, open_pnl REAL, hit_time TEXT, hit_price REAL,
                exit_price REAL, exit_time TEXT, realised_r REAL, realised_pnl REAL, updated_at TEXT
            );
        """)


# --- contracts ---------------------------------------------------------------------------

def _point_value(exchange: str, name: str, lot_size: float) -> float:
    """₹ per 1 point of price for 1 lot."""
    if exchange == "MCX":
        from backend.signals.fno import mcx_value_per_point
        return float(mcx_value_per_point().get(name) or lot_size or 1)
    return float(lot_size or 1)


def contracts(symbol: str, exchange: str) -> list[dict]:
    """Unexpired futures of an underlying, nearest first, with the one the scanner trades marked."""
    from backend.marketdata.instruments import active_future, futures_for
    segment = EXCHANGES[exchange][0]
    name = symbol.strip().upper()
    active = active_future(name, segment=segment)
    return [{"contract": r["tradingsymbol"], "expiry": r["expiry"], "lot_size": r["lot_size"],
             "instrument_token": r["instrument_token"], "active": bool(active) and r["tradingsymbol"] == active["tradingsymbol"],
             "point_value": _point_value(exchange, name, r["lot_size"])}
            for r in futures_for(name, segment)]


def validate(data: dict, contract_rows: list[dict] | None = None) -> dict:
    cfg = settings()
    exchange = data.get("exchange") or "NFO"
    if exchange not in EXCHANGES:
        raise ValueError("exchange must be NFO or MCX")
    symbol = str(data.get("symbol") or "").strip().upper()
    if not symbol or len(symbol) > 25:
        raise ValueError("symbol is required, e.g. RELIANCE, NIFTY or GOLDM")
    rows = contract_rows if contract_rows is not None else contracts(symbol, exchange)
    if not rows:
        raise ValueError(f"no unexpired {exchange} futures for {symbol} in the instrument list (log in to Kite first)")
    wanted = (data.get("contract") or "").strip().upper()
    row = next((r for r in rows if r["contract"] == wanted), None) if wanted else \
        next((r for r in rows if r["active"]), rows[0])
    if not row:
        raise ValueError(f"{wanted} is not an unexpired {symbol} future")
    direction = data.get("direction")
    if direction not in ("long", "short"):
        raise ValueError("direction must be long or short")
    lots = int(data.get("lots") or 0)
    if lots < 1:
        raise ValueError("lots must be at least 1")
    entry, stop, target = float(data.get("entry_price") or 0), float(data.get("initial_stop") or 0), data.get("target")
    long = direction == "long"
    if entry <= 0 or stop <= 0:
        raise ValueError("entry price and initial stop are required")
    if (stop >= entry) if long else (stop <= entry):
        raise ValueError(f"the stop must be {'below' if long else 'above'} the entry for a {direction}")
    if target is not None and ((target <= entry) if long else (target >= entry)):
        raise ValueError(f"the target must be {'above' if long else 'below'} the entry")
    entry_time = data.get("entry_time") or datetime.now().isoformat(timespec="minutes")
    datetime.fromisoformat(str(entry_time))
    out = {"symbol": symbol, "exchange": exchange, "contract": row["contract"],
           "instrument_token": row["instrument_token"], "expiry": row["expiry"], "lot_size": row["lot_size"],
           "point_value": row["point_value"], "direction": direction, "lots": lots, "entry_price": entry,
           "entry_time": str(entry_time)[:19], "initial_stop": stop, "target": target,
           "interval": data.get("interval") or cfg.interval, "atr_period": cfg.atr_period,
           "breakeven_at_r": float(data.get("breakeven_at_r") or cfg.breakeven_at_r),
           "breakeven_lock_r": float(data.get("breakeven_lock_r") if data.get("breakeven_lock_r") is not None
                                     else cfg.breakeven_lock_r),
           "trail_start_r": float(data.get("trail_start_r") or cfg.trail_start_r),
           "trail_atr": float(data.get("trail_atr") or cfg.trail_atr),
           "note": (data.get("note") or "").strip()[:300] or None}
    if out["interval"] not in ("10minute", "60minute"):
        raise ValueError("interval must be 10minute or 60minute")
    if exchange == "MCX" and out["interval"] != "60minute":
        raise ValueError("MCX trades trail on 60minute bars (10-minute MCX bars are not synced)")
    return out


# --- the trailing stop ----------------------------------------------------------------------

def follow(trade: dict, bars: list[dict], atr: float | None) -> dict:
    """Pure: the trade's state after ``bars`` (bars starting after the entry)."""
    from backend.journal.simulate import _run_exit
    s = 1 if trade["direction"] == "long" else -1
    entry, stop = trade["entry_price"], trade["initial_stop"]
    risk = s * (entry - stop)
    target = trade.get("target") or (entry + s * FAR)
    res = _run_exit(bars, 0, entry, stop, target, s, "9999-12-31", atr, trailing=bool(atr), fill_bar_done=False,
                    trail_start_r=trade["trail_start_r"], trail_atr=trade["trail_atr"],
                    breakeven_at_r=trade["breakeven_at_r"], breakeven_lock_r=trade["breakeven_lock_r"])
    breakeven = entry + s * trade["breakeven_lock_r"] * risk
    mult = trade["lots"] * trade["point_value"]
    last = bars[-1] if bars else None
    if res["status"] == "open":
        current = res["trail_stop"]
        # same label the engine's "Stop now" uses (simulate._run_exit); computed locally only as a fallback
        stage = res.get("stop_state") or ("initial" if abs(current - stop) < 1e-9
                                          else "breakeven" if abs(current - breakeven) < 1e-9 else "trailing")
        close = last["close"] if last else None
        return {"status": "open", "current_stop": round(current, 2), "stop_stage": stage, "atr": atr,
                "last_close": close, "last_bar_ts": last["ts"] if last else None, "mfe_r": res["mfe_r"],
                "open_r": round(s * (close - entry) / risk, 2) if close is not None else None,
                "open_pnl": round(s * (close - entry) * mult, 0) if close is not None else None,
                "locked_r": round(s * (current - entry) / risk, 2)}
    hit_target = res["exit_reason"] == "target"
    return {"status": "target_hit" if hit_target else "stop_hit", "current_stop": None if hit_target else
            round(res["exit_price"], 2), "stop_stage": res["exit_reason"], "atr": atr,
            "last_close": last["close"] if last else None, "last_bar_ts": last["ts"] if last else None,
            "mfe_r": res["mfe_r"], "hit_time": res["exit_ts"], "hit_price": res["exit_price"],
            "open_r": res["pnl_r"], "open_pnl": round(s * (res["exit_price"] - entry) * mult, 0)}


def _bars_and_atr(trade: dict) -> tuple[list[dict], float | None]:
    from backend.marketdata.candles import get_candles
    from backend.signals.indicators import atr
    all_bars = get_candles(trade["instrument_token"], trade["interval"], limit=2000)
    entry = trade["entry_time"]
    before = [b for b in all_bars if b["ts"] < entry]
    after = [b for b in all_bars if b["ts"] >= entry]     # bars that start at/after the entry (the entry bar itself is skipped)
    values = atr(before, trade["atr_period"]) if len(before) > trade["atr_period"] else []
    return after, (values[-1] if values and values[-1] else None)


# --- storage + updates ---------------------------------------------------------------------

def _save(trade_id: int, state: dict) -> None:
    values = {k: state.get(k) for k in STATE if k in state or k == "updated_at"}
    values["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(f"UPDATE my_trades SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                     [*values.values(), trade_id])


def get_trade(trade_id: int) -> dict | None:
    ensure_table()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM my_trades WHERE id = ?", (trade_id,)).fetchone()
    return dict(row) if row else None


def list_trades(limit: int = 300) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM my_trades ORDER BY CASE status WHEN 'closed' THEN 1 ELSE 0 END, entry_time DESC LIMIT ?",
            (limit,))]


def add_trade(data: dict, contract_rows: list[dict] | None = None) -> dict:
    ensure_table()
    row = validate(data, contract_rows)
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO my_trades ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})",
                           [row.get(c) for c in COLUMNS])
        trade_id = cur.lastrowid
    _save(trade_id, {"status": "open", "current_stop": row["initial_stop"], "stop_stage": "initial"})
    update_trades(ids=[trade_id], notify=lambda *a: "skipped")      # no alert for the trade you just entered
    return get_trade(trade_id)


def close_trade(trade_id: int, exit_price: float, exit_time: str | None = None) -> dict:
    trade = get_trade(trade_id)
    if not trade:
        raise ValueError("trade not found")
    if exit_price <= 0:
        raise ValueError("exit price must be positive")
    s = 1 if trade["direction"] == "long" else -1
    risk = s * (trade["entry_price"] - trade["initial_stop"])
    _save(trade_id, {**{k: trade.get(k) for k in STATE}, "status": "closed", "exit_price": exit_price,
                     "exit_time": exit_time or datetime.now().isoformat(timespec="minutes"),
                     "realised_r": round(s * (exit_price - trade["entry_price"]) / risk, 2),
                     "realised_pnl": round(s * (exit_price - trade["entry_price"]) * trade["lots"] * trade["point_value"], 0)})
    return get_trade(trade_id)


def delete_trade(trade_id: int) -> bool:
    ensure_table()
    with get_db() as conn:
        return conn.execute("DELETE FROM my_trades WHERE id = ?", (trade_id,)).rowcount > 0


def open_contracts() -> list[dict]:
    """Contracts of open trades, so the candle sync keeps their bars current."""
    ensure_table()
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT instrument_token, contract, exchange, interval FROM my_trades "
                            "WHERE status = 'open'").fetchall()
    return [dict(r) for r in rows]


def update_trades(exchange: str | None = None, ids: list[int] | None = None, notify=None,
                  interval: str | None = None) -> dict:
    """Re-follow open trades; Telegram when a stop moves or is hit."""
    ensure_table()
    sql, args = "SELECT * FROM my_trades WHERE status = 'open'", []
    if ids:
        sql = f"SELECT * FROM my_trades WHERE id IN ({', '.join('?' for _ in ids)})"
        args = list(ids)
    elif exchange:
        cal = {"NSE": "NFO", "MCX": "MCX"}.get(exchange, exchange)
        sql += " AND exchange = ?"
        args.append(cal)
    if interval and not ids:
        sql += " AND interval = ?"
        args.append(interval)
    with get_db() as conn:
        trades = [dict(r) for r in conn.execute(sql, args)]
    cfg = settings()
    send = notify or (send_alert if cfg.notify_telegram else (lambda *a: "off"))
    moved = hit = 0
    for trade in trades:
        if trade["status"] != "open":
            continue
        bars, atr = _bars_and_atr(trade)
        state = follow(trade, bars, atr)
        previous = trade.get("current_stop")
        _save(trade["id"], state)
        if state["status"] in ("stop_hit", "target_hit"):
            hit += 1
            send(trade, state, "hit")
        elif previous is not None and state["current_stop"] is not None and abs(state["current_stop"] - previous) > 1e-9:
            moved += 1
            send(trade, {**state, "previous_stop": previous}, "moved")
    return {"status": "ok", "checked": len(trades), "moved": moved, "hit": hit}


def build_alert(trade: dict, state: dict, kind: str) -> str:
    name = html.escape(trade["contract"])
    side = trade["direction"]
    if kind == "moved":
        return (f"<b>Trailing stop moved: {name} {side}</b>\n"
                f"Stop {state['previous_stop']:,.2f} → <b>{state['current_stop']:,.2f}</b> ({state['stop_stage']}, "
                f"locks {state.get('locked_r', 0):+.2f}R)\n"
                f"Last close {state['last_close']:,.2f}, open {state.get('open_r') or 0:+.2f}R "
                f"(₹{state.get('open_pnl') or 0:,.0f}). Update your stop order.")
    if state["status"] == "target_hit":
        return (f"<b>Target reached: {name} {side}</b> at {state['hit_price']:,.2f} ({state['hit_time']}).\n"
                f"Close it and enter your exit price on the My Trades page.")
    return (f"<b>Stop hit: {name} {side}</b> at {state['hit_price']:,.2f} ({state['hit_time']}, "
            f"{state['stop_stage'].replace('_', ' ')}), {state.get('open_r') or 0:+.2f}R.\n"
            f"If your broker stop did not fill, exit now, then enter your exit price on the My Trades page.")


def send_alert(trade: dict, state: dict, kind: str) -> str:
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(build_alert(trade, state, kind))
        return "sent"
    except TelegramConfigError as exc:
        return f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("my-trades alert failed: %s", exc)
        return "failed"
