# Indian Market Trading Agent

AI-powered multi-agent trading decision system for Indian markets (NSE/BSE).
Built on [TradingAgents](https://github.com/TauricResearch/TradingAgents) framework with LangGraph.

## Architecture

```
frontend/ (Next.js 16 + Tailwind + shadcn/ui + Open Sans)  :3000
    |
backend/ (FastAPI + WebSocket)                             :8000
    |
tradingagents/ (LangGraph multi-agent pipeline)
    |
yfinance + RSS feeds (NSE/BSE data + news)
```

## Quick Start

```bash
# 1. Install Python deps
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # exact, tested versions (Python 3.11.5+)
pip install -e .                  # or: pip install -e ".[kite]" for Zerodha Kite sync

# 2. Configure API keys (2 options)
# Option A: via .env
echo 'ANTHROPIC_API_KEY=your_key' > .env
# Option B: via Settings page in the UI (stored in SQLite, takes priority)

# 3. Start backend
uvicorn backend.app:app --reload --port 8000

# 4. Start frontend (separate terminal)
cd frontend && npm install && npm run dev

# 5. Open http://localhost:3000
```

## Information Architecture

The UI is organized by daily workflow (not by technical feature):

```
🏠 Today              — Daily starting page with auto-loaded top picks + workflow guide

DISCOVER
  ✨ Top Picks         — AI-free unified recommendation engine (combines all signals)
  📡 Market Scan       — Gap / Volume / Breakout scanner
  🎯 Strategies        — S/R, Pivot Points, Cyclical Patterns (seasonality, sector rotation)
  📰 News Feed         — Aggregated Indian market news (RSS + yfinance, customizable)

ANALYZE
  🔍 Deep Analysis     — AI-powered 10-agent pipeline (paid ~Rs.15-60)
  📊 Charts            — Candlestick charts with volume

VALIDATE
  🏆 Performance       — Historical win rate of each strategy (FREE)
  🧪 Simulation        — Paper trading + historical recommender backtest (FREE)
  🧠 Learning Insights — Pattern analysis of YOUR past trades (FREE, no ML)
  📈 Signal Performance — Per-signal win rate + auto-tune recommender (FREE)
  🎯 Verdict Calibration — Is the daily verdict actually predictive? (FREE)
  ⚖️ Confidence Calibration — Brier score: are stated probabilities honest? (FREE)
  👁️ Shadow Trades       — Counterfactual auto-tracking of skipped picks (FREE)
  🧠 Memory Admin        — Inspect + prune agent BM25 memories (FREE)
  🔬 AI Backtest       — Run AI pipeline on past dates (paid)
  📋 My Trades         — History with P&L tracking + "Teach the agent" reflection

⚙️ Settings           — API keys (UI), LLM provider switcher, model selection, cost guide
```

## Features

### FREE Features (no API cost)
- **Top Picks / Recommendations** — Combines 10+ signals (gap, volume, breakout, S/R, RSI, cyclical, trend) into ranked trade ideas with success probability %
- **Market Scanner** — Finds stocks with gap up/down, volume spikes, 20-day breakouts
- **Support/Resistance** — S1-S3 / R1-R3 levels from historical price action + daily Pivot Points
- **Cyclical Patterns** — Monthly seasonality, day-of-week patterns, sector rotation (9 sectors)
- **Seasonal Backtest** — "Buy in January, sell in March" strategy testing with pure price math
- **Performance Tracker** — Historical win rate of Gap/Volume/Breakout/S/R-Bounce strategies over 30/60/90 days
- **News Feed** — Aggregated from 7 Indian RSS sources (MoneyControl, ET, LiveMint, Business Standard, NDTV Profit) + yfinance search queries. Fully customizable (add/remove/edit sources).
- **Charts** — Interactive candlestick with volume (TradingView lightweight-charts)
- **Watchlist** — Persistent across sessions (SQLite)
- **Workflow Guide** — 3-step visual guide on Dashboard

### Paid Features (AI API cost)
- **Deep Analysis** — Full 10-agent pipeline with customization:
  - **Analyst selection** — pick Market/Social/News/Fundamentals (min 1)
  - **Research depth** — Shallow (1 round) / Medium (2) / Deep (3)
  - **Output language** — English / Hindi
  - **Live cost estimate** before you run
  - **Stats tracking** — exact tokens, LLM calls, cost in Rs./USD after completion
  - Per-model breakdown of token usage
  - Cost: ~Rs.8-70 depending on config (Haiku+Sonnet mix)
- **AI Backtest** — Runs Deep Analysis on past dates with P&L tracking + optional learning

### Agent Learning System
- **Memory persistence** — All 5 agent memories (Bull, Bear, Trader, Judge, Portfolio Manager) auto-save to `~/.tradingagents/memory/*.json`
- **Auto-load** on every backend startup
- **Reflect & Remember from History** — After a trade closes, click "Log P&L" on the trade:
  - Enter entry/exit prices → calculates P&L
  - Optional "Teach the agent" checkbox → runs reflection
  - 5 agents reflect on what went right/wrong
  - Lessons added to memory, used in future analyses
  - Shows "Agent Memory: N lessons learned" badge on History page
- **Important**: The LLM itself is NOT fine-tuned — memory is keyword-retrieved via BM25 and injected into future prompts as context

### Paper Trading Simulation (FREE)
- **Paper Trades** — Click "Track" on any Top Pick or Recommendation → opens virtual position at current market price
- **Multi-horizon tracking** — auto-fetches actual prices at 1/3/5/10 trading days later
- **Captures full context** — source (recommendation/scanner/manual), strategy name, triggered signals, confidence, score
- **Performance by Strategy** — compare win rates across sources (e.g., Recommendations vs Manual vs AI Analysis)
- **Historical Recommender Backtest** — replay recommendation engine on past 60 days, measure actual 5-day outcomes
  - Validates engine quality before risking real money
  - FREE (pure price math, no AI API calls)
  - Persists to `recommender_backtests` table so you can re-view past runs

### Learning Insights (FREE, no ML)
- **Pattern analysis** of all closed trades (paper + real with logged P&L)
- 7 insight categories:
  - Signal Type (STRONG BUY vs BUY vs SELL win rates)
  - Confidence Level (are HIGH picks actually better?)
  - Strategy (which source performs best for you)
  - Seasonality (which months you trade well)
  - Ticker (which stocks you read well/poorly)
  - Indicator (which specific signals work for your style)
  - Direction (long vs short bias)
- Classifies each insight: Strong edge / Works / Average / Marginal / Avoid
- Generates specific actionable tips per insight
- Minimum 3 closed trades required; best with 20+
- **Purpose**: helps YOU learn to filter the agent's output, not train the agent itself

### Settings Management (UI-based)
- **API Keys** — Store multiple provider keys in local SQLite (priority over .env)
  - Supports Anthropic, OpenAI, Google, xAI, DeepSeek, Qwen
  - Masked display (shows only last 4 chars)
  - Test button to verify keys work
  - Delete button (falls back to .env if set)
- **LLM Provider** — Switch between providers from UI
- **Model Selection** — Pick deep-think and quick-think models per provider
- **News Sources** — Enable/disable RSS feeds, add custom URLs, edit search queries
- **API Cost Guide** — Compares costs across providers

## Project Structure

### `tradingagents/` — Core AI Pipeline
- **agents/analysts/** — Market, News, Social, Fundamentals analysts (tool-calling agents)
- **agents/researchers/** — Bull & Bear researchers (debate agents)
- **agents/managers/** — Research Manager (debate judge) + Portfolio Manager (final decision)
- **agents/trader/** — Trader (converts plan to transaction proposal)
- **agents/risk_mgmt/** — Aggressive, Conservative, Neutral risk debaters
- **agents/utils/memory.py** — BM25 memory with **disk persistence** (JSON per agent)
- **agents/utils/** — LangChain `@tool` definitions, agent states
- **dataflows/** — Vendor-abstracted data layer (`interface.py` routes to yfinance/alpha_vantage/nse)
- **graph/** — LangGraph orchestration: `setup.py`, `trading_graph.py`, `propagation.py`, `signal_processing.py`, `reflection.py`
- **llm_clients/** — Multi-provider LLM factory (Anthropic, OpenAI, Google, xAI, DeepSeek, Qwen, etc.)
- **utils/** — `ticker.py` (NSE/BSE normalization), `market_calendar.py` (IST hours, holidays)
- **default_config.py** — Default configuration

### `backend/` — FastAPI REST + WebSocket API
- **app.py** — FastAPI app with CORS, loads API keys from DB + applies LLM config at startup
- **db.py** — SQLite schema: watchlist, analysis_history (with P&L cols), backtest_runs, backtest_trades, settings, paper_trades, recommender_backtests
- **ws.py** — WebSocket connection manager
- **models.py** — Pydantic request/response models (includes analyst selection, depth, language)
- **settings_manager.py** — API key storage + LLM config management (DB priority over .env)
- **stats_callback.py** — LangChain callback to track tokens + cost per analysis (supports Anthropic/OpenAI/Google/Gemini model pricing)
- **scanner.py** — Market scanner engine (Gap / Volume / Breakout detection)
- **stock_list.py** — 200+ NSE stock ticker → company name mapping for typeahead search
- **recommender.py** — Unified recommendation engine (combines ALL signals with weights)
- **performance.py** — Historical strategy performance measurement
- **cyclical.py** — Monthly seasonality, day-of-week, sector rotation analysis
- **backtest_engine.py** — AI-based backtesting loop with P&L calculation (paid)
- **simulation.py** — Paper trading + historical recommender backtest (FREE)
- **insights.py** — Learning insights: pattern analysis across trade categories
- **news_sources.py** — RSS + yfinance aggregator, customizable feeds
- **routers/**
  - `analysis.py` — `POST /api/analysis/run` (with customization) + `WS /api/analysis/ws/{task_id}` (streams heartbeats + stats) + `PUT /{task_id}/pnl` (with reflect option) + `GET /memory/stats`
  - `market_data.py` — Quotes, charts, indicators, fundamentals, news, market status, stock search
  - `watchlist.py` — Watchlist CRUD
  - `strategies.py` — S/R, Pivot Points, Cyclical endpoints
  - `scanner.py` — Market scanner (synchronous)
  - `recommender.py` — Unified recommendations
  - `performance.py` — Strategy win rate measurement
  - `backtest.py` — AI-based backtest run + WebSocket streaming (paid)
  - `simulation.py` — Paper trade CRUD + refresh prices + historical recommender backtest (FREE)
  - `insights.py` — Learning insights (pattern analysis on past trades)
  - `settings.py` — API key CRUD + LLM provider management
  - `news.py` — News feed aggregator + custom source management

### `frontend/` — Next.js 16 Trading Terminal
- **Font**: Open Sans (via `next/font/google`)
- **Theme**: Light mode (default)
- **app/**
  - `layout.tsx` — Root layout with sidebar, Open Sans font
  - `page.tsx` — Dashboard (Today): greeting + market status + auto-loaded top picks + sector heatmap + workflow guide + watchlist + quick actions (incl. position size calc)
  - `recommendations/page.tsx` — Unified recommendation engine UI with "Track" button to open paper trades
  - `analysis/page.tsx` — Run AI analysis with customization panel, live heartbeat, stats card, position size calc
  - `analysis/[id]/page.tsx` — View past analysis
  - `scanner/page.tsx` — Market scanner with 3 tabs (Gap has UP/DOWN filter)
  - `strategies/page.tsx` — Strategy Hub (cards)
  - `strategies/support-resistance/page.tsx` — S/R + Pivot Points
  - `strategies/cyclical/page.tsx` — Cyclical patterns with insights, entry/exit windows, seasonal backtest
  - `performance/page.tsx` — Historical strategy win rates
  - `simulation/page.tsx` — Paper trading + historical recommender backtest (FREE)
  - `insights/page.tsx` — Learning insights (pattern analysis on your trades)
  - `backtest/page.tsx` — AI-based backtest with P&L tracking (paid)
  - `charts/page.tsx` — Candlestick charts
  - `history/page.tsx` — Past analyses with Open/Closed tabs, "Log P&L" button per row, agent memory badge
  - `settings/page.tsx` — API keys, LLM config, system config, cost guide
  - `news/page.tsx` — News Feed tab + Customize Sources tab
- **components/**
  - `layout/Sidebar.tsx` — Grouped navigation (Discover/Analyze/Validate) with hints
  - `dashboard/` — MarketOverview, Watchlist (with alerts), RecentAnalyses, TodayPicks (with Track button + watchlist match highlights), WorkflowGuide, QuickActions (incl. Position Calc), SectorHeatmap
  - `analysis/` — AgentProgress, ReportPanel (controlled tabs, auto-advance), DebateView, DecisionCard, AnalysisOptions (customization panel), StatsCard (token + cost display)
  - `history/PnLDialog.tsx` — P&L entry dialog with "Mark as Open" + "Teach the agent" checkbox
  - `settings/` — ApiKeysManager, LLMSettings
  - `TickerSearch.tsx` — Typeahead stock search (name → ticker)
  - `PositionSizeCalculator.tsx` — Standalone dialog: capital, risk %, entry/SL/target → shares + R:R
  - `HelpSection.tsx` — Collapsible FAQ on every page
  - `NextStep.tsx` — Workflow continuity buttons across pages
- **lib/**
  - `api.ts` — REST + WebSocket client (includes stats, news, memory endpoints)
  - `store.ts` — Zustand global store for analysis state (stats + heartbeat + survives page navigation)
  - `types.ts` — Shared TypeScript types (incl. the analysis/backtest WebSocket events and `AnalysisStatsSummary`)
  - `utils.ts` — `cn()` class merging + `errorMessage(e, fallback)` for typed catch blocks
  - `help-content.ts` — All help text centralized

## Agent Pipeline Flow

```
Market Analyst → Social Analyst → News Analyst → Fundamentals Analyst
    ↓  (any subset can be enabled/disabled via UI)
Bull Researcher ←→ Bear Researcher (debate, 1-3 rounds)
    ↓
Research Manager (judge: Buy/Sell/Hold + trading plan)
    ↓
Trader (entry/SL/target/position size/time horizon)
    ↓
Aggressive ←→ Conservative ←→ Neutral (risk debate, 1-3 rounds)
    ↓
Portfolio Manager (final: Strong Buy/Buy/Hold/Sell/Short)
    ↓
[Optional after trade closes]
Reflect & Remember → 5 agent memories updated with P&L outcome
```

## Configuration

Default config in `tradingagents/default_config.py`. Runtime overrides in SQLite (via Settings UI):

| Key | Default | Description |
|-----|---------|-------------|
| `llm_provider` | `anthropic` | LLM provider (switch via Settings) |
| `deep_think_llm` | `claude-sonnet-4-20250514` | Complex reasoning (2 agents) |
| `quick_think_llm` | `claude-haiku-4-5-20251001` | Fast tasks (13 agents) |
| `market` | `india` | Market region |
| `default_exchange` | `NSE` | Default exchange |
| `trading_style` | `short_term` | short_term / swing / positional |
| `max_debate_rounds` | `1` | Bull/bear debate iterations (overridable per request) |
| `output_language` | `English` | Report language (English/Hindi) |
| `data_vendors` | `yfinance` | Data source |
| `dry_run` | `True` | Order safety — never executes real orders |
| `memory_dir` | `~/.tradingagents/memory` | Where BM25 memories are persisted |

### Per-Analysis Overrides (from `/analysis` page)
- `analysts: list[str]` — subset of ["market", "social", "news", "fundamentals"]
- `max_debate_rounds: int` — 1 (Shallow), 2 (Medium), or 3 (Deep)
- `max_risk_discuss_rounds: int` — same as debate
- `output_language: str` — "English" or "Hindi"

## Cost & Performance

Actual costs measured by `StatsCallback` per analysis:

| Config | LLM Calls | Tokens | Est. Cost | Duration |
|--------|-----------|--------|-----------|----------|
| Minimal (Market only, Shallow, English) | ~6 | ~20K | ~Rs.8-12 | 1 min |
| Balanced (all 4 analysts, Shallow) | ~17 | ~200K | ~Rs.15-25 | 2-3 min |
| Full (all 4 analysts, Deep) | ~25 | ~400K | ~Rs.50-70 | 4-6 min |
| + Reflect after P&L | +5 | +15K | +Rs.5-10 | +30s |

## Data Layer

`dataflows/interface.py` routes all data calls through `route_to_vendor()`:

- **yfinance** (default) — Free, NSE with `.NS` suffix. OHLCV, fundamentals, news
- **alpha_vantage** — Alternative with API key, auto-fallback
- **nse** — FII/DII activity, bulk deals, delivery % (stubs)

Ticker normalization: `RELIANCE` → `RELIANCE.NS`, `NIFTY50` → `^NSEI`

### News Sources (configurable via `/news` page)
Default RSS feeds:
- MoneyControl (Top News + Business)
- Economic Times (Markets + IPO)
- LiveMint Markets
- Business Standard Markets
- NDTV Profit

Plus custom yfinance search queries (default: "Nifty Sensex", "RBI monetary policy", "FII DII activity", "India GDP inflation", "India rupee forex")

All configurable: enable/disable, add custom RSS URLs, edit queries.

## Recommendation Engine Scoring

The unified engine (`recommender.py`) uses weighted scoring:

**Bullish signals (add points):**
- Volume-confirmed breakout: +3.0 (best signal, 71% historical win rate)
- Volume spike bullish: +2.0
- Near major support: +2.0
- Gap filled (reversal): +1.5
- RSI oversold: +1.5
- Cyclical bullish month: +1.5
- Strong uptrend: +1.0

**Bearish signals (subtract points):**
- Breakdown below support: -2.5
- Volume spike bearish: -2.0
- Near major resistance: -1.5
- Cyclical bearish month: -1.5
- RSI overbought: -1.0
- Strong downtrend: -1.0
- Gap up/down unfilled: -0.5 (historical fade signal)

**Ratings:**
- Score ≥ +4: STRONG BUY
- Score +2 to +4: BUY
- Score -2 to -4: SELL
- Score ≤ -4: STRONG SELL

**Confidence:** based on aligned signal count (HIGH: 4+, MEDIUM: 2-3, LOW: 1)
**Success Probability:** 50% baseline + 4% per score point + 2% per aligned signal (capped 85%)

## Key Patterns

- **Agent creation**: `create_X(llm, memory?) → node_function(state) → dict`
- **Tool routing**: `@tool` wrappers call `route_to_vendor()`
- **State**: `AgentState` TypedDict with all reports, debate states, final decision
- **Memory**: BM25-based similarity matching with **disk persistence** (auto load/save)
- **Stats tracking**: `StatsCallback` extends `BaseCallbackHandler` to track tokens/cost across LangChain/LangGraph calls
- **Streaming**: Graph `stream_mode="values"` → WebSocket events with heartbeats + stats

## Safety

- `dry_run: True` by default — orders are logged, never executed
- `order_execution_enabled: False` — master switch
- Max position value, max loss per trade, max daily loss limits
- Stop-loss required for all trades
- Exchange whitelist (NSE only by default)

## Storage Paths

- SQLite DB: `~/.tradingagents/trading_agent.db` — tables:
  - `watchlist` — saved tickers
  - `analysis_history` — AI analyses with P&L columns
  - `backtest_runs` + `backtest_trades` — AI backtest runs
  - `paper_trades` — virtual trades with multi-horizon P&L + triggered_signals JSON
  - `recommender_backtests` — historical recommender engine replay results
  - `settings` — API keys + LLM config + news sources
- Agent memories: `~/.tradingagents/memory/{bull,bear,trader,invest_judge,portfolio_manager}_memory.json`
- Analysis logs: `~/.tradingagents/logs/` (per-analysis JSON state dumps)

## Data Flow Across Features

```
Recommendations (FREE)
    ↓ "Track" button
Paper Trades (FREE, tracks at 1/3/5/10 days)
    ↓ time passes + refresh prices
Simulation / Learning Insights (FREE)
    ↓ patterns surface
You filter better → take real trades
    ↓ "Log P&L" with reflection
Real trades + agent memory growth
    ↓
Learning Insights updates with combined data
```

## Smart Filters on Recommendations

### FII/DII Flow Bias (NEW)
- Live NSE data via `nsepython` (cached 1 hour in `fii_dii_history` table)
- `backend/fii_dii.py` — fetcher + manual entry fallback
- `get_market_bias()` returns score adjustment -1.5 to +1.5 based on FII/DII net values
- Recommender's `_apply_market_bias()` adjusts every stock score before classifying
- Dashboard shows banner: today's FII/DII net + 5-day trend + reasoning
- API: `/api/fii-dii/today`, `/history`, `/bias`, `/manual`

### Earnings + Economic Calendar (NEW)
- `backend/calendar_data.py` — hardcoded RBI/Budget/Fed/F&O dates + yfinance earnings fetch
- `get_event_filter_for_ticker()` returns score adjustment for upcoming events
- Recommender's `_apply_event_filter()` penalizes stocks with imminent events
- Earnings (≤2 days): -2.5 | Budget (≤1 day): -2.0 | RBI today: -1.5 | FOMC today: -1.0 | F&O expiry today: -0.5
- Dashboard banner shows today's events + 14-day forecast
- API: `/api/calendar/today`, `/upcoming`, `/ticker/{ticker}`, `/refresh-earnings`

### Sector Concentration Checker (NEW)
- `backend/concentration.py` — reverse-maps tickers to sectors, tracks open positions
- `get_open_positions()` — aggregates active paper trades + open analysis trades
- `get_concentration_summary()` — risk level (HIGH/MEDIUM/LOW) + per-sector breakdown
- `check_new_trade_concentration()` — penalty if a new trade would breach limits
- Default limits: max 3 positions/sector, max 30% capital/sector
- Recommender's `_apply_concentration_filter()` only applied to BULLISH signals
- Dashboard widget with stacked bar showing sector allocation
- API: `/api/concentration/summary`, `/allocation`, `/check/{ticker}`, `/positions`

### Signal Performance Tracker (NEW)
- `backend/signal_performance.py` — analyzer + auto-tuner for recommender weights
- Reads `paper_trades` with `pnl_5d_pct IS NOT NULL`, explodes the `triggered_signals` JSON, credits/blames each signal that fired
- Win logic: BULLISH signal wins when 5d P&L > 0; BEARISH/FADE wins when < 0 (multi-attribution — every signal in a trade gets one observation)
- Wilson lower bound at 80% CI used for honest small-sample estimates (avoids over-reacting to lucky streaks)
- `compute_signal_performance(window_days=90)` returns per-signal: n, wins, losses, win_rate, wilson_lower_80, avg_return_5d_pct, current_weight, suggested_weight, delta, verdict (TUNE_UP / TUNE_DOWN / KEEP / INSUFFICIENT_DATA)
- `apply_tuned_weights()` persists suggestions to `settings.recommender_tuned_weights` (JSON dict)
- `recommender.py` calls `_refresh_active_weights()` at start of every `recommend()` — merges DEFAULT_WEIGHTS with tuned overrides into module-level `_ACTIVE_WEIGHTS`. All scoring inside `_analyze_stock` reads from `_ACTIVE_WEIGHTS`, so the engine literally rewrites itself from real outcomes
- Min sample size: 10 trades per signal before any change is suggested
- Suggestion formula: `new_mag = abs(current) * clamp((wilson_lower - 0.30) / 0.20, 0, 2.5)`, sign preserved, clipped to [0, 3.5]
- Frontend: `/signals` page — full per-signal table with current vs suggested weights, "Apply Suggested Weights" button, "Reset to Defaults"
- API: `GET /api/signal-performance/?window_days=N`, `GET /active-weights`, `POST /apply`, `POST /reset`

### Memory Pruning + Decay — Tier 4.2 (NEW)
- `tradingagents/agents/utils/memory.py` rewritten to v2 schema:
  - Each entry now: `{situation, recommendation, created_at, last_accessed, hit_count}`
  - Backward-compat: legacy `documents`+`recommendations` array files auto-migrate on load (timestamped with current time + zero hits, console logs the migration)
  - Disk format keeps both legacy mirror keys AND new `entries` array, schema_version=2
  - `documents` and `recommendations` exposed as `@property` for any caller still using them
- Decay formula: `score = max(0, bm25_score) × decay_factor`
  - Grace period: ≤30 days → 1.0
  - Linear decay: 30→365 days drops from 1.0 to 0.2 floor
  - Frecency bonus: 1.25× if `last_accessed` within 7 days
  - `_decay_factor()` is the single source of truth
- `get_memories()` now updates `last_accessed` + `hit_count` on retrieved entries (only when score > 0 — prevents irrelevant entries from inflating hit count)
- New methods: `prune(max_age_days, min_hits, min_decay, dry_run)`, `delete_entry(index)`, `list_entries()`, `stats()`
- Module-level helpers: `list_all_memory_stats()`, `prune_all_memories()` for cross-agent ops
- `backend/routers/memory.py` — admin REST API: `GET /api/memory/`, `GET /{name}/entries`, `POST /{name}/prune`, `POST /prune-all`, `DELETE /{name}/entry/{index}`
- Frontend `/memory-admin` page:
  - Per-agent stat grid (total, active, decayed, stale, never_hit, avg_decay, oldest age)
  - Pruning form with three optional criteria + Preview (dry-run) + Prune All buttons
  - Click an agent card to inspect entries (situation + lesson previews, age, decay color-coded, hit count, delete button)
- Bug fix during build: BM25 returns negative scores for below-average matches; multiplying negatives by smaller decay would invert ranking. Fixed by `max(0, score)` before applying decay.

### Conditional Regime Weights — Tier 4.1 (NEW)
- `backend/signal_performance.py` extended:
  - `compute_regime_conditional_weights(window_days)` — per-regime suggested overrides
  - `apply_regime_weights(only_regimes)` — persist to `settings.recommender_regime_weights` (JSON: `{"BULL": {...}, "BEAR": {...}, "SIDEWAYS": {...}, "HIGH_VOL": {...}}`)
  - `get_active_weights_for_regime(regime)` — three-layer merge: DEFAULT → base tuned → regime override
  - Conservative thresholds: `MIN_SAMPLE_PER_REGIME=5`, `REGIME_OVERRIDE_THRESHOLD=0.10`, delta>=0.25
- `backend/recommender.py` modified:
  - `_refresh_active_weights()` now detects current regime via `market_regime.get_current_regime()` and applies the matching layer
  - Module-level `_ACTIVE_REGIME` tracks which regime's weights are loaded
  - `recommend()` response includes `active_regime` + `regime_weight_overrides_active` count
- Frontend:
  - `/signals` page has new "Conditional Regime Weights (Tier 4.1)" section with 4-card grid (one per regime), Apply All / Reset buttons
  - `TodayPicks.tsx` header shows `NIFTY100 · HIGH_VOL ⚡N` where N is active override count
- API additions:
  - `GET /api/signal-performance/regime-suggestions?window_days=N`
  - `GET /api/signal-performance/regime-active`
  - `POST /api/signal-performance/regime-apply` (body: `{window_days, only_regimes?}`)
  - `POST /api/signal-performance/regime-reset`
- Smoke test: HIGH_VOL active, applied 1 override (rsi_overbought -1.0 → 0.0 from 5 trades, 40% WR), recommend response confirmed `regime_weight_overrides_active: 1`

### Shadow Trades / Counterfactual Learning (NEW)
- `backend/shadow_trades.py` + `shadow_trades` table — auto-tracks every STRONG BUY (and HIGH-conf BUY) the recommender produces, regardless of whether user clicked Track
- Hooked into `recommend()` in `backend/recommender.py` — after each call, idempotent insert (PRIMARY KEY ticker+signal_date) records picks
- `record_shadow_trades_from_recommendations(recs)` filters to STRONG BUY (any HIGH/MEDIUM conf) + BUY with HIGH conf only; skips noise from MEDIUM-conf BUYs
- `refresh_shadow_prices()` — backfills 1/3/5/10d prices via yfinance, computes pnl_*_pct; auto-runs whenever paper-trade `refresh_paper_trade_prices()` is called
- `shadow_vs_user_comparison()` — compares win rate of all shadows vs user-tracked subset vs skipped subset; verdict: filter_helps / filter_hurts / filter_neutral / insufficient_data (needs ≥5 in tracked + skipped buckets)
- `user_tracked` flag set to 1 if user opened a paper_trade for the ticker on the same day
- Reuses `_price_n_days_later()` from simulation.py for backfill (consistent with paper trades)
- Frontend `/shadow-trades`:
  - Verdict pill ("Filter HURTS" / "Filter HELPS" / etc.) with explanation
  - 4-card stats grid (all / tracked / skipped / strong_buys only)
  - Full trade table with date, ticker, signal, confidence, regime, predicted%, entry, per-horizon P&L, tracked icon
  - 6-step "How to use this page" callout
- Sidebar entry under VALIDATE
- API: `GET /api/shadow-trades/?window_days=N&only_ripe=bool`, `GET /comparison`, `POST /refresh`

### Confidence Calibration / Brier Score (NEW)
- `backend/confidence_calibration.py` — measures whether recommender's `success_probability` is honest
- Reads closed paper_trades with non-null `pnl_5d_pct` and `success_probability`
- Brier score = mean((predicted - actual)²); win = pnl_5d_pct > 0
- Quality bands: ≤0.15 excellent, ≤0.20 good, ≤0.25 fair (50% baseline), >0.25 poor
- Reliability bins: [40-50%, 50-60%, 60-70%, 70-80%, 80-90%, 90-100%]; per bin shows predicted_avg, actual_win_rate, gap
- Verdict from overall gap (actual - predicted): <-0.05 overconfident, >+0.05 underconfident, else well_calibrated
- Improvement % vs always-predict-50% baseline (0.25 Brier) tells you if the model has real signal
- Frontend `/confidence-calibration` page:
  - Headline metrics (verdict, Brier, improvement %, sample size, predicted vs actual)
  - Reliability diagram with paired blue (predicted) + gold (actual) bars per bin
  - Per-bin breakdown table with gap column color-coded
  - 6-step "How to use this page" callout
- API: `GET /api/confidence-calibration/?window_days=N`

### Market Regime Classifier (NEW)
- `backend/market_regime.py` — labels every trading day as one of:
  - `BULL`: Nifty > 50 SMA > 200 SMA, normal vol
  - `BEAR`: Nifty < 50 SMA < 200 SMA, normal vol
  - `SIDEWAYS`: SMAs not aligned (mixed trend)
  - `HIGH_VOL`: realized 20d vol > 1.5× the 120d-avg baseline (overrides trend label)
- `paper_trades.regime_at_entry` column populated automatically on every new trade insert
- `backend/regime_backfill.py` tags historical trades by classifying their entry_date once per unique date (cached)
- `compute_signal_performance_by_regime()` in `signal_performance.py` splits each signal's win rate by regime — surfaces "regime-dependent" signals (spread > 20% with n≥5 in 2+ regimes)
- Frontend:
  - `RegimeBadge` component on Dashboard — color-coded card showing current regime + reasoning + how it affects signal reliability
  - "Regime-Conditional Win Rates" section on `/signals` page — per-signal grid (BULL / BEAR / SIDEWAYS / HIGH_VOL columns) with spread + ⚡ flag for regime-dependent signals
- API: `GET /api/regime/current`, `GET /api/regime/on?d=YYYY-MM-DD`, `POST /api/regime/backfill-trades`, `GET /api/regime/signal-performance`

### Verdict Calibration (NEW)
- `backend/verdict_calibration.py` — measures whether the daily verdict actually predicts Nifty
- `verdict_history` table in `backend/db.py` — one row per snapshot day with verdict, flags, Nifty close, forward closes at 1/3/5 trading days, and outcome classification
- `snapshot_today()` saves today's verdict + Nifty close (idempotent — skips if already taken). Called automatically inside the `/api/daily-verdict/` route so loading the dashboard captures the snapshot
- `backfill_outcomes()` fills in forward Nifty closes when ripe (1/3/5 trading days passed) and classifies each horizon
- Outcome rules:
  - GREEN: correct if Nifty return > +0.10%, wrong if < -0.10%, neutral otherwise
  - RED: correct if Nifty return < -0.10%, wrong if > +0.10%, neutral otherwise
  - YELLOW: correct if abs(return) <= 0.50% (a quiet day matches the call), else wrong
- Accuracy = correct / (correct + wrong); neutrals excluded so noise doesn't pollute
- `compute_calibration(window_days=90)` returns per-verdict counts, avg returns at each horizon, accuracy %, and recent snapshot history
- Frontend: `/verdict-calibration` page — accuracy table by verdict + recent snapshots with per-horizon outcome icons (✓/✗/—)
- API: `GET /api/verdict-calibration/?window_days=N`, `POST /snapshot`, `POST /backfill`

### Daily Trading Verdict (NEW)
- `backend/daily_verdict.py` — synthesizes all 4 filters into ONE decision
- Inputs (each contributes a caution flag, favorable flag, or nothing):
  1. FII/DII bias — caution if BEARISH, favorable if BULLISH (HIGH confidence amplifies)
  2. Calendar events (next 3 days) — caution for FOMC ≤2 days, RBI ≤1 day, Budget ≤1 day, F&O expiry today, earnings ≤2 days
  3. Sector concentration — caution if HIGH/MEDIUM risk on open positions
  4. Live HIGH-conviction setup count (NIFTY 50 recommend) — favorable if ≥3, caution if 0
- Decision table (caution count → favorable count → outcome):
  - `caution ≥3` → RED skip-day (0% size, 0 trades, HIGH only)
  - `caution = 2` → RED stand-down (0% size, 0 trades, HIGH only)
  - `caution = 1, fav = 0` → YELLOW selective (50% size, 2 trades, HIGH only)
  - `caution = 1, fav ≥1` → YELLOW selective (75% size, 3 trades, HIGH only)
  - `caution = 0, fav ≥2` → GREEN aggressive (100% size, 5 trades, MEDIUM ok)
  - `caution = 0, fav = 1` → GREEN normal (100% size, 4 trades, MEDIUM ok)
  - `caution = 0, fav = 0` → YELLOW quiet-day (75% size, 2 trades, HIGH only)
- Caution is weighted over favorability — code at `backend/daily_verdict.py:130-178`
- Output shape: `{verdict, label, action, caution_flags[], favorable_flags[], recommended_position_size_pct, max_trades_today, min_conviction_required, reasoning, filter_results}`
- Headline card at TOP of Dashboard (between Market Status and FII/DII banner)
- API: `GET /api/daily-verdict/`

## Recently Added (Auth, Kite, Portfolio, Notifications)

Shipped after the initial feature set (see git history / PR #4):

- **Browser authentication** (`backend/auth.py`, `backend/routers/auth.py`, `/login`) — single-user HMAC-signed session cookie (12h, revocable on logout), CSRF double-submit + Origin allowlist, WebSocket auth. **Optional for local** (open by default when no secret is set), **required + fail-closed for public** (`TRADINGAGENTS_AUTH_MODE=public`). Secret is env-only (`TRADINGAGENTS_LOCAL_AUTH_SECRET`, or `TRADINGAGENTS_AUTH_USERNAME`/`_PASSWORD`) — never stored in SQLite. Central `AuthMiddleware` wraps all `/api/*` HTTP + WS traffic in `app.py`.
- **Zerodha Kite read-only integration** (`backend/brokers/kite.py`, `backend/routers/kite.py`) — OAuth login flow, daily access token, `holdings()` fetch, credential masking. **Read-only: no order placement, and no margin fetch yet.** `kiteconnect` is an **optional extra** (`pip install -e ".[kite]"`), lazy-imported in `_kite_connect_cls()`; without it the Kite routes return a 400 that says how to install it. It stays out of the default install and out of `requirements.txt` because kiteconnect pins `autobahn==19.11.2` (CVE-2020-35678, CVE-2026-77528), which would raise Dependabot alerts that can't be fixed. Only kiteconnect's live-feed client (`KiteTicker`) uses autobahn, and the app never opens that feed. Tests stub the client, so CI doesn't install it. Kite app **Redirect URL** must be the backend callback `http://localhost:8000/api/kite/callback` (exact match). The CSRF `state` is carried via Kite's `redirect_params` (URL-encoded) — Kite does **not** echo a bare top-level `state`, so passing it directly would fail every callback with `invalid_oauth_state` (issue #7).
- **Equity portfolio analysis** (`backend/equity_portfolio.py`, `backend/routers/equity_portfolio.py`, `/equity-portfolio-analysis`) — P&L, actions, sector allocation, concentration warnings, winners/losers, persisted review history.
- **Local positions tracking** (`backend/positions.py`, `backend/routers/positions.py`) — manual exchange-aware CRUD + explicit, manual-preserving Kite sync; feeds concentration checks.
- **Telegram notifications** (`backend/notifications/telegram.py`, `backend/routers/telegram.py`) — test + portfolio-review alerts with safe local-URL button fallback.
- **Dark mode** — light/dark/system theme via `next-themes` (`ThemeToggle`, status tokens in `lib/status-colors.ts`).
- **Ollama local LLM provider** — added to `PROVIDERS_INFO` (no API key/cost); `GET /api/settings/ollama/models` live-fetches installed models from `localhost:11434`.
- **Yahoo-powered ticker search** — `GET /api/market-data/search` now uses `yfinance`'s `Search` (always current, NSE-filtered). `stock_list.py` is only a small curated offline fallback; **no NSE data bundled or fetched**.
- **Security hardening** (full-repo audit) — `normalize_ticker` rejects path-like symbols (`InvalidTickerError` → HTTP 400) and `safe_path_component` guards every ticker-derived path; RSS feeds are fetched by `news_sources._fetch_feed` (public http(s) only, re-checked per redirect, bytes handed to feedparser as a stream); open mode (no auth secret) only accepts loopback Host headers + allowed/same Origin (`TRADINGAGENTS_ALLOWED_HOSTS` to extend); AI backtests capped at 60 dates (`TRADINGAGENTS_MAX_BACKTEST_DATES`) and one at a time; `~/.tradingagents` 0700 / DB 0600; CLI upstream announcements opt-in (`TRADINGAGENTS_CLI_ANNOUNCEMENTS=1`). Pinned deps in `requirements.txt`.
- **Test suite** (`tests/`) — 78 `unittest` cases (auth/CSRF/OAuth-replay/WS-origin, security hardening, equity portfolio, positions, Telegram). Run: `python -m unittest discover -s tests`.
- **CI + contribution rules** — `.github/workflows/tests.yml` runs the backend tests and the frontend lint (`npm run lint`) + typecheck + build on every push to `main` and every PR (read-only token, `npm ci --ignore-scripts`, Actions pinned to commit SHAs and updated weekly via `.github/dependabot.yml`). CodeQL code scanning (default setup) checks every PR. `main` is protected by the "Protect main" ruleset (both checks required for PRs; the owner can bypass for direct commits). `.github/CODEOWNERS` auto-requests the maintainer's review on every PR. Contributor workflow: `CONTRIBUTING.md` (one change per PR, from a feature branch); vulnerability reports: `SECURITY.md`.
- **Leaner frontend dependencies** — the `shadcn` CLI is no longer a dependency (it pulled in ~250 packages, including `hono` from Dependabot alert #53). Its theme helpers are vendored in `frontend/src/app/shadcn-tailwind.css` (byte-identical to shadcn@4.2.0, MIT notice included); add components with `npx shadcn@latest add <name>`.
- **Dependency security fixes (2026-11-17, commit `cb435ba`)** — cleared all 11 open Dependabot alerts in one commit on `main` and closed the Dependabot PRs it replaced (#25, #26, #27):
  - `next` 16.4.1 → 16.4.5 (GHSA-mfr8-8qww-v76h: a `'use cache'` function called through `unstable_cache()` could leak data across root params)
  - `soupsieve` 2.9 → 4.0.1 (4 ReDoS/quadratic-time advisories). Only used through `beautifulsoup4` (from yfinance); the 4.0 breaking changes only affect `<template>`/`<iframe>` matching. **Needs Python ≥ 3.11.5.**
  - `pyasn1` 0.6.4 → 0.6.5 (4 quadratic-time DoS advisories); `setuptools` 83.0.0 → 84.1.0 (Windows path escape)
  - `minimatch` 10.2.5 → 11.0.4 for `@typescript-eslint/typescript-estree` (GHSA-g3hv-75wg-m249), first set with a temporary `overrides` entry in `frontend/package.json`. Dev-only (ESLint). **Override removed 2026-11-17:** `typescript-eslint` was updated 8.58.1 → 8.73.0 (comes in through `eslint-config-next`), and 8.73.0 depends on `minimatch ^11.0.1` itself. The lockfile kept minimatch 11.0.4. The update also bumped `ignore`, `semver` and `brace-expansion` within their ranges. `npm audit signatures` passes (8.73.0 is published from typescript-eslint's GitHub Actions with provenance). `npx eslint src` reported the same 339 problems before and after the update. That older lint debt was then cleared (`cf368f0..b910dab`, 0 problems), and `npm run lint` is now a CI step.
  - Each Dependabot PR was checked against the external-PR checklist before closing (only its own package changed, all tarballs from `registry.npmjs.org`). Fresh releases (16.4.6, typescript-eslint 8.73.0) were skipped in favour of the first patched version at least ~2 weeks old.
- **Frontend lint cleanup (2026-11-17, `cf368f0..b910dab`)** — `npx eslint src` went from 339 problems (299 errors, 40 warnings) to 0, without changing ESLint config or adding `eslint-disable` comments. `npm run lint` is now a step in the CI frontend job (`f404754`). The job keeps the name "Frontend typecheck and build" because that name is a required check in the "Protect main" ruleset. Conventions to keep it clean:
  - **API responses:** `lib/api.ts` functions return `Promise<unknown>`. Each page declares local types for the response shapes it reads (taken from the backend route) and asserts them at the call site: `(await getX()) as XResponse`, or `as [A, B]` for `Promise.all`. Don't write `any`. When an API function's *parameter* type is missing a field the backend accepts, fix it in `api.ts` rather than casting.
  - **catch blocks:** `catch (e) { toast.error(errorMessage(e, "fallback")) }`, using `errorMessage` from `lib/utils.ts`.
  - **Data-loading effects** (react-hooks `set-state-in-effect`): a `fetchData()` sets state only inside promise callbacks; `load()` = `setLoading(true)` + `fetchData()` for refresh buttons; the effect calls `fetchData()`. A handler that changes a dependency (e.g. a window select) sets `loading` itself. Keep render pure: no `Date.now()` and no variable reassignment during render.
  - **JSX text:** escape `'` and `"` as `&apos;` / `&quot;`.
- **Frontend bug fixes (2026-11-17, `b910dab..780e684`)** — the typing pass above turned up these, each fixed in its own commit:
  - Cyclical page: the seasonal backtest pre-fill is now Buy = weakest months, Sell = strongest, matching the page's advice text. "+-0.4%" now shows as "-0.4%". The exit window's "+X% to +Y%" uses the real low–high range.
  - News: the Sources tab shows the error and a Retry button instead of spinning forever.
  - Simulation: missing averages show an uncoloured "—" instead of green. A paper-trade row with undecodable `triggered_signals` no longer crashes the page.
  - P&L dialog: shows "Analysis not found" instead of a bogus success toast.
  - Analysis detail: the task ID is never blank.
  - Confidence Calibration: the `no_valid_data` badge is styled and labelled.
  - `lib/types.ts`: `WSEvent` / `BacktestWSEvent` now list everything the backend streams.
  - Analysis page: the "Live: … Ns ago" label now counts up every second; it used to read the clock during render and was nearly always "0s ago".

## F&O / MCX Data Layer (Phase 2, fork)

- **Market calendar** (`tradingagents/utils/market_calendar.py`) — official 2026 NSE list (the old 2026 entries were placeholders), MCX holidays with morning/evening status, MCX evening close 23:30/23:55 from US DST, weekend special sessions (Budget, Muhurat; not trading days for new entries). Exchange-aware API: `session_windows()`, `is_exchange_open()`, `exchange_status()`, `upcoming_closures()`, `calendar_loaded()`. Update every December.
- **Instrument master** (`backend/marketdata/instruments.py`) — daily Kite dump into the `instruments` table: all NFO futures, NIFTY/BANKNIFTY options, NSE spot for F&O stocks + spot indices, MCX minis (GOLDM, SILVERM, CRUDEOILM, NATGASMINI). A dump with < 100 NFO futures is rejected and the old master kept. `fno_universe()` = F&O stocks only (cash-only stocks never scanned), with spot token, active future (rolls to next month at ≤ 3 trading days to expiry) and ban flag. Ban list from NSE `fo_secban.csv`; if it can't be fetched the flag is `"unknown"`.
- **Candles** (`backend/marketdata/candles.py`) — Kite historical 10minute / 60minute / day into the `candles` table. Only completed bars are stored (bar end capped at session close); each sync resumes from the last stored bar. `resample_4h()` builds session-anchored 4H bars (NSE 09:15/13:15; MCX 09:00/13:00/17:00/21:00); `resample_weekly()` builds Monday-anchored weekly bars from daily (cash swing context).
- **Jobs** (`backend/marketdata/jobs.py`) — `morning_refresh()` (instruments + ban list) and `candle_sync()` over `build_sync_plan()`. One job at a time; skipped while paused; every run logged in `marketdata_runs`.
- **Pause switch** (`backend/system_state.py`) — `is_paused()` must be checked before any scan, signal or AI call. Sidebar `SystemControl` shows Pause/Resume plus NSE/MCX open/closed and the next closure.
- **API** — `GET/POST /api/system/pause`, `GET /api/market-calendar/status`, `GET /api/marketdata/status`, `POST /api/marketdata/refresh-instruments`, `POST /api/marketdata/sync-candles?interval=`, `GET /api/marketdata/universe`, `GET /api/marketdata/candles?token=&interval=10minute|60minute|day|4h|week&limit=`.
- **Scheduler** (`backend/scheduler.py`) — one background thread started in the app lifespan (disable with `TRADINGAGENTS_SCHEDULER=off`; tests set it off). Every 30 s `due_jobs()` (pure, unit-tested) decides what runs: `morning_refresh` once per trading day from 08:30 or right after the Kite login, then `candles:<EXCHANGE>:<interval>` 2 min after each bar closes (NSE 10m + 1h, MCX 1h) and daily bars 10 min after each exchange closes. Nothing runs on weekends/holidays, while paused or before today's Kite login; a missed tick catches up. Single uvicorn worker assumed. `GET /api/scheduler/status`; the sidebar shows the data status line.
- **Decision engine config** (`config/decision_engine.yaml`, loader `backend/decision/config.py`): the single source of trading parameters in three layers — `global` (capital, system switches, risk, limits, drawdown, scoring weights, geometry, regime thresholds, regime × setup permissions, trailing shadow), `instruments` (per stock_futures / index_futures / mcx / index_options / stock_options / cash_swing: enabled, primary/confirmation/context timeframes, directions, shortlist size, holding days, timing, spread, extras like MCX value per point) and `strategies` (pullback / breakout / range_reversal: EMAs, RSI, volume multiplier, ATR, min R:R, lookback, extras). Validated with pydantic (`extra="forbid"` on fixed sections, weights must sum to 100, fast EMA < slow EMA); `paper_trading_only` must be true and `live_order_execution` / `averaging_down` / `forced_trades` false or the app refuses to load it. `get_config()` re-reads on file change; `rule_version` = `v<version>-<sha256[:8]>` is stored on every scan run and journal record. `TRADINGAGENTS_DECISION_CONFIG` overrides the path (tests). **Never hardcode a threshold in the engine — add it to the YAML.** Change control: edit via a PR, not on the server. `GET /api/decision-engine/config`.
- **Staged decisions:** every candidate carries `stages` — a list of `{stage, decision (pass / fail / soft_fail / below_threshold / pending / not_sent / not_applicable), reason, at, data?}` for eligibility → regime → setup → construction → scoring (→ context / sizing / liquidity for futures) → ai_panel → final_approval. Stored in `trade_journal.stages` and scan candidate extras.
- **Rule engine** (`backend/signals/`, Phase 3; rules agreed in the project doc `trade-selection-rules.md`):
  - `indicators.py` — EMA/SMA, Wilder RSI/ATR/ADX, realised vol, confirmed 5-bar fractal swings (plain lists).
  - `regime.py` — HIGH_VOL / STRONG_UP / STRONG_DOWN / SIDEWAYS / UNSTABLE with thresholds from `global.regime`; the permission matrix lives in `global.permissions`. Range reversal additionally has `requires_regime: SIDEWAYS` (hard — never generated, not even as shadow, outside sideways).
  - `setups.py` — `evaluate()` checks pullback / breakout / range reversal, symmetrical long/short, with per-strategy parameters; `confirmations={tf: bars}` must not oppose the trade (hard); volume is compared with comparable completed bars (`volume_ratio()`: same time-of-day slot for intraday); scoring weights from config; stop = latest confirmed swing + 0.25 ATR (min 1 ATR, reject > 3 ATR or > `max_stop_pct`); target ≥ 2R before the next swing level (else reject; capped 4R; 3R when no level); 100-point score with per-factor breakdown (relative strength only for stocks; others scaled from 90). Every checked (setup, direction) returns a row with `valid`/`reject_reason`.
  - `events.py` — verified RBI/Budget/FOMC dates (official sources) with `VERIFIED_THROUGH`; windows past it fail closed. Results dates from yfinance cached in `results_dates`; a symbol is clear only with a known next date after the window or a result within the last 45 days — otherwise excluded.
  - `cash_swing.py` — daily scan from 15:45 (scheduler job `scan:cash_swing`, after today's daily candles): NIFTY data/event checks block the whole scan; per stock data quality (fresh, no gaps in 20 days, ≥ 250 bars), ₹50 cr turnover, results clear, then `evaluate()` long-only with weekly context and 8% max stop. All rows go to `scan_runs` / `scan_candidates`; top 2 with score ≥ 60 are `shortlisted`. Exposure and daily-loss checks arrive with the paper engine.
  - API: `GET /api/scans/cash-swing?signal_date=`, `POST /api/scans/cash-swing/run`; page `/swing-scan`.
  - **Soft vs hard checks:** regime permission, breakout volume and the pullback RSI turn are *soft* — a setup failing only these still gets stop/target/score, `valid=False`, `trackable=True`, `soft_checks` recorded, and is shadow-tracked. Structural conditions, HTF agreement, geometry and all hard filters are never relaxed.
- **Hourly futures / MCX scan** (`backend/signals/fno.py`): `run_hourly_scan("nse"|"mcx")` after each synced 1h bar (scheduler jobs `scan:nse_1h`, `scan:mcx_1h`; scan segments `fno_1h` / `mcx_1h`). Entry TF = 1h bars of the traded contract (active future, OI required); confirmation 2H and 4H (resampled, `bars_for_timeframe()`), 4H scored; `context_must_not_oppose: [day]` (NSE spot / MCX continuous daily). Cash swing: daily entry, weekly scored HTF, 4H (from spot hourly) confirmation. Regime: NIFTY daily for NSE, each commodity's own daily for MCX. Extra hard filters: F&O ban (or unknown) → reject; live bid-ask spread from one `kite.quote()` call must be ≤ 0.1% (unavailable → reject); 1 lot risking > ₹10k → reject; RBI/Budget/results inside the 3-day hold (NSE); `events.mcx_event_block()` (US CPI/FOMC for GOLDM/SILVERM, Wed/Thu crude and Thu/Fri gas inventory windows 17:00–23:59 IST, fail closed past `US_EVENTS_VERIFIED_THROUGH`); no signal whose entry would start in the last 30 min. Entry: limit at the signal close for the next 2 bars; time stop: 3rd trading day close, never on/after expiry. `MCX_VALUE_PER_POINT` (GOLDM 10, SILVERM 5, CRUDEOILM 10, NATGASMINI 250 ₹ per point per lot). Scoring for futures: volume up to 10 + 5 for OI build-up in the trade direction (`soft_checks.oi_buildup`). The sync plan now also pulls 1h+OI for every stock's active future. API: `GET /api/scans/hourly/{nse|mcx}`, `POST .../run`; page `/hourly-scan` (shared `components/scans/ScanView.tsx`).
- **Trade journal** (`backend/journal/`): `trade_journal` table, one row per trackable candidate, unique on (segment, symbol, `signal_ts`, setup, direction) — v1 tables are migrated (`decision`: soft_rejected / below_score / not_selected / shortlisted / approved / rejected_panel; `is_paper` only for panel-approved). `simulate.py` is the pure bar-by-bar outcome model (limit fill in the entry window, no fill if it opens beyond the stop, stop-first when a bar touches both, gap exits at the open, time stop, MFE/MAE in R, trailing shadow exit after 1.5R at 2 ATR from the best close, delivery charges). `tracker.py` re-simulates every pending/open record (all segments) statelessly from stored candles (cash swing: hourly bars on the entry day, then daily; futures/MCX: hourly bars of the contract after `signal_ts`) with lots × value-per-point sizing and `futures_charges()` (NSE STT 0.05% sell from 1 Apr 2026; MCX CTT 0.01% sell) and never closes on a data gap; runs before each daily scan, also while paused. `learning.py` answers the six agreed questions (min 30 closed per group, otherwise flagged). API: `GET /api/journal?segment=&decision=&status=`, `GET /api/journal/learning`, `POST /api/journal/track`; page `/journal`.
- **Rule backtester** (`backend/backtest/`, page `/rule-backtests`): replays a rule set over stored history, point in time, through the same `evaluate()` and outcome `simulate()` as the live journal. Rule sets: `current` (`config/decision_engine.yaml`) plus every `config/experiments/*.yaml`; `v1_baseline.yaml` reproduces the rules before the decision-engine change (no 2H/4H confirmations, previous-bar volume, no pullback/range volume check, range lookback 60 and regime soft). `use_config(path)` (context variable) swaps the rule set only inside a backtest; live scans are unaffected. New knob `strategies.*.volume_reference: same_slot | previous_bars`.
  - Segments: `cash_swing` (daily signal, next-day hourly entry window, then daily bars) and `stock_futures_proxy` (hourly signals on the **spot** stock, because expired futures can't be downloaded). Higher timeframes come from `IncrementalBars` views (completed bars + the current partial bar, never later data, unit-tested against full resampling). RBI/Budget days inside the hold block signals; `events.VERIFIED_FROM` = 2024-01-01 (2024–25 RBI/Budget dates added).
  - Summary per run: groups shortlisted (top N per scan) / passed all rules / all trackable, plus by setup, regime, exit, month and soft check passed vs failed; win rate, avg R, PF, total R, max drawdown in R, trailing-shadow avg R. Limitations are listed with every run: survivorship (today's F&O list), no historical ban list / spreads / OI / results dates, no portfolio limits.
  - History: `jobs.history_backfill()` → `backtest/data.backfill_for_backtests()` extends stored candles **backwards** (3 years daily, 12 months hourly spot, NIFTY + F&O stocks), same `candles` table.
  - Runs in a background thread, one at a time, stored in `rule_backtests` (summary + trades JSON); a restart marks running runs as interrupted. API: `GET /api/rule-backtests/rule-sets`, `POST /api/rule-backtests/history`, `POST /api/rule-backtests` `{segment, rule_set, start_date, end_date, max_symbols?}`, `GET /api/rule-backtests`, `GET /api/rule-backtests/{id}?trades=`, `GET /api/rule-backtests/compare?ids=1,2`.
- **Two-stage selection** (`global.selection` in the YAML, agreed 21 Nov 2026): `mode: strict` (live default) keeps today's behaviour; `mode: scored` (rule set `config/experiments/two_stage.yaml`) turns higher-timeframe disagreement (`htf_against_penalty`, half for a flat HTF), confirmation/context timeframes against the trade (`confirmation_against_penalty` each) and a setup's required regime (`required_regime_penalty`) into score penalties; regime permission, volume and the RSI turn only change factor scores. Candidates stay valid and are ranked (`penalties` and `score_before_penalties` are stored). Stage A safety filters (universe/ban, liquidity, data, stop, min R:R, events, expiry, timing, duplicates) stay hard in both modes. `evaluate()` now takes `context={tf: bars}` (must-not-oppose; fno.py passes the daily context through it) and `volume_available=False` (index spot: volume not tested, left out of the score).
- **Cup and handle** (`backend/signals/cup_handle.py`, config `instruments.cup_handle`, page `/cup-handle-scan`): William O'Neil pattern on daily bars, cash, long only, chart pattern only. `detect()` (prior uptrend ≥ 30%, cup 7–65 weeks and 12–33% deep with a rounded bottom — ≥ 30% of cup bars in its lowest quarter, right rim within 10% of the left, handle 5–25 days in the upper half and ≤ 12% deep, close above the handle high ≤ 5% extended on ≥ 1.5× 50-day volume, outperforming NIFTY over ~6 months). Buy limit at the top of the buy zone (pivot + 5%) next session; stop 8% / target 20% from the signal close; time stop 40 trading days; own book (max 3 positions, ₹2L each — proposed). Scored 0–100 on breakout volume, relative strength, handle tightness, cup depth, handle volume dry-up, closeness to the pivot. `run_scan()` runs from the scheduler right after the cash swing scan (even when the swing scan is blocked); results/RBI/Budget are checked only for the first `event_block_days` (5) of the hold. Journal segment `cup_handle` is tracked like cash swing (delivery charges). API `GET /api/scans/cup-handle`, `POST /api/scans/cup-handle/run`.
- **Backtester additions:** segments `cup_handle` and `index_futures_proxy` (NIFTY/BANKNIFTY index hourly; volume/OI not tested); cash swing and cup runs reach back before hourly history (entry on the next daily bar, 4H confirmation recorded as untested, `data: daily_only`); summary adds `by_setup_regime`, `by_data` and `untested`; limitations are per segment. The history download adds BANKNIFTY and reports live progress (`GET /api/rule-backtests/history` → done/total, current, candles saved, time left, failures), shown as a progress bar on the page. Rule sets: `current`, `fixed_2r` (target always 2R), `two_stage`, `v1_baseline`.
- **Sideways-market range trades** (rule set `config/experiments/sideways_v1.yaml` = two_stage + range changes, agreed 21 Nov 2026): strategy knobs `regime_source: market|stock` (judge the setup's regime requirement, permission and fit on the instrument's own daily regime; `evaluate(stock_regime=...)`, scans pass `current_regime(stock daily)`, backtests use `regime.regime_at(label_series(daily), i)`) and `target: levels|range_opposite|range_middle` (range trades aim at the other side / middle of the range, `ctx["range"]` from `_range_reversal`); range reversal `min_rr: 1.5` (other setups keep 2.0). Live config unchanged until backtested.
- **Backtest options:** `daily_only` (request field + checkbox): every entry modelled on the next daily bar and hourly-only checks skipped, so recent and older periods can be compared on equal terms; stored in `rule_backtests.options`. Summary adds `by_year` (signal year).
- **Opening range breakout** (`backend/signals/orb.py`, config `strategies.orb` + `instruments.stock_futures.orb_enabled/orb_universe_size`, agreed 21 Nov 2026): an intraday stock-futures strategy (journal segment `stock_futures`, setup `orb`; shares the F&O book limits), on the top 25 F&O stocks by 20-day turnover (`orb_universe()`). Opening range = first 30 min (three 10-min bars of the active future); signal = the day's first 10-min close beyond the range; stop = other side, or the midpoint when the range is wider than 0.6 × daily ATR; target 2R; exit with the bar ending at/after 15:15 (`mark_exit()`); one ORB per stock per day; no extra entry-time limit beyond the last-30-minutes rule. Hard: range 0.15–1.2 daily ATR, opening gap ≤ 3%, ban/results today/RBI-Budget today/data/spread/1-lot risk. Scored (100): same-slot breakout volume 30, NIFTY same direction 25, strength vs NIFTY 15, range quality 15, close strength 15; volume is a soft check (strict mode). Scheduler job `scan:orb` after each synced NSE 10-min bar ending 09:55–14:50; the sync plan adds 10-min + OI for those futures; scan runs saved as `fno_orb`, shown as the "Stock ORB (intraday)" tab on `/hourly-scan`. API `GET /api/scans/orb`, `POST /api/scans/orb/run`. The tracker follows records with `extra.bar_interval` / `exit_time` on 10-min bars. Backtest segment `orb_proxy` (spot 10-min bars of the top 25 by turnover before the start date; 24 months of 10-min history downloaded for them + NIFTY); summary adds `by_stop_type`.
- **Managed exit, sideways mean reversion, backtest funnel** (asked 22 Nov 2026):
  - `global.exit_management` (`ExitCfg`, default `fixed`): `breakeven` moves the stop to entry + `breakeven_lock_r` R once a closed bar is `breakeven_at_r` in favour; `breakeven_trail` then trails `trail_atr` ATR behind the best close from `trail_start_r` (floor: breakeven). The stop only moves to a level the bar closed beyond. Target and time stop unchanged. `simulate(exit_rule=...)` is the real exit for the journal tracker and backtests; with a managed exit the plain fixed exit is kept (`fixed` in the result, `extra.fixed_exit` in the journal, `fixed_pnl_r` / `fixed_avg_r` in backtests). New exit reason `breakeven_stop`.
  - Setup `mean_reversion` (`backend/signals/setups.py`, checked only when a rule set configures `strategies.mean_reversion`): a bar outside the Bollinger band (`lookback`, `band_std`) within `probe_bars`, then a rejection close back inside; stop beyond the probe extreme (`ctx["stop_anchor"]`); target `mean` (band middle) or `band_opposite`. `requires_regime_hard: true` rejects outside the required regime even in scored mode.
  - `global.selection.hard_permission_regimes`: in these regimes the permission matrix is hard in both modes (not valid, still shadow-tracked).
  - Rule sets: `be_trail_1r` (live + managed exit) and `sideways_mr_1r` (sideways_v1 + SIDEWAYS = range reversal and mean reversion only + managed exit). Live config unchanged.
  - Backtest summary `funnel` (`engine.Funnel`): trading days, days skipped (RBI/Budget, history), bars/stock-days checked, market regime per day, and rejection reasons per setup (numbers stripped). Shown on `/rule-backtests`. `summary.exit_mode` records the exit used.
- **One open trade per instrument, score bands, train/test split, weekly post-mortem** (agreed 22 Nov 2026; spec: "Trading Agent — Futures & Option Spread Strategy Spec" doc, build step 1):
  - `global.limits.one_open_trade_per_instrument` (default true). Live: `backend/signals/selection.py` `choose_shortlist()` is used by every scan (hourly futures/MCX, ORB, cash swing, cup & handle); a symbol with a shortlisted/approved journal record still `pending`/`open` in the same book (journal segment + symbol; hourly futures and ORB share `stock_futures`) is not shortlisted, nor is a second setup on the same symbol in one scan. Skipped rows stay valid, get a `selection` stage and `reject_reason`, are journaled as `not_selected` and shadow-tracked; scan results list `skipped_open_trade`.
  - Backtests: `engine.apply_position_books()` runs after every runner when the switch is on: per group (all trackable, passed all rules, shortlisted) a signal on a symbol whose counted trade hasn't exited is flagged (`stacked_all` / `stacked_passed`) and left out of every table; the shortlist is re-chosen per scan (ORB: per day) from free symbols. Summary adds `stacked_signals`, `stacking`, `by_score_band` (0-40 … 80-100) and, with `split_date` (request field + date input), `by_period` = train (signals before) vs test (on/after) for groups, setups and score bands.
  - Journal user columns `user_action` (taken/skipped), `user_fill_price`, `user_exit_price`, `user_note`; `PUT /api/journal/{id}/action`; a "You" column on `/journal` for shortlisted/approved rows. The simulated outcome is never overwritten.
  - Weekly post-mortem (`backend/journal/postmortem.py`, table `weekly_postmortems`, page `/postmortem`): scheduler job `postmortem:weekly` Saturday from 08:00 (Sunday catches up; once per week, skips weeks already stored), runs on weekends and while paused. Cause tags per closed record (`tag_trade()`: bad_entry, gave_back_profit, gap_loss, stop_too_tight — target hit within 5 bars after a stop, against_trend, event_hit, left_on_table, clean_win/clean_loss); books paper (shortlisted/approved) vs shadow vs taken; previous 4 weeks; latest backtest of the same segment; rule adherence (trades per day/week, positions open at once, daily loss stop). One AI review (deep model from Settings) with at most 3 proposed experiments; groups under 30 trades are labelled; skipped while paused; never changes rules. API: `GET /api/journal/postmortem`, `GET /api/journal/postmortem/{date in week}`, `POST /api/journal/postmortem/run {week_start?, with_ai}`.
- **Delete backtest runs; your fills and exits in the post-mortem** (asked 22 Nov 2026):
  - `engine.delete_run()` / `delete_finished_runs()`; `DELETE /api/rule-backtests/{id}` (409 while running) and `DELETE /api/rule-backtests` (all non-running). Two-click delete per run and "Delete all finished" on `/rule-backtests`. Old post-mortems keep the backtest numbers they copied.
  - The "You" column on `/journal` now takes your fill, your exit and a note (the reason when skipped). `postmortem.user_result()`: `entry_diff_r` (your fill vs plan, + = worse), `user_r` (your fill to your exit, in planned R), `deviation_r` (yours minus the rules'), tag your_exit_better / worse / same_as_rules (±0.25R). Summary `your_execution` (averages, total deviation, skip notes) is shown on `/postmortem` and given to the AI review.
- **AI panel + Telegram trade alerts** (asked 22 Nov 2026; `backend/journal/panel.py`, config `global.ai_panel`):
  - Every scan (hourly futures/MCX, ORB, cash swing, cup & handle) ends with `review_after_scan(now)` (off with `TRADINGAGENTS_AI_PANEL=off`; tests set it). `review_pending()` takes shortlisted records with no panel decision whose entry window is still open (`fill_until > now`), oldest signal then best score first. Skipped while paused or when `ai_panel.enabled` is false.
  - Order per trade: portfolio limits first (`limit_block(today, segment)`: approved trades today vs `max_new_trades_day` and this week vs `max_new_trades_week` across all books; approved pending/open positions and open risk per book — F&O book (stock/index futures, MCX, options) uses `global.risk.max_open_positions` / `max_open_risk_pct` (10 / 10% since 22 Nov 2026), cash swing and cup & handle use their instrument block's limits; today's realised loss vs `drawdown.daily_loss_pct`) → `panel_decision = not_sent` with the reason; then the daily AI budget (`limits.max_ai_evaluations_day`, counted from `panel.evaluated_at`) → `not_sent` + optional "rules only" alert (`notify_unreviewed`); then one LLM call (deep model from Settings, `timeout_seconds`) with the trade plan, stages and recent daily/intraday bars. The AI answers JSON approve/reject + confidence + reasons/risks (`parse_reply()`); it can't change entry/stop/target/size.
  - Approve (and confidence ≥ `min_confidence`) → `decision = approved`, `is_paper = 1`, stages ai_panel/final_approval pass, Telegram alert (`build_alert()`: contract, direction, limit entry + valid-until, stop, target, R:R, lots/shares, score, regime, time stop, AI reasons/risks). Reject or low confidence → `rejected_panel` with `reject_reason` (still shadow-tracked, frees the symbol). AI error → stays shortlisted, `panel_decision = error`, alert marked "rules only, AI review failed". Telegram not configured → recorded as "not sent", never fails the scan.
  - `POST /api/journal/panel/run`; "Run AI panel" button and AI confidence/reasons (tooltip) on `/journal`.
- **Option data — spread strategy step 1** (asked 22 Nov 2026; `backend/options/`, config `global.option_data`, page `/options-data`): data only, no signals. `bhavcopy.py` downloads and parses the exchange F&O UDiFF bhavcopy (NSE `BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip` for NIFTY/BANKNIFTY, BSE `BhavCopy_BSE_FO_0_0_0_<YYYYMMDD>_F_0000.CSV` for SENSEX; URL templates in the YAML; the BSE path was not verifiable from the sandbox). Keeps index options + index futures of the configured symbols; expiry = actual expiry column. `black76.py` (pure): price, implied vol (bisection), delta/gamma/vega (per vol point)/theta (per day), parity forward. `enrich()`: forward per expiry from put-call parity at the strike where call ≈ put (traded strikes first), else the same-expiry future, else spot; IV/Greeks on every priced option row (none on expiry day). Tables `option_eod` (PK date+symbol+expiry+strike+type; `FUT` rows with strike 0), `option_files` (ok/missing/error per date+exchange), `india_vix` (Yahoo `^INDIAVIX`). `jobs.py`: `backfill(start, end)` (one job at a time, skips stored days, retries missing/error, progress for the page, ≤ `max_history_days`), `daily_update()` (last `recent_days` trading days; `complete` once today's files are stored for every exchange), `import_file()` for manual uploads. Scheduler job `options:eod` on trading days from `update_after` (19:00) every `retry_minutes` until `update_until`; needs no Kite login and runs while paused (data only); off with `TRADINGAGENTS_OPTION_DATA=off` (tests). Store helpers for the next step: `chain()`, `expiries()`, `contract_history()`, `vix_on()`. API: `GET /api/options/status`, `GET|POST /api/options/backfill`, `POST /api/options/update`, `POST /api/options/upload?exchange=` (raw .csv/.zip body), `GET /api/options/chain?symbol=&date=&expiry=`.
- **Weekly backups** (asked 22 Nov 2026; `backend/backup.py`, `backend/routers/backups.py`, Settings → Backups card): zips in `<TRADINGAGENTS_HOME>/backups` (0700, files 0600; `TRADINGAGENTS_BACKUP_DIR` overrides). `essentials` = DB copy (SQLite online backup API) without `candles` / `option_eod` / `instruments`, settings whose key matches api_key/secret/token/password/chat_id/oauth_state/kite_profile blanked, plus `memory/*.json` and a README; also sent to the Telegram chat (`sendDocument`, skipped above 45 MB). `full` = whole DB with secrets + memories, server only. Keeps 8 essentials / 2 full. Scheduler job `backup:weekly` (`backup_due()`: Sundays from 02:00, or any time once the last essentials backup is 8+ days old; a failed run retries after 6 h); runs while paused and without Kite; off with `TRADINGAGENTS_BACKUP=off` (tests). API: `GET /api/backups`, `POST /api/backups {kind, to_telegram}`, `GET /api/backups/{name}/download` (name must match `ita-(essentials|full)-YYYYMMDD-HHMMSS.zip`). Restore: stop the backend, put `trading_agent.db` back in `~/.tradingagents/`, start.
- **RSI divergence alerts** (asked 22 Nov 2026; `backend/signals/divergence.py`, `backend/routers/divergence.py`, config `global.divergence`, page `/divergence`): regular divergence on daily bars, information only (no AI review, no paper trade, no portfolio limits), alerted on Telegram whether or not anything is shortlisted. Bullish = confirmed 5-bar swing low lower than the previous one 5–60 bars earlier while RSI(14) is higher by `min_rsi_gap`, first RSI low ≤ `bullish_rsi_max` (40), no lower low in between, close below the 50 EMA at the second low (`require_trend`); bearish mirrored (≥ 60, above the EMA). Signal bar = second swing + `swing_bars` (point in time). `outcome()`: risk = second swing ± `stop_atr` ATR; −1R at the invalid level (stop first), +2R target, else the close after `track_bars` (20); MFE/MAE, 5/10/20-day move in the signal's favour. Table `divergence_signals` (unique symbol+timeframe+kind+second swing date, so each divergence is alerted once; only signals confirmed in the last 3 bars, data no older than 5 days; stocks need ₹`min_turnover_cr` cr turnover). Scheduler: NSE (F&O stocks + NIFTY/BANKNIFTY spot) right after the cash swing / cup scans; MCX (active mini contracts) after the MCX daily candle sync; skipped while paused; open alerts re-simulated each run. One Telegram digest per run (`max_alerts_per_message`) with the history line. `run_study()` / `study()` replays the rule over all stored daily bars with and without the trend filter, by instrument type and year, against a baseline 10-day move from every day; saved in `divergence_studies`. API: `GET /api/divergence`, `POST /api/divergence/run?group=nse|mcx`, `GET|POST /api/divergence/study`.
- **Divergence follow-up** (agreed 22 Nov 2026 after the first history study: bullish +0.08R/PF 1.15 in every year, bearish −0.04R/PF 0.93 as a short; the trend filter passes ~99% of signals): `divergence.bearish_alert` (`all | open_longs | off`, default `open_longs`) sends bearish divergences only as an **exit warning** for symbols held long (`open_long_symbols()`: shortlisted/approved journal longs not closed, waiting/open long ideas, positions with quantity > 0); others are stored with `alert_status = "not sent: bearish_alert=…"` and still tracked. Every signal now records `uptrend` (close above the `uptrend_sma` 200-day SMA); `bullish_context: pullback_in_uptrend` would alert only those (default `any`, unchanged until the study's test half agrees). The study adds `above/below 200 SMA` groups and train/test halves split at `study_split_date` (2025-06-01), shown on `/divergence`.
- **Risk-adjusted metrics** (asked 22–23 Nov 2026; `backend/metrics.py` `risk_metrics([(exit_date, R)])`): `sharpe_trade` / `sortino_trade` (per trade), `sharpe_annual` / `sortino_annual` (daily R curve: each trade's R on its exit day, zero on other weekdays, × √252) and `calmar` (R per year ÷ max drawdown in R). Added to `journal.learning.stats()` (so every backtest group, the journal learning report and the post-mortem books carry them) and to `divergence._summary()` (divergence, chart patterns and ideas). Shown as Sharpe / Sortino / Calmar columns on `/rule-backtests`, the study tables and `/postmortem`. Live bullish divergence alerts now use `bullish_context: below_uptrend_sma` (23 Nov 2026 study: below the 200 SMA +0.115R train / +0.126R test, above it −0.387R / +0.036R).
- **Chart pattern alerts** (asked 22 Nov 2026 from shared chart ideas; `backend/signals/chart_patterns.py`, `backend/routers/chart_patterns.py`, config `global.chart_patterns`, page `/chart-patterns`): information-only daily alerts like RSI divergence, same outcome model (`divergence.outcome()`, now with a per-signal `target_r`), tracked in `pattern_signals` (unique symbol+pattern+direction+date) and studied over stored history (`pattern_studies`, background thread with progress). Patterns (both directions): `trendline_bounce` — best unbroken line through ≥ `min_touches` confirmed swings among the last `max_swings` (touch = within `touch_atr` ATR, broken = a close `break_atr` ATR through), span `min_span`–`max_span`; the bar touches and closes strongly back (close beyond open, in the right half); invalid `stop_atr` ATR beyond the line/bar extreme; a parallel line through ≥ 2 opposite swings = channel, target the other line when ≥ `min_channel_rr` R. `trendline_break` — close `break_atr` beyond a falling resistance (rising support for shorts) after the previous close was not, volume ≥ `volume_mult` × `volume_lookback`-day average (not tested without volume), invalid beyond the last swing. `triangle_breakout` — ≥ 2 swing highs within `flat_tol_pct` over `triangle_lookback` bars spanning ≥ 10 bars with rising swing lows, first close above the flat side on volume (descending mirrored). All skip a stop wider than `max_stop_atr` ATR; `near_sma` flag (200-day SMA ± `near_sma_pct`); `cooldown_bars` per pattern+direction; outcome over `track_bars` (30). Same scheduler hooks as divergence (`_divergence()` now runs divergence, patterns and the ideas refresh). API: `GET /api/chart-patterns`, `POST /api/chart-patterns/run?group=`, `GET|POST /api/chart-patterns/study`.
- **Ideas tracker** (asked 22 Nov 2026; `backend/ideas.py`, `backend/routers/ideas.py`, page `/ideas`): trade ideas from other sources (X posts, chats) logged with symbol, long/short, entry type (`market` next open / `limit` at or better / `breakout` on a break), entry, invalid level, optional target, idea date, horizon (default 40 days), source and pattern tag. `evaluate()` (pure): fill within 5 days; −R at the invalid level checked first (gap through it exits at the open), target, else the close after the horizon; MFE/MAE. Prices: stored daily candles for F&O stocks/indices, else Yahoo (`SYMBOL.NS`, NIFTY/BANKNIFTY/SENSEX index tickers). `refresh_ideas()` after the NSE close; `record()` by source / pattern / direction; the weekly post-mortem summary has `ideas` (closed this week + record by source), shown on `/postmortem` and given to the AI review. API: `GET|POST /api/ideas`, `DELETE /api/ideas/{id}`, `POST /api/ideas/refresh`.
- **My futures trades** (asked 23 Nov 2026; `backend/my_trades.py`, `backend/routers/my_trades.py`, config `global.my_trades`, page `/my-trades`): you enter your own NFO/MCX futures trades (underlying → contract from `futures_for()`, active future preselected; side, lots, entry price + time, initial stop, optional target, interval 60minute or 10minute (NFO only), trail ATR). After each synced bar of that interval/exchange the scheduler calls `update_trades()`: `follow()` re-runs `journal.simulate._run_exit()` from the entry on the contract's bars (bars starting at/after the entry time; ATR = `atr_period` ATR of the bars before the entry) with the trade's own copy of `breakeven_at_r` / `breakeven_lock_r` / `trail_start_r` / `trail_atr` — the same managed exit as the engine, stop only moves to a level a bar closed beyond, never loosens. States: open (stop stage initial / breakeven / trailing), `stop_hit`, `target_hit`, `closed` (your exit price → realised R and ₹ with lots × point value; MCX uses `instruments.mcx.value_per_point`). Telegram on every stop move and on a hit (`notify_telegram`). `build_sync_plan()` adds the contracts of open trades (any expiry) so their bars stay current. API: `GET|POST /api/my-trades`, `GET /api/my-trades/contracts?symbol=&exchange=`, `POST /api/my-trades/refresh`, `POST /api/my-trades/{id}/close`, `DELETE /api/my-trades/{id}`.
- **Wolfe Wave** (agreed 23 Nov 2026; `backend/signals/wolfe.py`, config `instruments.wolfe`, page `/wolfe-scan`): bullish only, cash, daily bars, its own book (max 3 positions, ₹2L each), inside the shared daily AI budget and trade limits. Points 1–5 = the last five alternating confirmed 5-bar swings (`alternating_swings()`: same-side swings collapsed to the more extreme one, only swings confirmed by the signal bar): lows 1 > 3 > 5, highs 2 > 4, 4 above 1, the 2–4 line falling faster than 1–3 (`require_convergence`), bars 3→5 `min_time_ratio`–`max_time_ratio` × bars 1→3, span `min_span_bars`–`max_span_bars`, point 5 within `line_tolerance_atr` above to `max_overshoot_atr` below the 1–3 line. Signal = the day point 5 is confirmed. Buy limit at the signal close next session; stop `stop_atr` ATR under point 5 (≤ `max_stop_pct`); target = the 1–4 line on the signal day, ≥ `min_rr` R; no time stop since 23 Nov 2026 (`time_stop: false`, safety exit after `max_hold_trading_days` 120; `hold_trading_days` 20 is only the event window); results/RBI/Budget checked for the first `event_block_days`. Score 100: symmetry 25, R:R 25, sweet zone 15, convergence 15, RSI higher at 5 than at 3 20. `run_scan()` runs after the cup & handle scan → journal (segment `wolfe`) → AI panel → Telegram; a stock already held in any cash book (cash swing, cup & handle, Wolfe) is not shortlisted. **Per-book exit:** `config.exit_rule_for(segment)` merges an instrument block's `exit_management` over the global one; Wolfe uses breakeven at 1R then a 2 ATR trail (tracker + backtests; other books unchanged). Backtest segment `wolfe` (`run_wolfe()`, `find_all()` point in time). The scan card draws the pattern (`components/scans/WolfeChart.tsx`: candles, points 1–5, lines 1–3/2–4, the 1–4 target line, entry/stop/target). API `GET /api/scans/wolfe`, `POST /api/scans/wolfe/run`. Tests `tests/test_wolfe.py`.
- **Fibonacci pullback + change-of-character flip** (asked 23 Nov 2026; `backend/signals/setups.py`, rule sets `config/experiments/fib_pullback.yaml` and `fib_choch.yaml`; live config unchanged): optional setups, checked only when a rule set configures them. `_impulse()` finds the latest break of structure (the highest high of the last `lookback` bars, the most recent confirmed swing high below it, the first close above that swing, and the lowest low in between as the impulse origin; shorts mirrored, bars before the signal bar only). `fib_pullback` (with the trend; HTF must agree like pullback/breakout): the signal bar dips into the `zone` retracement (0.6–0.7) and closes back as a rejection candle; one entry per break (an earlier rejection in the zone uses it up); invalid once a close passes the origin; stop beyond the pullback extreme (`stop: retrace`) or the origin (`stop: origin`); quality +5 in the 60–66% "golden" band. `choch_flip`: an opposite structure that reached its zone and then closes beyond its origin for the first time → trade the other way, stop beyond the bounce extreme. New target mode `target: fixed_r` with `target_r` (2.5 for the pullback, 2 for the flip; capped by `max_rr`). Both rule sets = live + breakeven/trail exit; permissions: fib_pullback with the trend in STRONG_UP/DOWN (HIGH_VOL half risk, with trend only), choch_flip counter-trend at half risk and both ways in SIDEWAYS. Candidates carry `fib` (extreme, origin, structure level, zone, depth). Tests `tests/test_fib_pullback.py`.
- **Momentum book** (asked 23 Nov 2026; `backend/momentum/` — `rules.py` pure, `book.py` live paper book, `backtest.py` portfolio backtest; config `instruments.momentum`; page `/momentum`): NSE stock futures, its own book, paper and rules only (no AI panel), Telegram on every entry and exit. **Ranking** after the first close of each week (`run_ranking()`, table `momentum_ranks`): each F&O stock's return over `lookbacks_days` (63/126) ending `skip_days` (21) ago minus NIFTY's, averaged; stocks with stale data or under `min_turnover_cr` are left out; long group = `top_n` strongest, short group = `top_n` weakest; a held stock stays while ranked within `buffer_n` of its end. **Entry** (`hourly_run()` after each NSE hourly bar): daily trend agrees (`ema_fast` beyond `ema_slow`, close beyond the slow EMA, `return_days` 20/60 returns in the trade direction), then on the active future's hourly bars a touch of the hourly 20 EMA within `pullback_bars` and the first close beyond the last `structure_bars` extreme; ban (or unknown), results within `results_block_days`, optional `min_oi_value_cr` and the 1%-risk lot check (`size_lots()`; a stock where one lot risks more is skipped) are hard. Stop `stop_atr` × daily ATR, trail `trail_atr` × daily ATR behind the best hourly close (`follow()`, stop first, gaps at the open). **Exits after the close** (`daily_run()`): daily EMA reversal, leaving the buffer group, `max_hold_days`. `long_enabled` / `short_enabled` switch the sides separately (short off by default), `max_positions_per_side` 3. Table `momentum_positions`. Scheduler `_momentum("hourly"|"daily")` after the NSE 1h scan and after the cash swing scan. **Portfolio backtest** (`backtest.run()`, background thread, table `momentum_backtests`): weekly re-rank on closes up to the day before, hourly entries/stops (the daily bar where hourly history is missing, counted as `daily_only`), daily mark-to-market equity in R → book Sharpe/Sortino (annualised), Calmar, max drawdown, return % at 1R = 1%; by side, exit reason, year and train/test (`split_date`); limitations listed (survivorship, spot for futures, no lots/costs/ban/results history). API `GET /api/momentum/`, `POST /api/momentum/rank`, `POST /api/momentum/scan`, `GET|POST /api/momentum/backtests`, `GET|DELETE /api/momentum/backtests/{id}`. Tests `tests/test_momentum.py`.
- **Index option credit spreads** (agreed 22 Nov 2026, built 23 Nov; `backend/options/spreads.py`, config `instruments.option_spreads`, page `/option-spreads`): NIFTY / BANKNIFTY / SENSEX from the stored evening option files (EOD closes, stored delta). Strategy by the index's daily regime (`regime_strategy`: STRONG_UP bull put, STRONG_DOWN bear call, SIDEWAYS iron condor; none in HIGH_VOL/UNSTABLE; SENSEX uses NIFTY via `regime_source`). Short leg = |delta| inside `short_delta` (0.15–0.20) closest to the middle with OI ≥ `min_oi`; long leg `wing` points further out. Entry window per symbol (`weekdays` Mon=0, `min_days`–`max_days` calendar days to expiry: NIFTY Wed–Fri before the Tuesday expiry, SENSEX Fri–Mon before Thursday, BANKNIFTY 10–20 days out); credit ≥ `min_credit_ratio` × width; India VIX ≤ `max_vix`; no RBI/Budget before expiry. Exits on daily closes: take profit at `take_profit_pct` % of the credit, stop at `stop_multiple` × credit (1R = one credit), else the last trading day before expiry. Lots: 1R ≤ 1% of capital and worst case (width − credit) ≤ `max_loss_pct`. Live `run_eod()` runs right after the evening option download completes (scheduler `options:spreads`; skipped while paused): follows open spreads, records new candidates in `option_spreads` (one per index while one is shortlisted/approved), AI review per candidate sharing the daily AI budget (`journal.panel.evaluations_today()` now counts spreads too), Telegram on approval and on every approved exit; rejected/unreviewed ones are still tracked. `backtest()` replays the rules over stored chains (by symbol, strategy, exit, year, train/test; limitations: close-only stops, close fills, no costs). API `GET /api/options/spreads`, `POST /api/options/spreads/run`, `POST /api/options/spreads/backtest`. Tests `tests/test_option_spreads.py`.
- **Deep Analysis survives a lost connection** (asked 23 Nov 2026, iPad screen off mid-run): `routers/analysis._emit()` records every streamed event in `_tasks[id]["progress"]` (reports, debates, risk debates, heartbeat, signal) before sending it, and a failed send never stops the run. `GET /api/analysis/{id}` returns the saved result with `status: completed`, or `{status: running|error, error, progress}`; `GET /api/analysis/tasks/recent` lists runs since the backend started. The store (`lib/store.ts`) keeps the running task in `localStorage`, and `resume()` catches up on page load, on `visibilitychange`/`pageshow` and 3 s after the WebSocket drops, then reopens the stream. Errors are kept in memory only (history still holds completed runs); a backend restart loses a run that hadn't finished, and the page says so.
- **Fixes 23 Nov 2026 (first paper-trading day):**
  - Deep Analysis / AI backtest run the Market (technical) analyst only: `backend/models.py` `ALLOWED_ANALYSTS = ["market"]` (requests naming news/social/fundamentals get a 422); the UI no longer offers or lists those agents (agreed 21 Nov 2026, AI works on price data only).
  - News feed: `news_sources.is_market_relevant()` keeps only market/economy articles (`MARKET_TERMS`; an `OFF_TOPIC_TERMS` hit — sport, travel, politics, entertainment — needs ≥ 2 market words). `GET /api/news/?market_only=false` returns everything; the response has `filtered_out`; the News page shows the hidden count with a "Show all" toggle.
  - Equity portfolio page: the local store is **Holdings** ("Sync holdings from Kite" = `kite.holdings()`, used by the review, concentration and divergence exit warnings). Live F&O / intraday / MCX positions come from `kite.positions()["net"]` via `GET /api/positions/kite-open` (`brokers/kite.normalize_positions`, open rows only; read-only, not stored), shown in the "Open positions" panel.
  - Top Picks "View all": `lib/recommendations-cache.ts` shares the Today card's result (15 min) with `/recommendations`, which otherwise auto-runs the default scan and opens on the first non-empty tab.
  - **No 3-day time stop for futures/MCX** (agreed): `instruments.<x>.time_stop: false` + an instrument-level `exit_management` (`breakeven_trail`: stop to breakeven at 1R, then 2 ATR behind the best close) for stock_futures, index_futures and mcx. A trade ends at the target, the stop, the breakeven stop or the trailing stop; `time_stop_date` is now only the safety exit = last session before the contract's expiry (`fno.last_session_before()`). `hold_trading_days: 3` remains the RBI/Budget/results check window. `DecisionConfig.exit_rule(segment)` picks the instrument override or `global.exit_management` (still `fixed`; every live instrument now overrides it); the tracker, backtests (`_simulate(..., instrument)`; spot proxies follow a trade up to `max_hold_trading_days`, default 20) and the Telegram alert ("Exit: target, stop or trailing stop … no time stop") use it. ORB (segment stock_futures) also trails but still closes at 15:15.
  - **Cash swing and cup & handle also trail** (asked 23 Nov 2026): same `time_stop: false` + `breakeven_trail`; shares have no expiry, so `time_stop_date` = safety exit after `max_hold_trading_days` (cash swing 60, cup & handle 120). Events stay checked over `hold_trading_days` (cash 7) / `event_block_days` (cup 5). Cup & handle candidates now carry daily ATR(14) (`cup_handle._last_atr`), which the trailing stop needs. The tracker reads up to 400 daily bars.
  - **Trailing stop while open + "move your SL" alerts** (asked 23 Nov 2026): `simulate()` open results carry `trail_stop`, `stop_state` (initial / breakeven / trailing) and `last_close`; every tracker run (after each NSE/MCX hourly scan and the daily cash scan) saves `extra.stop_now` {level, state, locked_r, mfe_r, last_close, at, alerted_level, alert} (`tracker._stop_now`), shown as the **Stop now** column on `/journal`. `panel.stop_move_alert()` sends a Telegram "Move your stop-loss" (old SL → new SL, state, R locked) for trades you were alerted on (`was_alerted()`: approved, or `panel.alert == "sent"` for rules-only / AI-error alerts — `_notify(..., store=True)` now records it): once when the stop leaves the initial stop, then each further `global.stop_alerts.min_move_r` (0.25R); a failed send retries on the next run. The post-mortem's `deviation_r` already compares your exit with the trailed (rules') exit.
  - **No silent days** (asked 23 Nov 2026): (1) `backend/watchdog.py` — `stall_reason()` (pure) / `check_and_warn()`, called every scheduler tick (`_watchdog`): Telegram "scans stalled" when an exchange is open ≥ 75 min and no hourly scan has completed for 75 min (since the last OK scan, the scheduler start or the session open), or "Kite is not logged in" during market hours; one message per stall, never while paused; `GET /api/scheduler/status` adds `last_scan_ok` and `stalled`. (2) `panel.limit_notice()` — a shortlisted trade blocked by a portfolio limit (daily/weekly trade limit, open positions, open risk, daily loss stop) now sends one Telegram note per limit reason per day (`panel_notices` table; numbers stripped from the reason; failed sends retry), switch `global.ai_panel.notify_limit_blocks`. (3) `GET /api/journal/usage` (`panel.usage_today()`: AI reviews used/limit, approved today/week vs limits, F&O open vs max, current `limit_block`) shown as a "Today:" line on `/journal`.
  - **Per-book AI reviews + shadow strategies** (agreed 23 Nov 2026): `global.ai_panel.reviews_per_day` gives each review book its own share — futures (stock + index) 3, MCX 1, cash 2, option spreads 1 — inside `limits.max_ai_evaluations_day` (7). `ai_panel.reserved_after.futures = {after: "13:15", reviews: 1}`: before 13:15 only 2 futures reviews can be used, so the afternoon scans keep one (`panel.book_share_now()`, `review_budget_block(now, book)`). `ai_panel.top_per_scan: 1`: per (review book, `signal_ts`) only the top-scoring shortlisted trade goes to the AI; the rest are `not_sent` ("lower score than X in the same scan") without an alert and stay shadow-tracked (`_select_for_review()`). Option spreads: candidates of one evening run ranked by credit/width, top one reviewed. `global.shadow_strategies` (`cup_handle`, `wolfe`, `orb`, `momentum`; `GlobalCfg.is_shadow(segment, setup)`): still scanned and journal-tracked, never sent to the AI or alerted (momentum book Telegram off too). A book quota hit still sends the "rules only" alert for the top trade. `/api/journal/usage` adds `ai_books` + `shadow_strategies`, shown on the `/journal` "Today:" line. Tests `tests/test_review_quotas.py`.
  - **Today tab regime:** the badge now leads with the trading engine's regime (`GET /api/regime/engine` → `signals.regime.explain_current()` on the stored Kite NIFTY daily: STRONG_UP / STRONG_DOWN / SIDEWAYS / HIGH_VOL / UNSTABLE with close, EMA20/50 + slope, ADX, vol ratio) and shows the old Top Picks regime (`market_regime.py`, Yahoo, 50/200 SMA) underneath. The old classifier still labels "price above both SMAs, 50 SMA below 200 SMA" as SIDEWAYS (its stats keys are unchanged) but now says "Recovery not yet confirmed" (mirror case: "Pullback in an uptrend not yet resolved").
  - **Simulation → "Top Picks Tracker"**: same page and API, relabelled for its purpose — paper trades of the Top Picks recommender (Track button) + the recommender replay, feeding Signal Performance / calibration / Learning Insights. The trading engine's paper trades are in the Trade Journal.
  - `/divergence`: "Signals found" table shows the Telegram status per signal (bearish ones on symbols not held long are tracked but not sent) and when each was found.
- **NIFTY option buying, 5-minute** (asked 23 Nov 2026; `backend/signals/nifty_options.py`, config `instruments.nifty_options`, page `/nifty-options`; **shadow** — in `global.shadow_strategies`, no AI review or Telegram until it shows an edge): signals on the NIFTY index's own 5-minute bars (new interval `5minute` in `candles.INTERVALS` / `TIMEFRAMES`; the sync plan adds NIFTY 5m + the options of pending/open signals; scheduler `candles:NSE:5minute` then `scan:nifty_opt` after each 5-minute bar, only while the block is enabled — `state["nifty_options"]`). Three setups tracked side by side (`setups`): `opt_vwap_ema` (close beyond the session time-weighted average — the index has no volume — with EMA9 beyond EMA21, first close beyond the last 3 bars), `opt_orb` (first close beyond the 15-minute opening range) and `opt_supertrend` (Supertrend(10, 3) flip); all need the 15-minute trend (completed 15-minute bars vs their EMA21) to agree. Signals from `first_signal` 09:30 to `last_signal` 14:30 (bar end), none on RBI/Budget days, none on the weekly expiry day from `expiry_day_cutoff` 13:00, at most `max_signals_per_setup_day` 2 per setup. Long → ATM call, short → ATM put of the nearest expiry (`atm_option()`, strike rounded to `strike_step`). Stop beyond the last 3 bars' extreme + 0.1 ATR (0.5–2 ATR of the 5-minute ATR14), target 2R, own `exit_management` (breakeven at 1R, 2 ATR trail), out with the bar ending 15:15 (`extra.exit_time`, tracker `mark_exit`). Journal segment `nifty_options` (review book futures if shadow is switched off); R is on the index, ₹ is estimated with `value_per_point` = lot × `assumed_delta` 0.5; `option_premium_move()` reads the option's real premium at the fill and exit from its stored 5-minute bars. Backtest segment `nifty_options_proxy` (`engine.run_nifty_options()`, 5-minute NIFTY history added to the history download, each setup its own position book; limitations: R on the index only, premium decay/IV/spread not modelled, expiry weekday Thursday before 1 Sep 2025 / Tuesday after). API `GET /api/scans/nifty-options`, `POST /api/scans/nifty-options/run`. Tests `tests/test_nifty_options.py`.
- **Fixes 24 Nov 2026 (first holiday: NSE shut, MCX evening only):**
  - **Run now on a closed day:** `cash_swing.run_scan(force=True)` on a holiday/weekend scans the last session's daily bars (entries for the next session) unless that session's scan already ran OK (`_last_ok_run()`; then it says so and points at that run). Hourly futures/MCX don't re-scan an old session (the entry window of its last bar is already over): `fno.closed_reason()` / `waiting_reason()` say why, when the next bar closes (MCX evening-only days: first bar 18:00) and when the last scan ran.
  - **Option spreads Run now:** `spreads.run_now()` runs `run_eod()` on the latest day with stored option files (today's arrive ~19:00; holidays have none) and adds a `note`; safe to repeat (unique per index/day, only new candidates reviewed). The scheduler still calls `run_eod(today)`.
  - **Momentum long/short switches:** the badges on `/momentum` are buttons; `book.set_side()` / `PUT /api/momentum/sides/{side}` `{enabled: bool|null}` stores an override in `settings.momentum_side_switches` over the YAML `long_enabled` / `short_enabled` (null = back to the YAML). New entries only; open positions are still followed.
  - **SENSEX futures for My trades:** the morning instrument refresh also keeps BFO-FUT SENSEX and the BSE `SENSEX` spot (`store.BFO_INDICES`; a failed BFO/BSE dump is logged and skipped, never fails the refresh). `my_trades.EXCHANGES["BFO"]` (NSE hours/holidays); NIFTY / BANKNIFTY / SENSEX quick-pick buttons on `/my-trades`. The hourly index-futures scan is unchanged (NIFTY, BANKNIFTY).
  - **My trades:** `check_entry_time()` rejects an entry outside the exchange's session (holiday, closed morning session, after hours) or in the future. `fetch_bars()` downloads the contract's bars from Kite (from ~6–10 days before the entry, for the ATR, up to now) when a trade is added and on Recalculate (`recalculate()`, `POST /api/my-trades/refresh`); without a Kite login the row says "Log in to Kite to fetch bars" (`data_note`). Followed bars must start inside a session (`in_session()`; a bar stamped after the close never moves or hits a stop). New columns `best_close`, `data_note`; the table shows bar size, trail ATR in points, best close, last bar date/time and the stop-hit time.
  - **Today's P&L** (`backend/pnl_today.py`, `GET /api/pnl/today?day=`, card `components/dashboard/TodayPnl.tsx` on the Today page, refreshes every minute): My futures trades (closed today at your exit; stop/target hit today at the hit price until you close it; open at the last close) and system paper trades (AI-approved journal records closed today with ₹ after charges or open at the tracker's last close via `tracker._size`; approved option spreads closed today or open at the last evening value). Realised / open ₹ and R per book and in total. Nothing stored: it reads what the trackers keep current. Tests `tests/test_holiday_fixes.py`.
- **Indian time everywhere:** the backend sets `TZ=Asia/Kolkata` at startup (override `TRADINGAGENTS_TZ`); SQL uses `datetime('now', 'localtime')`; `db.install_localtime_triggers()` adds an AFTER INSERT trigger to older tables whose columns still default to UTC `datetime('now')`. Rows written before this change keep their UTC times.
- Tests: `tests/test_marketdata.py`, `tests/test_scheduler.py`, `tests/test_signals.py`, `tests/test_journal.py`, `tests/test_fno_scan.py`, `tests/test_decision_config.py`, `tests/test_rule_backtests.py`, `tests/test_two_stage_and_cup.py`, `tests/test_sideways.py`, `tests/test_orb.py`, `tests/test_exit_and_mean_reversion.py`, `tests/test_one_trade_and_postmortem.py`, `tests/test_delete_and_user_exits.py`, `tests/test_ai_panel.py`, `tests/test_option_data.py`, `tests/test_backup.py`, `tests/test_divergence.py`, `tests/test_chart_patterns_and_ideas.py`, `tests/test_metrics.py`, `tests/test_my_trades.py`, `tests/test_review_quotas.py` (fake Kite client and synthetic bars, no network).

## Remaining Phases (Unimplemented)

### Pre-Kite Hardening
- ✅ Sector concentration checker — prevents over-exposure to single sector
- ✅ Daily Verdict synthesizer — single trade-or-skip decision
- ✅ **Phase 4a: Kite read-only sync** — live portfolio/holdings (order-free). ⚠️ Margin visibility still pending.
- **Phase 4b: Kite order placement** — one-click bracket orders with safety checks (NOT implemented; Kite stays read-only)

### Future Big Features
- **Options/Futures Phase 3** — Derivatives analyst, option chains, Greeks, PCR, lot sizes
- **Real-time signal loop Phase 5** — Intraday scanning during market hours

### Nice-to-haves
- News sentiment analysis (auto-score articles as bullish/bearish)
- Stock-specific news on Analysis page
- Promoter activity tracker (NSE bulk/block deals)
- Comparative analysis (side-by-side stock comparison)
- Export analysis reports as PDF
- Multi-stock batch analysis queue
