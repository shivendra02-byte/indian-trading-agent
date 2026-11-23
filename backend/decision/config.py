"""Loader for config/decision_engine.yaml — the single source of trading parameters.

* Validated on load (pydantic); a bad file stops the app rather than trading on
  wrong numbers.
* Safety switches are enforced: paper_trading_only must be true and
  live_order_execution, averaging_down and forced_trades must be false.
* ``rule_version`` is a short hash of the file; every journal record stores it.
* ``TRADINGAGENTS_DECISION_CONFIG`` points at another file (tests, experiments).
"""

from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "decision_engine.yaml"
TIMEFRAMES = ("10minute", "30m", "60minute", "1h", "2h", "4h", "day", "week")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemCfg(_Strict):
    paper_trading_only: bool
    live_order_execution: bool
    averaging_down: bool
    forced_trades: bool

    @model_validator(mode="after")
    def _safe(self):
        if not self.paper_trading_only or self.live_order_execution:
            raise ValueError("this build is paper-trading only: live_order_execution must be false")
        if self.averaging_down:
            raise ValueError("averaging_down must be false")
        if self.forced_trades:
            raise ValueError("forced_trades must be false")
        return self


class RiskCfg(_Strict):
    risk_per_trade_pct: float = Field(gt=0, le=2)
    max_open_positions: int = Field(ge=1, le=10)
    max_open_risk_pct: float = Field(gt=0, le=10)
    max_margin_blocked_pct: float = Field(gt=0, le=100)
    max_total_deployed_pct: float = Field(gt=0, le=100)


class LimitsCfg(_Strict):
    max_new_trades_day: int = Field(ge=0)
    max_new_trades_week: int = Field(ge=0)
    max_ai_evaluations_day: int = Field(ge=0)
    # A signal on an instrument that already has a trade open in the same book
    # (segment + symbol) is not taken: live it is journaled as not_selected
    # (shadow-tracked); backtests leave it out of the trade counts.
    one_open_trade_per_instrument: bool = True


class DrawdownCfg(_Strict):
    daily_loss_pct: float = Field(gt=0)
    weekly_loss_pct: float = Field(gt=0)
    peak_drawdown_pct: float = Field(gt=0)


class ScoringCfg(_Strict):
    min_shortlist_score: float = Field(ge=0, le=100)
    min_rr: float = Field(ge=1)
    weights: dict[str, float]

    @model_validator(mode="after")
    def _weights(self):
        expected = {"htf_trend", "setup_quality", "volume_oi", "rr_stop", "regime_fit", "relative_strength"}
        if set(self.weights) != expected:
            raise ValueError(f"scoring.weights must have exactly {sorted(expected)}")
        if abs(sum(self.weights.values()) - 100) > 1e-6:
            raise ValueError("scoring.weights must add up to 100")
        return self


class GeometryCfg(_Strict):
    stop_buffer_atr: float = Field(ge=0)
    min_stop_atr: float = Field(gt=0)
    max_stop_atr: float = Field(gt=0)
    max_rr: float = Field(gt=0)
    no_level_rr: float = Field(gt=0)
    swing_fractal_bars: int = Field(ge=1, le=5)


class RegimeCfg(_Strict):
    high_vol_ratio: float = Field(gt=1)
    adx_period: int = Field(ge=5)
    adx_trend_min: float
    adx_sideways_max: float
    ema_slope_bars: int = Field(ge=1)
    instability_changes: int = Field(ge=1)
    instability_window: int = Field(ge=2)
    min_daily_bars: int = Field(ge=50)


class Permission(_Strict):
    directions: list[Literal["long", "short"]]
    risk_mult: float = Field(gt=0, le=1)
    preferred: bool = False
    min_volume_ratio: float | None = None
    with_trend_only: bool = False


class TrailingCfg(_Strict):
    start_r: float = Field(gt=0)
    trail_atr: float = Field(gt=0)


class ExitCfg(_Strict):
    """The real exit (journal and backtests). ``fixed``: stop, target, time stop.
    ``breakeven``: once ``breakeven_at_r`` in favour (on a closed bar), the stop
    moves to entry + ``breakeven_lock_r`` R. ``breakeven_trail``: breakeven, then
    from ``trail_start_r`` the stop trails ``trail_atr`` ATR behind the best
    close (never below breakeven). The target and time stop stay in place."""
    mode: Literal["fixed", "breakeven", "breakeven_trail"] = "fixed"
    breakeven_at_r: float = Field(default=1.0, gt=0, le=5)
    breakeven_lock_r: float = Field(default=0.0, ge=0, lt=1)
    trail_start_r: float = Field(default=1.0, gt=0, le=5)
    trail_atr: float = Field(default=2.0, gt=0, le=10)

    @model_validator(mode="after")
    def _order(self):
        if self.breakeven_lock_r >= self.breakeven_at_r:
            raise ValueError("breakeven_lock_r must be below breakeven_at_r")
        if self.mode == "breakeven_trail" and self.trail_start_r < self.breakeven_at_r:
            raise ValueError("trail_start_r must be at or above breakeven_at_r")
        return self


class AiPanelCfg(_Strict):
    """AI review of shortlisted trades (one Claude call per trade, at most
    ``limits.max_ai_evaluations_day`` a day). Only reviews: it approves or rejects,
    never changes entry, stop, target or size. Approved trades are the paper book."""
    enabled: bool = True
    min_confidence: int = Field(default=60, ge=0, le=100)   # approve needs at least this
    notify_telegram: bool = True                             # alert every approved trade
    notify_unreviewed: bool = True                           # also alert shortlisted trades the AI limit skipped
    timeout_seconds: int = Field(default=90, ge=10, le=600)


class StopAlertsCfg(_Strict):
    """Telegram "move your stop" messages for trades you were alerted on (asked 23 Nov 2026):
    once when the managed stop leaves the initial stop (breakeven / trailing), then each time it
    moves at least ``min_move_r`` R further. Checked on every tracker run (hourly)."""
    enabled: bool = True
    min_move_r: float = Field(default=0.25, gt=0, le=5)


class OptionDataCfg(_Strict):
    """End-of-day option data (exchange bhavcopy files) for the option spread
    strategy: download, implied volatility and Greeks. Data only — no signals."""
    enabled: bool = True
    symbols: dict[Literal["NSE", "BSE"], list[str]] = {"NSE": ["NIFTY", "BANKNIFTY"], "BSE": ["SENSEX"]}
    risk_free_rate: float = Field(default=0.065, ge=0, le=0.2)      # annual, for Black-76
    update_after: str = Field(default="19:00", pattern=r"^\d{2}:\d{2}$")   # IST, files are out by then
    update_until: str = Field(default="23:30", pattern=r"^\d{2}:\d{2}$")
    retry_minutes: int = Field(default=30, ge=5, le=240)
    recent_days: int = Field(default=7, ge=1, le=30)               # the daily update re-checks this many trading days
    max_history_days: int = Field(default=800, ge=30, le=2000)     # largest backfill range allowed
    request_pause_seconds: float = Field(default=1.0, ge=0, le=30) # be polite to the exchange sites
    nse_url: str = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
    bse_url: str = "https://www.bseindia.com/download/Bhavcopy/Derivative/BhavCopy_BSE_FO_0_0_0_{yyyymmdd}_F_0000.CSV"


class DivergenceCfg(_Strict):
    """Regular RSI divergence on daily bars (trend-reversal warning). Alerts are
    information only: sent whether or not anything is shortlisted, never reviewed
    by the AI, never a paper trade. Every alert is tracked so its record can be judged."""
    enabled: bool = True
    rsi_period: int = Field(default=14, ge=2, le=50)
    swing_bars: int = Field(default=2, ge=1, le=5)            # fractal: k bars each side (2 = 5-bar swing)
    min_bars_apart: int = Field(default=5, ge=2, le=100)      # between the two swing points
    max_bars_apart: int = Field(default=60, ge=5, le=250)
    bullish_rsi_max: float = Field(default=40, ge=0, le=100)  # first low's RSI must be at or below this
    bearish_rsi_min: float = Field(default=60, ge=0, le=100)  # first high's RSI must be at or above this
    min_rsi_gap: float = Field(default=2.0, ge=0, le=50)      # RSI must differ by at least this
    trend_ema: int = Field(default=50, ge=5, le=400)          # reversal needs a prior trend: below (bullish) / above (bearish) this EMA
    require_trend: bool = True
    uptrend_sma: int = Field(default=200, ge=20, le=400)      # "pullback in an uptrend" = close above this SMA
    bullish_context: Literal["any", "pullback_in_uptrend"] = "any"   # live bullish alerts: all, or only pullbacks in an uptrend
    bearish_alert: Literal["all", "open_longs", "off"] = "open_longs"  # bearish = exit warning only for symbols you hold long
    study_split_date: str = Field(default="2025-06-01", pattern=r"^\d{4}-\d{2}-\d{2}$")   # history study: train before, test from
    stop_atr: float = Field(default=0.25, ge=0, le=3)         # risk unit: beyond the second swing by this many ATR
    track_bars: int = Field(default=20, ge=5, le=120)         # outcome window
    min_turnover_cr: float = Field(default=50, ge=0)          # stocks only: 20-day average, crore
    include_stocks: bool = True
    include_indices: bool = True
    include_mcx: bool = True
    notify_telegram: bool = True
    max_alerts_per_message: int = Field(default=25, ge=1, le=100)


class ChartPatternsCfg(_Strict):
    """Trendline, channel and triangle patterns on daily bars, from the chart ideas
    shared on 22 Nov 2026. Information-only alerts like RSI divergence: tracked and
    studied on history before any of them becomes a trade setup."""
    enabled: bool = True
    patterns: list[Literal["trendline_bounce", "trendline_break", "triangle_breakout"]] = [
        "trendline_bounce", "trendline_break", "triangle_breakout"]
    swing_bars: int = Field(default=2, ge=1, le=5)            # 5-bar fractal swings
    max_swings: int = Field(default=6, ge=3, le=12)           # recent swings tried as line anchors
    min_touches: int = Field(default=3, ge=2, le=6)           # swings on the line, the signal bar not counted
    touch_atr: float = Field(default=0.3, ge=0.05, le=2)      # within this many ATR of the line counts as a touch
    break_atr: float = Field(default=0.25, ge=0, le=2)        # a close this far through the line breaks it
    min_span: int = Field(default=20, ge=5, le=250)           # first to last anchor, bars
    max_span: int = Field(default=250, ge=20, le=750)
    flat_tol_pct: float = Field(default=1.5, ge=0.1, le=10)   # triangle: swing highs (lows) within this % form the flat side
    triangle_lookback: int = Field(default=60, ge=20, le=250)
    volume_mult: float = Field(default=1.5, ge=0, le=10)      # breakouts: volume vs the 50-day average (0 = not checked)
    volume_lookback: int = Field(default=50, ge=10, le=200)
    stop_atr: float = Field(default=0.25, ge=0, le=3)
    max_stop_atr: float = Field(default=3.0, ge=0.5, le=10)   # skip if the invalid level is further than this
    target_r: float = Field(default=2.0, ge=1, le=10)
    min_channel_rr: float = Field(default=1.5, ge=0.5, le=10) # channel bounce aims at the other line when that is at least this R
    near_sma: int = Field(default=200, ge=20, le=400)         # recorded: is price near this SMA (the NIFTY 200-DMA idea)
    near_sma_pct: float = Field(default=3.0, ge=0, le=20)
    cooldown_bars: int = Field(default=10, ge=0, le=60)       # same pattern + direction on a symbol at most once in this many bars
    track_bars: int = Field(default=30, ge=5, le=120)
    min_turnover_cr: float = Field(default=50, ge=0)
    include_stocks: bool = True
    include_indices: bool = True
    include_mcx: bool = True
    notify_telegram: bool = True
    max_alerts_per_message: int = Field(default=25, ge=1, le=100)


class SelectionCfg(_Strict):
    """How non-safety technical checks are applied.

    ``strict``: higher-timeframe agreement, confirmation/context timeframes and a
    setup's required regime reject the candidate; regime permission, volume and
    the RSI turn mark it invalid (shadow-tracked only).
    ``scored`` (two-stage selection): only the Stage A safety filters reject;
    those technical checks subtract the penalty points below from the score and
    the candidate stays valid, so ranking decides the shortlist."""
    mode: Literal["strict", "scored"] = "strict"
    htf_against_penalty: float = Field(default=20, ge=0, le=100)
    confirmation_against_penalty: float = Field(default=10, ge=0, le=100)   # per timeframe
    required_regime_penalty: float = Field(default=15, ge=0, le=100)
    # Regimes where the setup × regime permission matrix is hard in both modes:
    # a setup or direction not listed for the regime is never valid (still
    # shadow-tracked for comparison). E.g. [SIDEWAYS] = range setups only there.
    hard_permission_regimes: list[Literal["STRONG_UP", "STRONG_DOWN", "SIDEWAYS", "HIGH_VOL", "UNSTABLE"]] = []


class GlobalCfg(_Strict):
    capital: float = Field(gt=0)
    system: SystemCfg
    risk: RiskCfg
    limits: LimitsCfg
    drawdown: DrawdownCfg
    scoring: ScoringCfg
    geometry: GeometryCfg
    regime: RegimeCfg
    permissions: dict[Literal["STRONG_UP", "STRONG_DOWN", "SIDEWAYS", "HIGH_VOL", "UNSTABLE"],
                      dict[Literal["pullback", "breakout", "range_reversal", "mean_reversion"], Permission]]
    trailing_shadow: TrailingCfg
    exit_management: ExitCfg = ExitCfg()
    selection: SelectionCfg = SelectionCfg()
    ai_panel: AiPanelCfg = AiPanelCfg()
    stop_alerts: StopAlertsCfg = StopAlertsCfg()
    option_data: OptionDataCfg = OptionDataCfg()
    divergence: DivergenceCfg = DivergenceCfg()
    chart_patterns: ChartPatternsCfg = ChartPatternsCfg()


class InstrumentCfg(BaseModel):
    model_config = ConfigDict(extra="allow")   # instrument-specific extras (e.g. value_per_point)
    enabled: bool
    primary_tf: str | None = None
    confirmation_tfs: list[str] = []
    context_tfs: list[str] = []
    context_must_not_oppose: list[str] = []
    htf_score_tf: str | None = None
    directions: list[Literal["long", "short", "long_options"]] = []
    shortlist_per_scan: int = 0
    hold_trading_days: int = 0     # time stop (when time_stop) and the window checked for events
    min_primary_bars: int = 60
    # time_stop: false = no time exit (agreed 23 Nov 2026): the trade ends at the target, the stop
    # or the managed (breakeven -> trailing) stop. Safety exit: futures on the last session before
    # expiry; cash (no expiry) after max_hold_trading_days. Backtests on spot data (futures proxies)
    # also follow a trade for at most max_hold_trading_days.
    time_stop: bool = True
    max_hold_trading_days: int = Field(default=20, ge=1, le=250)
    exit_management: ExitCfg | None = None     # overrides global.exit_management for this instrument

    @model_validator(mode="after")
    def _tfs(self):
        for tf in [self.primary_tf, self.htf_score_tf, *self.confirmation_tfs, *self.context_tfs]:
            if tf is not None and tf not in TIMEFRAMES:
                raise ValueError(f"unknown timeframe {tf!r}")
        return self

    @property
    def scored_htf(self) -> str | None:
        """Timeframe whose trend is scored and must agree for pullback/breakout."""
        return self.htf_score_tf or (self.confirmation_tfs[-1] if self.confirmation_tfs else None)


class StrategyCfg(BaseModel):
    model_config = ConfigDict(extra="allow")
    ema_fast: int = Field(ge=2)
    ema_slow: int = Field(ge=3)
    rsi_period: int = Field(ge=2)
    volume_multiplier: float = Field(ge=0)
    atr_period: int = Field(ge=2)
    min_rr: float = Field(ge=1)
    lookback: int = Field(ge=5)

    @model_validator(mode="after")
    def _emas(self):
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        return self


class DecisionConfig(_Strict):
    version: int
    global_: GlobalCfg = Field(alias="global")
    instruments: dict[str, InstrumentCfg]
    strategies: dict[Literal["pullback", "breakout", "range_reversal", "mean_reversion", "orb"], StrategyCfg]
    rule_version: str = ""

    def instrument(self, name: str) -> InstrumentCfg:
        if name not in self.instruments:
            raise KeyError(f"instrument {name!r} not configured")
        return self.instruments[name]

    def exit_rule(self, name: str | None) -> dict:
        """The real exit for an instrument / journal segment: its own ``exit_management``
        or ``global.exit_management``."""
        inst = self.instruments.get(name) if name else None
        return (inst.exit_management if inst and inst.exit_management else self.global_.exit_management).model_dump()

    def strategy(self, name: str) -> StrategyCfg:
        return self.strategies[name]

    def strategy_value(self, name: str, key: str, default=None):
        return getattr(self.strategies[name], key, None) if hasattr(self.strategies[name], key) \
            else (self.strategies[name].model_extra or {}).get(key, default)


_lock = threading.Lock()
_override: ContextVar[Path | None] = ContextVar("decision_config_override", default=None)
EXPERIMENTS_DIR = DEFAULT_PATH.parent / "experiments"


@contextmanager
def use_config(path: str | Path):
    """Evaluate with another rule set inside this block (backtests). Live scans are unaffected."""
    token = _override.set(Path(path))
    try:
        get_config()  # validate up front
        yield
    finally:
        _override.reset(token)


def available_rule_sets() -> dict[str, Path]:
    """'current' plus every config/experiments/*.yaml."""
    sets = {"current": config_path()}
    if EXPERIMENTS_DIR.exists():
        for p in sorted(EXPERIMENTS_DIR.glob("*.yaml")):
            sets[p.stem] = p
    return sets


def config_path() -> Path:
    return Path(os.getenv("TRADINGAGENTS_DECISION_CONFIG") or DEFAULT_PATH)


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> DecisionConfig:
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    cfg = DecisionConfig.model_validate(data)
    cfg.rule_version = f"v{cfg.version}-{hashlib.sha256(raw).hexdigest()[:8]}"
    return cfg


def get_config() -> DecisionConfig:
    """The current configuration (re-read automatically when the file changes)."""
    path = _override.get() or config_path()
    with _lock:
        return _load(str(path), path.stat().st_mtime)
