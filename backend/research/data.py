"""Bars for the research strategies and the long daily history they need.

* MCX minis: Kite's continuous daily series of the active mini contract (stored under the
  active contract's token, the same series the live scans use). Kite's continuous series is
  the front contract's prices joined at each expiry — not back-adjusted — so every roll can
  show a small jump; research results carry that as a stated limitation.
* NIFTY / BANKNIFTY: the index's own daily bars (spot). Futures follow the index within a
  small basis; costs are charged as futures, including a roll each month.
* Stocks: the spot daily bars the live scans already keep (F&O stocks + NIFTY 500).

``download_history()`` extends these daily series back ``history.daily_years`` years
(button on the Research Lab page; the evening run also tops up a series that is too short).
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta
from typing import Callable

from backend.marketdata.candles import INTERVALS, REQUEST_GAP_SECONDS, _naive_ist, completed, count_stored, \
    get_candles, save_candles
from backend.marketdata.store import MCX_UNDERLYINGS
from tradingagents.utils.market_calendar import IST, MCX, NSE

INDICES = ("NIFTY", "BANKNIFTY")
PROGRESS: dict = {"status": "idle"}


def group_of(symbol: str) -> str:
    """Which evening/hourly run updates ``symbol``: "mcx" or "nse"."""
    first = symbol.split("/")[0]
    return "mcx" if first in MCX_UNDERLYINGS else "nse"


def kind_of(symbol: str) -> str:
    return "mcx" if symbol in MCX_UNDERLYINGS else "index_futures" if symbol in INDICES else "stock_futures"


def ensure_tables() -> None:
    """Market data tables (fresh installs and tests)."""
    from backend.marketdata.store import ensure_marketdata_tables
    ensure_marketdata_tables()


def series_token(symbol: str) -> tuple[int | None, bool, str]:
    """(token, continuous, exchange) of the daily series used for ``symbol``."""
    from backend.marketdata.instruments import active_future, spot_instrument
    ensure_tables()
    if symbol in MCX_UNDERLYINGS:
        fut = active_future(symbol, segment="MCX-FUT")
        return (fut["instrument_token"] if fut else None), True, MCX
    spot = spot_instrument(symbol)
    return (spot["instrument_token"] if spot else None), False, NSE


def daily_bars(symbol: str, limit: int = 4000) -> list[dict]:
    token, _, _ = series_token(symbol)
    return get_candles(token, "day", limit=limit) if token else []


def point_value(symbol: str) -> tuple[float | None, str | None]:
    """₹ per point per lot and the contract used for it."""
    from backend.marketdata.instruments import active_future
    if symbol in MCX_UNDERLYINGS:
        from backend.signals.fno import mcx_value_per_point
        fut = active_future(symbol, segment="MCX-FUT")
        return mcx_value_per_point().get(symbol), fut["tradingsymbol"] if fut else None
    fut = active_future(symbol)
    if not fut:
        return None, None
    return float(fut["lot_size"]), fut["tradingsymbol"]


def first_stored(token: int) -> datetime | None:
    from backend.db import get_db
    with get_db() as conn:
        row = conn.execute("SELECT MIN(ts) AS ts FROM candles WHERE instrument_token = ? AND interval = 'day'",
                           (int(token),)).fetchone()
    return datetime.fromisoformat(row["ts"]) if row and row["ts"] else None


def backfill_daily(client, token: int, start: datetime, continuous: bool, exchange: str,
                   now: datetime | None = None, sleep: Callable[[float], None] = _time.sleep) -> int:
    """Daily bars from ``start`` to the first stored bar of ``token``."""
    now = _naive_ist(now or datetime.now(IST))
    end = first_stored(token) or now
    if start >= end:
        return 0
    chunk = timedelta(days=INTERVALS["day"][1])
    saved, cursor = 0, start
    while cursor < end:
        stop = min(cursor + chunk, end)
        rows = client.historical_data(token, cursor, stop, "day", continuous=continuous, oi=False)
        saved += save_candles(token, "day", completed(rows or [], "day", exchange, now))
        cursor = stop
        sleep(REQUEST_GAP_SECONDS)
    return saved


def research_symbols() -> list[str]:
    return [*MCX_UNDERLYINGS, *INDICES]


def download_history(client, years: float | None = None, symbols: list[str] | None = None,
                     sleep: Callable[[float], None] = _time.sleep) -> dict:
    """Long daily history for MCX minis (continuous) and the indices (spot)."""
    from backend.research.config import load
    years = years or float((load().get("history") or {}).get("daily_years", 10))
    now = _naive_ist(datetime.now(IST))
    start = now - timedelta(days=int(years * 365))
    names = symbols or research_symbols()
    PROGRESS.update(status="running", started_at=now.isoformat(timespec="seconds"), done=0, total=len(names),
                    current=None, saved=0, errors=[], finished_at=None)
    saved, errors = 0, []
    for i, name in enumerate(names, 1):
        PROGRESS["current"] = name
        token, continuous, exchange = series_token(name)
        if not token:
            errors.append(f"{name}: no instrument")
        else:
            try:
                saved += backfill_daily(client, token, start, continuous, exchange, now, sleep)
            except Exception as exc:
                from backend.brokers.kite import KiteAuthExpired, _is_auth_error
                if _is_auth_error(exc):
                    PROGRESS.update(status="error", message="Kite session expired")
                    raise KiteAuthExpired("Kite session expired. Please connect Kite again.") from exc
                errors.append(f"{name}: {type(exc).__name__}")
        PROGRESS.update(done=i, saved=saved, errors=errors[-20:])
    PROGRESS.update(status="done", current=None, finished_at=datetime.now(IST).replace(tzinfo=None)
                    .isoformat(timespec="seconds"))
    return {"symbols": len(names), "candles_saved": saved, "errors": errors}


def history_status() -> list[dict]:
    out = []
    for name in research_symbols():
        token, continuous, _ = series_token(name)
        bars = daily_bars(name, limit=1) if token else []
        first = first_stored(token) if token else None
        out.append({"symbol": name, "token": token, "continuous": continuous,
                    "bars": count_stored(token, "day") if token else 0,
                    "first": first.date().isoformat() if first else None,
                    "last": bars[-1]["ts"][:10] if bars else None})
    return out
