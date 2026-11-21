"""Indian market calendar — NSE and MCX trading days, sessions and holidays.

NSE (equity, F&O): 09:15–15:30 IST on trading days.
MCX (commodities): morning 09:00–17:00 and evening 17:00–23:30 IST, or 23:55
while the US is on standard time. On many NSE holidays MCX closes only its
morning session, so the two exchanges are tracked separately.

Weekly off is Saturday and Sunday for both. Weekend special sessions (Union
Budget, Muhurat trading) are listed separately and are *not* trading days for
the scanner: open paper trades are tracked, no new entries are taken.

Holiday lists must be updated every December from the official circulars:
  NSE 2026 — F&O circular FAOP71777
  MCX 2026 — circular MCX/TRD/636/2025 (15 Dec 2025)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytz

IST = pytz.timezone("Asia/Kolkata")
NEW_YORK = pytz.timezone("America/New_York")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_MARKET_OPEN = time(9, 0)
POST_MARKET_CLOSE = time(16, 0)

MCX_MORNING_OPEN = time(9, 0)
MCX_EVENING_OPEN = time(17, 0)
MCX_CLOSE_US_DST = time(23, 30)
MCX_CLOSE_US_STD = time(23, 55)

NSE = "NSE"
MCX = "MCX"
EXCHANGES = (NSE, MCX)

# Weekday NSE trading holidays (equity + equity derivatives).
NSE_HOLIDAYS = {
    # 2025
    date(2025, 2, 26),  # Mahashivratri
    date(2025, 3, 14),  # Holi
    date(2025, 3, 31),  # Id-Ul-Fitr (Ramadan)
    date(2025, 4, 10),  # Shri Mahavir Jayanti
    date(2025, 4, 14),  # Dr. Ambedkar Jayanti
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 1),   # Maharashtra Day
    date(2025, 8, 15),  # Independence Day
    date(2025, 8, 27),  # Ganesh Chaturthi
    date(2025, 10, 1),  # Mahatma Gandhi Jayanti / Dussehra
    date(2025, 10, 2),  # Dussehra
    date(2025, 10, 21), # Diwali Laxmi Pujan
    date(2025, 10, 22), # Diwali Balipratipada
    date(2025, 11, 5),  # Guru Nanak Jayanti
    date(2025, 12, 25), # Christmas
    # 2026 — official list (15 weekday holidays)
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Holi
    date(2026, 3, 26),  # Shri Ram Navami
    date(2026, 3, 31),  # Shri Mahavir Jayanti
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 5, 28),  # Bakri Id
    date(2026, 6, 26),  # Muharram
    date(2026, 9, 14),  # Ganesh Chaturthi
    date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    date(2026, 10, 20), # Dussehra
    date(2026, 11, 10), # Diwali Balipratipada
    date(2026, 11, 24), # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25), # Christmas
}

# Weekday MCX holidays: date -> (morning_open, evening_open).
MCX_HOLIDAYS: dict[date, tuple[bool, bool]] = {
    date(2026, 1, 1): (True, False),    # New Year
    date(2026, 1, 15): (False, True),   # Maharashtra municipal elections
    date(2026, 1, 26): (False, False),  # Republic Day
    date(2026, 3, 3): (False, True),    # Holi
    date(2026, 3, 26): (False, True),   # Shri Ram Navami
    date(2026, 3, 31): (False, True),   # Shri Mahavir Jayanti
    date(2026, 4, 3): (False, True),    # Good Friday
    date(2026, 4, 14): (False, True),   # Ambedkar Jayanti
    date(2026, 5, 1): (False, True),    # Maharashtra Day
    date(2026, 5, 28): (False, True),   # Bakri Id
    date(2026, 6, 26): (False, True),   # Muharram
    date(2026, 9, 14): (False, True),   # Ganesh Chaturthi
    date(2026, 10, 2): (False, False),  # Gandhi Jayanti
    date(2026, 10, 20): (False, True),  # Dussehra
    date(2026, 11, 10): (False, True),  # Diwali Balipratipada
    date(2026, 11, 24): (False, True),  # Guru Nanak Jayanti
    date(2026, 12, 25): (False, False), # Christmas
}

# Additional NSE closures that fall on days MCX trades (e.g. 2026-01-15).
NSE_EXTRA_CLOSURES = {
    date(2026, 1, 15),  # Maharashtra municipal elections
}

# Weekend sessions. Not trading days for new entries.
SPECIAL_SESSIONS = {
    date(2026, 2, 1): "Union Budget",
    date(2026, 11, 8): "Muhurat trading",
}

# Calendar years whose official lists have been loaded above.
CALENDAR_YEARS = {NSE: {2025, 2026}, MCX: {2026}}

HOLIDAY_NAMES = {
    date(2026, 1, 1): "New Year", date(2026, 1, 15): "Maharashtra municipal elections",
    date(2026, 1, 26): "Republic Day", date(2026, 3, 3): "Holi",
    date(2026, 3, 26): "Shri Ram Navami", date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday", date(2026, 4, 14): "Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day", date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram", date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Gandhi Jayanti", date(2026, 10, 20): "Dussehra",
    date(2026, 11, 10): "Diwali Balipratipada", date(2026, 11, 24): "Guru Nanak Jayanti",
    date(2026, 12, 25): "Christmas",
}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _as_ist(dt: datetime | None) -> datetime:
    if dt is None:
        return _now_ist()
    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


# --- NSE (backward-compatible API) -------------------------------------------

def is_trading_day(d: date = None) -> bool:
    """True if NSE trades on this date (weekday, not a holiday)."""
    if d is None:
        d = _now_ist().date()
    if d.weekday() >= 5:
        return False
    return d not in NSE_HOLIDAYS and d not in NSE_EXTRA_CLOSURES


def is_market_open(dt: datetime = None) -> bool:
    """True while the NSE regular session is open."""
    dt = _as_ist(dt)
    return is_trading_day(dt.date()) and MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def next_trading_day(d: date = None) -> date:
    if d is None:
        d = _now_ist().date()
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def previous_trading_day(d: date = None) -> date:
    if d is None:
        d = _now_ist().date()
    candidate = d - timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def get_market_session(dt: datetime = None) -> str:
    """NSE session: pre_market, open, closing_hour, post_market or closed."""
    dt = _as_ist(dt)
    if not is_trading_day(dt.date()):
        return "closed"
    t = dt.time()
    if t < PRE_MARKET_OPEN:
        return "closed"
    if t < MARKET_OPEN:
        return "pre_market"
    if t <= time(14, 30):
        return "open"
    if t <= MARKET_CLOSE:
        return "closing_hour"
    if t <= POST_MARKET_CLOSE:
        return "post_market"
    return "closed"


# --- MCX ----------------------------------------------------------------------

def mcx_evening_close(d: date) -> time:
    """MCX evening close: 23:30 during US daylight saving, else 23:55."""
    noon_ny = NEW_YORK.localize(datetime(d.year, d.month, d.day, 12, 0))
    return MCX_CLOSE_US_DST if noon_ny.dst() else MCX_CLOSE_US_STD


def mcx_sessions_open(d: date) -> tuple[bool, bool]:
    """(morning_open, evening_open) for MCX on this date."""
    if d.weekday() >= 5:
        return (False, False)
    return MCX_HOLIDAYS.get(d, (True, True))


# --- Generic, exchange-aware API ----------------------------------------------

def session_windows(exchange: str, d: date) -> list[tuple[time, time]]:
    """Trading windows (IST) for an exchange on a date; empty when closed."""
    exchange = exchange.upper()
    if exchange == NSE:
        return [(MARKET_OPEN, MARKET_CLOSE)] if is_trading_day(d) else []
    if exchange == MCX:
        morning, evening = mcx_sessions_open(d)
        close = mcx_evening_close(d)
        if morning and evening:
            return [(MCX_MORNING_OPEN, close)]
        windows = []
        if morning:
            windows.append((MCX_MORNING_OPEN, MCX_EVENING_OPEN))
        if evening:
            windows.append((MCX_EVENING_OPEN, close))
        return windows
    raise ValueError(f"Unknown exchange: {exchange}")


def is_exchange_trading_day(exchange: str, d: date | None = None) -> bool:
    return bool(session_windows(exchange, d or _now_ist().date()))


def is_exchange_open(exchange: str, dt: datetime | None = None) -> bool:
    dt = _as_ist(dt)
    t = dt.time()
    return any(start <= t < end for start, end in session_windows(exchange, dt.date()))


def calendar_loaded(exchange: str, year: int) -> bool:
    return year in CALENDAR_YEARS.get(exchange.upper(), set())


def holiday_name(d: date) -> str | None:
    return HOLIDAY_NAMES.get(d) or SPECIAL_SESSIONS.get(d)


def upcoming_closures(exchange: str, start: date | None = None, days: int = 45) -> list[dict]:
    """Weekday full or partial closures in the next ``days`` days."""
    exchange = exchange.upper()
    start = start or _now_ist().date()
    out = []
    for i in range(days + 1):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if exchange == NSE and not is_trading_day(d):
            out.append({"date": d.isoformat(), "name": holiday_name(d), "closed": "full"})
        elif exchange == MCX and d in MCX_HOLIDAYS:
            morning, evening = MCX_HOLIDAYS[d]
            closed = "full" if not (morning or evening) else ("morning" if evening else "evening")
            out.append({"date": d.isoformat(), "name": holiday_name(d), "closed": closed})
    return out


def exchange_status(exchange: str, dt: datetime | None = None) -> dict:
    """Snapshot for the dashboard's market-status strip."""
    dt = _as_ist(dt)
    exchange = exchange.upper()
    windows = session_windows(exchange, dt.date())
    closures = upcoming_closures(exchange, dt.date() + timedelta(days=1), days=60)
    return {
        "exchange": exchange,
        "open_now": is_exchange_open(exchange, dt),
        "trading_day": bool(windows),
        "sessions_today": [[s.strftime("%H:%M"), e.strftime("%H:%M")] for s, e in windows],
        "holiday_today": holiday_name(dt.date()) if dt.weekday() < 5 and not windows else None,
        "next_closure": closures[0] if closures else None,
        "calendar_loaded_this_year": calendar_loaded(exchange, dt.year),
        "calendar_loaded_next_year": calendar_loaded(exchange, dt.year + 1),
    }
