"""Live prices from Kite's WebSocket feed (asked 24 Nov 2026).

Subscribes to the contracts of open trades (My futures trades, journal trades you marked Taken,
and system paper trades you were alerted on) in ``ltp`` mode and keeps the latest traded price of
each in memory. The Today page's P&L reads it (``price()``) so open trades move with the market.

It never closes, fills or moves a trade: stops, targets and trailing stops still run on completed
bars, as in the backtests (agreed 24 Nov 2026). Nothing is stored.

Kite's official client (``KiteTicker``) pins an old ``autobahn`` with known CVEs, so this module
speaks the documented protocol itself with the ``websockets`` library already in requirements:

* connect ``wss://ws.kite.trade?api_key=…&access_token=…``
* send ``{"a": "subscribe", "v": [tokens]}`` and ``{"a": "mode", "v": ["ltp", [tokens]]}``
* binary frames: int16 packet count, then per packet an int16 length and the packet; an ``ltp``
  packet is 8 bytes (uint32 token, int32 price in paise). A 1-byte frame is a heartbeat; text frames
  are JSON (errors, order updates) and are only logged.

Runs in one background thread (``feed.start()`` from the app lifespan, off with
``TRADINGAGENTS_LIVE_FEED=off``). Connects only while the system isn't paused, Kite is logged in
today, an exchange of a wanted contract is open and there is something to follow; otherwise it
stays disconnected. Reconnects with a back-off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)

URL = "wss://ws.kite.trade"
# Kite exchange segment (token & 0xff) → price divisor: CDS 3, BCD 6; everything else paise
DIVISORS = {3: 10_000_000.0, 6: 10_000.0}
CALENDAR_EXCHANGE = {"NSE": "NSE", "NFO": "NSE", "BSE": "NSE", "BFO": "NSE", "MCX": "MCX"}


def parse_frame(data: bytes) -> list[tuple[int, float]]:
    """(token, last price) pairs from one binary frame; [] for a heartbeat or a short frame."""
    if len(data) < 2:
        return []
    count = struct.unpack(">H", data[:2])[0]
    out, pos = [], 2
    for _ in range(count):
        if pos + 2 > len(data):
            break
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        packet = data[pos + 2:pos + 2 + length]
        pos += 2 + length
        if len(packet) < 8:
            continue
        token, raw = struct.unpack(">Ii", packet[:8])
        out.append((token, raw / DIVISORS.get(token & 0xFF, 100.0)))
    return out


def feed_enabled() -> bool:
    return os.getenv("TRADINGAGENTS_LIVE_FEED", "on").lower() not in ("off", "0", "false", "no")


def settings():
    from backend.decision.config import get_config
    return get_config().global_.live_feed


def wanted_contracts() -> dict[int, str]:
    """instrument token → exchange of every open trade worth a live price."""
    from backend.db import get_db
    out: dict[int, str] = {}
    with get_db() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "my_trades" in tables:
            for r in conn.execute("SELECT instrument_token, exchange FROM my_trades WHERE status = 'open'"):
                out[int(r[0])] = r[1]
        if "trade_journal" in tables:
            for r in conn.execute("SELECT instrument_token, exchange FROM trade_journal WHERE track_status = 'open' "
                                  "AND (user_action = 'taken' OR decision IN ('approved', 'shortlisted'))"):
                out[int(r[0])] = r[1]
    return out


def _open_exchanges(contracts: dict[int, str], now: datetime | None = None) -> set[str]:
    from tradingagents.utils.market_calendar import is_exchange_open
    found = set()
    for ex in set(contracts.values()):
        cal = CALENDAR_EXCHANGE.get((ex or "").upper())
        if cal and is_exchange_open(cal, now):
            found.add(cal)
    return found


def tokens_to_follow(contracts: dict[int, str], open_exchanges: set[str], limit: int) -> list[int]:
    """Contracts whose exchange is open, capped at ``limit`` (pure)."""
    keep = [t for t, ex in sorted(contracts.items()) if CALENDAR_EXCHANGE.get((ex or "").upper()) in open_exchanges]
    return keep[:limit]


class LiveFeed:
    def __init__(self):
        self._prices: dict[int, tuple[float, float]] = {}      # token → (price, epoch seconds)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.subscribed: list[int] = []
        self.last_tick: float | None = None
        self.last_error: str | None = None
        self.state = "stopped"

    # ── prices ──
    def update(self, pairs: list[tuple[int, float]], at: float | None = None):
        at = at or time.time()
        with self._lock:
            for token, price in pairs:
                self._prices[token] = (price, at)
        if pairs:
            self.last_tick = at

    def price(self, token: int | None, max_age: float | None = None) -> tuple[float, float] | None:
        """(price, epoch) when a live price no older than ``max_age`` seconds exists."""
        if token is None:
            return None
        with self._lock:
            got = self._prices.get(int(token))
        if not got:
            return None
        if max_age is None:
            try:
                max_age = settings().max_age_seconds
            except Exception:
                max_age = 120
        return got if time.time() - got[1] <= max_age else None

    def status(self) -> dict:
        return {"state": self.state, "connected": self.connected, "subscribed": len(self.subscribed),
                "last_tick": datetime.fromtimestamp(self.last_tick).isoformat(timespec="seconds") if self.last_tick else None,
                "last_error": self.last_error, "enabled": feed_enabled()}

    # ── thread ──
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="kite-live-feed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _wanted(self) -> tuple[list[int], str]:
        """(tokens, reason when none)."""
        from backend.brokers.kite import get_kite_status
        from backend.system_state import is_paused
        try:
            cfg = settings()
        except Exception as exc:                       # bad config: stay quiet rather than crash the thread
            return [], f"config: {exc}"
        if not cfg.enabled:
            return [], "switched off in the config"
        if is_paused():
            return [], "system paused"
        if not get_kite_status().get("connected_today"):
            return [], "Kite not logged in today"
        contracts = wanted_contracts()
        if not contracts:
            return [], "no open trades"
        tokens = tokens_to_follow(contracts, _open_exchanges(contracts), cfg.max_instruments)
        return tokens, "" if tokens else "market closed"

    async def _main(self):
        backoff = 5
        while not self._stop.is_set():
            try:
                tokens, why = await asyncio.to_thread(self._wanted)
            except Exception as exc:
                tokens, why = [], f"error: {exc}"
            if not tokens:
                self.state = f"idle ({why})"
                await self._sleep(15)
                continue
            try:
                await self._session(tokens)
                backoff = 5
            except Exception as exc:
                self.last_error = f"{datetime.now():%H:%M:%S} {type(exc).__name__}: {exc}"[:300]
                log.warning("live feed: %s", self.last_error)
                self.state = "reconnecting"
                await self._sleep(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                self.connected = False
                self.subscribed = []
        self.state = "stopped"

    async def _sleep(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            await asyncio.sleep(1)

    async def _session(self, tokens: list[int]):
        from websockets.asyncio.client import connect
        from backend.brokers import kite
        url = f"{URL}?api_key={kite._api_key()}&access_token={kite._access_token()}"
        refresh = settings().refresh_tokens_seconds
        async with connect(url, open_timeout=15, max_size=2 ** 22) as ws:
            self.connected, self.state, self.last_error = True, "live", None
            await self._subscribe(ws, tokens, [])
            next_check = time.time() + refresh
            while not self._stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    msg = None
                if isinstance(msg, bytes):
                    self.update(parse_frame(msg))
                elif isinstance(msg, str):
                    try:
                        body = json.loads(msg)
                        if body.get("type") == "error":
                            self.last_error = str(body.get("data"))[:300]
                    except ValueError:
                        pass
                if time.time() >= next_check:
                    next_check = time.time() + refresh
                    wanted, why = await asyncio.to_thread(self._wanted)
                    if not wanted:
                        self.state = f"idle ({why})"
                        return
                    if set(wanted) != set(self.subscribed):
                        await self._subscribe(ws, wanted, self.subscribed)

    async def _subscribe(self, ws, tokens: list[int], old: list[int]):
        gone = sorted(set(old) - set(tokens))
        new = sorted(set(tokens) - set(old))
        if gone:
            await ws.send(json.dumps({"a": "unsubscribe", "v": gone}))
        if new:
            await ws.send(json.dumps({"a": "subscribe", "v": new}))
            await ws.send(json.dumps({"a": "mode", "v": ["ltp", new]}))
        self.subscribed = list(tokens)


feed = LiveFeed()
