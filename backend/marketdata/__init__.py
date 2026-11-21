"""Kite-backed market data layer: instrument master, F&O universe and candles.

Everything here is read-only against Kite (instruments + historical data). The
tables live in the same SQLite DB as the rest of the app and are created by
``ensure_marketdata_tables()``.
"""

from backend.marketdata.store import ensure_marketdata_tables

__all__ = ["ensure_marketdata_tables"]
