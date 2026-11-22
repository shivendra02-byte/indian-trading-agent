"""Weekly post-mortem over the trade journal (agreed 22 Nov 2026).

Every Saturday from 08:00 IST (Sunday catches up a missed Saturday) the week's
closed journal records are reviewed:

* each trade gets cause tags computed from stored data (no AI, no cost);
* the week is summarised by setup, direction, regime and segment, next to the
  previous four weeks and the latest backtest of the same segment;
* the paper book (shortlisted/approved) is compared with shadow records and with
  the trades you marked as taken;
* rule adherence: trade limits, open positions and the daily loss stop;
* one AI review (Claude) turns that into what went right/wrong and at most three
  proposed experiments. It never changes rules: proposals go through a backtest
  and your approval. The AI call is skipped while the system is paused.

Results are stored in ``weekly_postmortems`` (one row per week, re-runs replace it).
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import get_config
from backend.journal.learning import MIN_SAMPLE, stats
from backend.journal.store import list_records

log = logging.getLogger(__name__)

RUN_AT = time(8, 0)                      # Saturday, after Friday's MCX close
CASH_SEGMENTS = ("cash_swing", "cup_handle")
PAPER_DECISIONS = ("shortlisted", "approved")
STOP_EXITS = ("stop", "gap_stop")
AFTER_EXIT_BARS = 5

TAGS = {
    "bad_entry": "Stopped out without ever reaching +0.5R",
    "gave_back_profit": "Reached +1R or more, then closed at or below 0R",
    "gap_loss": "Exit on a gap through the stop",
    "stop_too_tight": f"Stopped out, then the original target was hit within {AFTER_EXIT_BARS} bars",
    "against_trend": "Higher timeframe or daily trend was against the trade at entry",
    "event_hit": "RBI / Budget / FOMC inside the holding window",
    "left_on_table": "Winner whose best move was 1.5R or more above what it banked",
    "clean_win": "Winner with none of the above",
    "clean_loss": "Loser with none of the above",
}

USER_DEVIATION_R = 0.25                 # your result vs the rules' result: smaller gaps are noise

# Journal segment (+ ORB setup) -> backtest segment used for the expectation.
BACKTEST_SEGMENT = {"cash_swing": "cash_swing", "cup_handle": "cup_handle",
                    "stock_futures": "stock_futures_proxy", "index_futures": "index_futures_proxy"}


# --- weeks -------------------------------------------------------------------------------

def week_bounds(d: date) -> tuple[date, date]:
    """Monday and Sunday of the week containing ``d``."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def postmortem_due(now: datetime, last_done: date | None) -> date | None:
    """Week start to review at ``now`` (naive IST): Saturday from 08:00 or any time
    on Sunday, once per week. Pure, for the scheduler."""
    wd = now.weekday()
    if wd < 5 or (wd == 5 and now.time() < RUN_AT):
        return None
    start, _ = week_bounds(now.date())
    return None if last_done == start else start


# --- storage -----------------------------------------------------------------------------

def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_postmortems (
                week_start TEXT PRIMARY KEY,
                week_end TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                summary TEXT,
                trades TEXT,
                ai_status TEXT,
                ai_model TEXT,
                ai_review TEXT
            )""")


def exists(week_start: date) -> bool:
    ensure_table()
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM weekly_postmortems WHERE week_start = ?",
                            (week_start.isoformat(),)).fetchone() is not None


def list_postmortems(limit: int = 52) -> list[dict]:
    ensure_table()
    with get_db() as conn:
        rows = conn.execute("SELECT week_start, week_end, created_at, summary, ai_status FROM weekly_postmortems "
                            "ORDER BY week_start DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        summary = json.loads(r["summary"] or "{}")
        out.append({"week_start": r["week_start"], "week_end": r["week_end"], "created_at": r["created_at"],
                    "ai_status": r["ai_status"], "headline": (summary.get("books") or {}).get("paper")})
    return out


def get_postmortem(week_start: str) -> dict | None:
    ensure_table()
    with get_db() as conn:
        r = conn.execute("SELECT * FROM weekly_postmortems WHERE week_start = ?", (week_start,)).fetchone()
    if not r:
        return None
    item = dict(r)
    for f in ("summary", "trades"):
        item[f] = json.loads(item[f]) if item.get(f) else None
    return item


def _save(start: date, end: date, summary: dict, trades: list[dict], ai: dict) -> None:
    ensure_table()
    with get_db() as conn:
        conn.execute("""INSERT OR REPLACE INTO weekly_postmortems
                        (week_start, week_end, created_at, summary, trades, ai_status, ai_model, ai_review)
                        VALUES (?, ?, datetime('now', 'localtime'), ?, ?, ?, ?, ?)""",
                     (start.isoformat(), end.isoformat(), json.dumps(summary), json.dumps(trades),
                      ai.get("status"), ai.get("model"), ai.get("review")))


# --- per-trade cause tags ----------------------------------------------------------------

def _sign(rec: dict) -> int:
    return 1 if rec["direction"] == "long" else -1


def _bars_after_exit(rec: dict, n: int = AFTER_EXIT_BARS) -> list[dict]:
    from backend.marketdata.candles import get_candles
    exit_ts = rec.get("exit_ts")
    if not exit_ts:
        return []
    interval = "day" if rec["segment"] in CASH_SEGMENTS else (rec.get("extra") or {}).get("bar_interval", "60minute")
    try:
        since = datetime.fromisoformat(exit_ts[:19])
    except ValueError:
        return []
    return [b for b in get_candles(rec["instrument_token"], interval, since=since) if b["ts"] > exit_ts][:n]


def _trend_against(rec: dict) -> bool:
    extra, s = rec.get("extra") or {}, _sign(rec)
    values = [extra.get("htf_trend"), *((extra.get("context") or {}).values())]
    return any(isinstance(v, (int, float)) and v * s < 0 for v in values)


def _event_in_hold(rec: dict) -> list[str]:
    from backend.signals import events
    try:
        first = date.fromisoformat((rec.get("fill_ts") or rec["entry_date"])[:10])
        last = date.fromisoformat((rec.get("exit_ts") or rec["time_stop_date"])[:10])
        found = events.market_events_between(first, last, kinds=("RBI", "BUDGET", "FOMC"))["events"]
    except Exception:
        return []
    return [f"{e['event']} {e['date']}" for e in found]


def tag_trade(rec: dict, bars_after: list[dict] | None = None, event_names: list[str] | None = None) -> list[str]:
    """Cause tags for one closed record. ``bars_after``/``event_names`` are looked up
    when not given (tests pass them in)."""
    pnl, mfe = rec.get("pnl_r") or 0.0, rec.get("mfe_r") or 0.0
    tags = []
    if pnl < 0 and mfe < 0.5:
        tags.append("bad_entry")
    if mfe >= 1.0 and pnl <= 0:
        tags.append("gave_back_profit")
    if rec.get("exit_reason") == "gap_stop":
        tags.append("gap_loss")
    if rec.get("exit_reason") in STOP_EXITS:
        bars = _bars_after_exit(rec) if bars_after is None else bars_after
        s, target = _sign(rec), rec["target"]
        if any((b["high"] >= target) if s > 0 else (b["low"] <= target) for b in bars):
            tags.append("stop_too_tight")
    if _trend_against(rec):
        tags.append("against_trend")
    if (event_names if event_names is not None else _event_in_hold(rec)):
        tags.append("event_hit")
    if pnl > 0 and mfe - pnl >= 1.5:
        tags.append("left_on_table")
    if not tags:
        tags.append("clean_win" if pnl > 0 else "clean_loss")
    return tags


# --- weekly summary ----------------------------------------------------------------------

def _closed_between(first: date, last: date) -> list[dict]:
    rows = list_records("track_status = 'closed' AND substr(exit_ts, 1, 10) BETWEEN ? AND ?",
                        (first.isoformat(), last.isoformat()), limit=100_000)
    return rows


def _book(rec: dict) -> str:
    return "paper" if rec["decision"] in PAPER_DECISIONS else "shadow"


def _grouped(rows: list[dict], key: Callable[[dict], str]) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {k: stats(v) for k, v in sorted(groups.items())}


def _setup_key(r: dict) -> str:
    return f"{r['setup']} {r['direction']}"


def _backtest_expectation(segments: set[str]) -> dict:
    """Latest finished backtest per segment (same rule version preferred)."""
    live_version = get_config().rule_version
    out = {}
    try:
        with get_db() as conn:
            for seg in sorted(segments):
                bt = BACKTEST_SEGMENT.get(seg)
                if not bt:
                    continue
                rows = conn.execute("SELECT id, rule_set, rule_version, start_date, end_date, summary "
                                    "FROM rule_backtests WHERE segment = ? AND status = 'done' "
                                    "ORDER BY (rule_version = ?) DESC, id DESC LIMIT 1", (bt, live_version)).fetchall()
                if not rows:
                    continue
                r = rows[0]
                summary = json.loads(r["summary"] or "{}")
                out[seg] = {"run_id": r["id"], "rule_set": r["rule_set"], "rule_version": r["rule_version"],
                            "same_rules_as_live": r["rule_version"] == live_version,
                            "period": f"{r['start_date']} to {r['end_date']}",
                            "shortlisted": (summary.get("groups") or {}).get("shortlisted"),
                            "all_trackable": (summary.get("groups") or {}).get("all_trackable"),
                            "by_setup": summary.get("by_setup")}
    except Exception as exc:   # no backtest table yet
        log.debug("backtest expectation unavailable: %s", exc)
    return out


def _adherence(start: date, end: date) -> dict:
    """Trade limits, open positions and the daily loss stop for the paper book."""
    g = get_config().global_
    paper = list_records("decision IN ('shortlisted', 'approved') AND signal_date BETWEEN ? AND ?",
                         (start.isoformat(), end.isoformat()), limit=100_000)
    per_day = Counter(r["signal_date"] for r in paper)
    issues = []
    for day, n in sorted(per_day.items()):
        if n > g.limits.max_new_trades_day:
            issues.append(f"{day}: {n} shortlisted trades (limit {g.limits.max_new_trades_day} a day)")
    if len(paper) > g.limits.max_new_trades_week:
        issues.append(f"{len(paper)} shortlisted trades this week (limit {g.limits.max_new_trades_week})")
    # Most positions open at once (filled paper trades, by fill/exit time).
    points = []
    for r in paper:
        if r.get("fill_ts"):
            points.append((r["fill_ts"], 1))
            points.append((r.get("exit_ts") or "9999", -1))
    peak = cur = 0
    for _, step in sorted(points, key=lambda p: (p[0], p[1])):
        cur += step
        peak = max(peak, cur)
    if peak > g.risk.max_open_positions:
        issues.append(f"{peak} paper positions open at once (limit {g.risk.max_open_positions})")
    daily_loss_limit = g.capital * g.drawdown.daily_loss_pct / 100
    loss_by_day = defaultdict(float)
    for r in paper:
        if r.get("track_status") == "closed" and r.get("pnl_rupees") is not None:
            loss_by_day[(r.get("exit_ts") or "")[:10]] += r["pnl_rupees"]
    for day, pnl in sorted(loss_by_day.items()):
        if pnl <= -daily_loss_limit:
            issues.append(f"{day}: realised ₹{pnl:,.0f} (daily stop ₹{-daily_loss_limit:,.0f})")
    return {"shortlisted_per_day": dict(sorted(per_day.items())), "shortlisted_week": len(paper),
            "max_open_at_once": peak, "issues": issues}


def user_result(rec: dict) -> dict:
    """Your execution vs the plan, in R of the planned risk (entry to stop).
    ``entry_diff_r``: your fill vs the planned entry (+ = worse for you).
    ``user_r``: your fill to your exit. ``deviation_r``: your R minus the rules' R."""
    if rec.get("user_action") != "taken" or not rec.get("user_fill_price"):
        return {}
    risk = abs(rec["entry"] - rec["stop"])
    if not risk:
        return {}
    s = _sign(rec)
    out = {"entry_diff_r": round(s * (rec["user_fill_price"] - rec["entry"]) / risk, 2)}
    if rec.get("user_exit_price"):
        out["user_r"] = round(s * (rec["user_exit_price"] - rec["user_fill_price"]) / risk, 2)
        if rec.get("pnl_r") is not None:
            dev = round(out["user_r"] - rec["pnl_r"], 2)
            out["deviation_r"] = dev
            out["user_tag"] = ("your_exit_better" if dev >= USER_DEVIATION_R
                               else "your_exit_worse" if dev <= -USER_DEVIATION_R else "same_as_rules")
    return out


def _execution_summary(closed: list[dict]) -> dict:
    """How your real trades compared with the rules' version of the same trades."""
    rows = [(r, user_result(r)) for r in closed]
    filled = [u for _, u in rows if "entry_diff_r" in u]
    exited = [(r, u) for r, u in rows if "user_r" in u and "deviation_r" in u]
    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None   # noqa: E731
    return {
        "taken_with_fill": len(filled),
        "taken_with_exit": len(exited),
        "avg_entry_diff_r": avg([u["entry_diff_r"] for u in filled]),
        "your_avg_r": avg([u["user_r"] for _, u in exited]),
        "rules_avg_r_same_trades": avg([r["pnl_r"] for r, _ in exited]),
        "total_deviation_r": round(sum(u["deviation_r"] for _, u in exited), 2) if exited else None,
        "exit_better": sum(1 for _, u in exited if u["user_tag"] == "your_exit_better"),
        "exit_worse": sum(1 for _, u in exited if u["user_tag"] == "your_exit_worse"),
        "same_as_rules": sum(1 for _, u in exited if u["user_tag"] == "same_as_rules"),
        "skip_notes": [{"symbol": r["symbol"], "note": r.get("user_note")} for r in closed
                       if r.get("user_action") == "skipped" and r.get("user_note")][:20],
    }


def _trade_row(r: dict, tags: list[str]) -> dict:
    keep = ("id", "segment", "symbol", "setup", "direction", "regime", "score", "decision", "signal_ts",
            "fill_ts", "exit_ts", "exit_reason", "entry", "stop", "target", "pnl_r", "mfe_r", "mae_r",
            "pnl_rupees", "user_action", "user_fill_price", "user_exit_price", "user_note")
    return {**{k: r.get(k) for k in keep}, "book": _book(r), "tags": tags,
            "reject_reason": r.get("reject_reason"), **user_result(r)}


def build_summary(start: date, end: date, tagger: Callable[[dict], list[str]] = tag_trade) -> tuple[dict, list[dict]]:
    closed = _closed_between(start, end)
    trades = [_trade_row(r, tagger(r)) for r in closed]
    paper = [r for r in closed if _book(r) == "paper"]
    shadow = [r for r in closed if _book(r) == "shadow"]
    taken = [r for r in closed if r.get("user_action") == "taken"]
    prev = _closed_between(start - timedelta(days=28), start - timedelta(days=1))
    prev_paper = [r for r in prev if _book(r) == "paper"]
    open_now = list_records("track_status = 'open' AND decision IN ('shortlisted', 'approved')", (), limit=1000)
    tag_counts = Counter(t for row in trades for t in row["tags"])
    paper_tags = Counter(t for row in trades if row["book"] == "paper" for t in row["tags"])

    summary = {
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "rule_version": get_config().rule_version, "min_sample": MIN_SAMPLE,
        "closed": len(closed),
        "books": {"paper": stats(paper), "shadow": stats(shadow), "taken": stats(taken), "all": stats(closed)},
        "previous_4_weeks": {"paper": stats(prev_paper), "all": stats(prev),
                             "paper_by_setup": _grouped(prev_paper, _setup_key)},
        "by_setup": _grouped(paper or closed, _setup_key),
        "by_setup_all": _grouped(closed, _setup_key),
        "by_regime": _grouped(closed, lambda r: r.get("regime") or "n/a"),
        "by_segment": _grouped(closed, lambda r: r["segment"]),
        "by_exit": _grouped(closed, lambda r: r.get("exit_reason") or "?"),
        "tags": {k: tag_counts.get(k, 0) for k in TAGS},
        "paper_tags": {k: paper_tags.get(k, 0) for k in TAGS},
        "tag_labels": TAGS,
        "user_marked": {"taken": len(taken), "skipped": sum(1 for r in closed if r.get("user_action") == "skipped"),
                        "unmarked_paper": sum(1 for r in paper if not r.get("user_action"))},
        "your_execution": _execution_summary(closed),
        "open_at_week_end": [{"symbol": r["symbol"], "segment": r["segment"], "setup": r["setup"],
                              "direction": r["direction"], "mfe_r": r.get("mfe_r"), "mae_r": r.get("mae_r")}
                             for r in open_now],
        "backtest_expectation": _backtest_expectation({r["segment"] for r in closed}),
        "adherence": _adherence(start, end),
    }
    return summary, trades


# --- AI review ---------------------------------------------------------------------------

def _config_excerpt() -> dict:
    data = get_config().model_dump(by_alias=True)
    g = data.get("global") or data.get("global_") or {}
    return {"global": {k: g.get(k) for k in ("scoring", "selection", "exit_management", "permissions", "limits")},
            "strategies": data.get("strategies")}


def _prompt(summary: dict, trades: list[dict]) -> str:
    closed = sorted((t for t in trades if t.get("pnl_r") is not None), key=lambda t: t["pnl_r"])
    paper = [t for t in closed if t["book"] == "paper"] or closed
    sample = {"worst": paper[:5], "best": paper[-5:][::-1]}
    return (
        "You are reviewing one week of an Indian F&O/cash swing paper-trading journal for its owner, who executes "
        "trades manually. Results are in R (1R = the planned risk). 'paper' = trades the system shortlisted; "
        "'shadow' = candidates it tracked but did not pick; 'taken' = trades the owner marked as actually taken. "
        "'your_execution' compares the owner's own fills and exits with the rules' version of the same trades "
        "(deviation_r > 0 = the owner did better than the rules); skip_notes are the owner's reasons for skipping.\n\n"
        "Write a short review in markdown with exactly these headings:\n"
        "## What went right\n## What went wrong\n## Patterns\n## Proposed experiments\n\n"
        "Rules:\n"
        f"- Any group with fewer than {MIN_SAMPLE} trades: say 'too few trades' and do not draw a conclusion from it.\n"
        "- One week is never enough to change a rule. Prefer patterns that also show in the previous 4 weeks or "
        "the backtest.\n"
        "- At most 3 proposed experiments. Each names the exact YAML key from the config excerpt, the new value, "
        "and the backtest to run (segment + compare against the current rule set). If nothing qualifies, say so.\n"
        "- Never recommend skipping the stop, averaging down, or live order execution.\n"
        "- Plain words, numbers with units, under 400 words.\n\n"
        f"WEEK SUMMARY (JSON):\n{json.dumps(summary, default=str)[:20000]}\n\n"
        f"BEST AND WORST TRADES (JSON):\n{json.dumps(sample, default=str)[:8000]}\n\n"
        f"CONFIG EXCERPT (JSON):\n{json.dumps(_config_excerpt(), default=str)[:8000]}\n"
    )


def _default_llm_call(prompt: str) -> tuple[str, str]:
    from backend.settings_manager import get_llm_config
    from tradingagents.llm_clients import create_llm_client

    cfg = get_llm_config()
    model = cfg["deep_think_llm"]
    llm = create_llm_client(provider=cfg["llm_provider"], model=model).get_llm()
    reply = llm.invoke(prompt)
    content = reply.content if hasattr(reply, "content") else str(reply)
    if isinstance(content, list):   # some providers return content blocks
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return content.strip(), model


def ai_review(summary: dict, trades: list[dict], llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    from backend.system_state import is_paused
    if is_paused():
        return {"status": "skipped: system paused (run again after resuming)"}
    if not trades:
        return {"status": "skipped: no closed trades this week"}
    try:
        text, model = (llm_call or _default_llm_call)(_prompt(summary, trades))
    except Exception as exc:
        log.warning("post-mortem AI review failed: %s", exc)
        return {"status": f"error: {type(exc).__name__}"}
    return {"status": "ok", "model": model, "review": text}


# --- entry point -------------------------------------------------------------------------

def run_weekly_postmortem(week_start: date | None = None, with_ai: bool = True,
                          llm_call: Callable[[str], tuple[str, str]] | None = None,
                          tagger: Callable[[dict], list[str]] = tag_trade) -> dict:
    """Review the week starting ``week_start`` (default: the current week) and store it.
    Read-only on the journal: nothing here changes rules or records."""
    start, end = week_bounds(week_start or date.today())
    summary, trades = build_summary(start, end, tagger)
    ai = ai_review(summary, trades, llm_call) if with_ai else {"status": "skipped: not requested"}
    _save(start, end, summary, trades, ai)
    return {"status": "ok", "week_start": start.isoformat(), "week_end": end.isoformat(),
            "closed": summary["closed"], "ai_status": ai["status"]}
