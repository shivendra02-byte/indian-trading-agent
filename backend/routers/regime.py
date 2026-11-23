"""Market regime API — current regime classifier + backfill + conditional signal stats."""

from fastapi import APIRouter
from datetime import date

from backend.market_regime import get_current_regime, classify_regime_for_date
from backend.regime_backfill import backfill_regime_at_entry
from backend.signal_performance import compute_signal_performance_by_regime

router = APIRouter(prefix="/api/regime", tags=["regime"])


@router.get("/current")
def current():
    """Today's Nifty regime: BULL / BEAR / SIDEWAYS / HIGH_VOL."""
    return get_current_regime()


@router.get("/engine")
def engine():
    """Regime the trading engine uses (backend/signals/regime.py) on the stored Kite NIFTY daily bars:
    STRONG_UP / STRONG_DOWN / SIDEWAYS / HIGH_VOL / UNSTABLE, with the numbers behind it."""
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.signals.regime import explain_current
    nifty = spot_instrument("NIFTY")
    if not nifty:
        return {"regime": None, "reason": "NIFTY instrument missing (log in to Kite for the instrument master)"}
    bars = get_candles(nifty["instrument_token"], "day", limit=300)
    if not bars:
        return {"regime": None, "reason": "no NIFTY daily candles stored yet"}
    return explain_current(bars)


@router.get("/on")
def on_date(d: str):
    """Regime on a specific date (YYYY-MM-DD)."""
    target = date.fromisoformat(d)
    return classify_regime_for_date(target)


@router.post("/backfill-trades")
def backfill():
    """Tag all paper_trades that are missing regime_at_entry."""
    return backfill_regime_at_entry()


@router.get("/signal-performance")
def signal_perf_by_regime(window_days: int = 180):
    """Per-signal win rates split by market regime."""
    return compute_signal_performance_by_regime(window_days=window_days)
