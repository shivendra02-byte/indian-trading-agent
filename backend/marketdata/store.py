"""SQLite tables for the market data layer."""

from __future__ import annotations

from backend.db import get_db

INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
# Index underlyings we scan (futures 1h, options 10m).
SCANNED_INDICES = ("NIFTY", "BANKNIFTY")
# MCX mini contracts we scan (Kite instrument "name").
MCX_UNDERLYINGS = ("GOLDM", "SILVERM", "CRUDEOILM", "NATGASMINI")
# Kite tradingsymbols of the NSE spot indices (segment INDICES).
INDEX_SPOT_SYMBOLS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE"}


def ensure_marketdata_tables() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS instruments (
                instrument_token INTEGER PRIMARY KEY,
                exchange TEXT NOT NULL,
                segment TEXT NOT NULL,
                tradingsymbol TEXT NOT NULL,
                name TEXT,
                instrument_type TEXT,
                expiry TEXT,
                strike REAL,
                lot_size INTEGER,
                tick_size REAL,
                refreshed_on TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_instruments_name ON instruments (segment, name, expiry);
            CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments (exchange, tradingsymbol);

            CREATE TABLE IF NOT EXISTS candles (
                instrument_token INTEGER NOT NULL,
                interval TEXT NOT NULL,
                ts TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER,
                oi INTEGER,
                PRIMARY KEY (instrument_token, interval, ts)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS fo_ban_list (
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                PRIMARY KEY (trade_date, symbol)
            );

            CREATE TABLE IF NOT EXISTS marketdata_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT,
                detail TEXT
            );
        """)
