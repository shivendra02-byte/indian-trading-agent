"""AI panel: one Claude review per shortlisted trade (agreed 22 Nov 2026).

After every scan, shortlisted journal records whose entry window is still open
go to the panel, best score first:

1. Portfolio limits are checked first (trades today / this week, open positions,
   open risk, daily loss stop). A trade over a limit is not sent; it stays
   shortlisted with the reason.
2. Shadow strategies (``global.shadow_strategies``) are never sent or alerted.
   In each scan only the top-scoring trade of each review book goes on
   (``ai_panel.top_per_scan``); the rest are ``not_sent`` without an alert.
3. At most ``global.limits.max_ai_evaluations_day`` AI calls a day in total and
   ``ai_panel.reviews_per_day[book]`` per book (futures / mcx / cash /
   option_spreads). Past that, trades are marked ``not_sent`` (and alerted as
   "rules only" when ``ai_panel.notify_unreviewed`` is on).
4. The AI sees the rules' trade plan and recent price context and answers
   approve/reject with a confidence. It can't change entry, stop, target or size.
   Approve needs ``ai_panel.min_confidence``.
5. Approved → journal ``decision = approved``, ``is_paper = 1``, Telegram alert.
   Rejected → ``rejected_panel`` (still shadow-tracked). An AI error leaves the
   trade shortlisted and says so in the alert.

Skipped entirely while the system is paused. Never places orders.
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import get_config
from backend.journal.store import ensure_journal_table, list_records, update_record

log = logging.getLogger(__name__)

APPROVED = "approved"
REJECTED = "rejected_panel"
CASH_SEGMENTS = ("cash_swing", "cup_handle", "wolfe")
SETUP_LABEL = {"mcx_trend": "trend pullback (4H trend, 2H pullback, 1H trigger)"}   # alert wording (25 Nov 2026)
SEGMENT_LABEL = {"stock_futures": "Stock futures", "index_futures": "Index futures", "mcx": "MCX",
                 "cash_swing": "Cash swing", "cup_handle": "Cup & handle", "wolfe": "Wolfe Wave"}


# --- limits ------------------------------------------------------------------------------

def _count(where: str, params: tuple) -> int:
    ensure_journal_table()
    with get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM trade_journal WHERE {where}", params).fetchone()[0]


def evaluations_today(today: date) -> int:
    """AI calls answered today (approve or reject), trade journal plus option spreads: one
    shared daily budget. Failed calls (panel_decision 'error') don't count: they never got an
    answer, and a broken model or key must not use up the day's budget (23 Nov 2026: 5 failures did)."""
    from backend.options.spreads import evaluations_today as spread_evaluations
    journal = _count("panel_decision IN ('approve', 'reject') AND substr(json_extract(panel, '$.evaluated_at'), 1, 10) = ?",
                     (today.isoformat(),))
    return journal + spread_evaluations(today)


FNO_BOOK = ("stock_futures", "index_futures", "mcx", "index_options", "stock_options")

# Review books: each has its own share of the daily AI reviews (ai_panel.reviews_per_day).
REVIEW_BOOKS = {"futures": ("stock_futures", "index_futures", "nifty_options"), "mcx": ("mcx",), "cash": CASH_SEGMENTS,
                "option_spreads": ()}


def review_book(segment: str) -> str:
    for book, segments in REVIEW_BOOKS.items():
        if segment in segments:
            return book
    return "option_spreads" if segment == "option_spreads" else segment


def book_evaluations_today(today: date, book: str) -> int:
    """Answered AI reviews today in one review book."""
    if book == "option_spreads":
        from backend.options.spreads import evaluations_today as spread_evaluations
        return spread_evaluations(today)
    segments = REVIEW_BOOKS.get(book, (book,))
    marks = ", ".join("?" * len(segments))
    return _count(f"panel_decision IN ('approve', 'reject') AND segment IN ({marks}) "
                  "AND substr(json_extract(panel, '$.evaluated_at'), 1, 10) = ?", (*segments, today.isoformat()))


def book_share_now(book: str, now: datetime) -> int | None:
    """The book's usable share of reviews at ``now``: the reserved part only from its time on."""
    ap = get_config().global_.ai_panel
    share = ap.reviews_per_day.get(book)
    reserve = ap.reserved_after.get(book)
    if share is None or reserve is None:
        return share
    return share if now.strftime("%H:%M") >= reserve.after else share - reserve.reviews


def review_budget_block(now: datetime | date, book: str) -> str | None:
    """Why no AI review is left for ``book`` at ``now`` (total cap first, then the book's share), or None."""
    g = get_config().global_
    if not isinstance(now, datetime):
        now = datetime.combine(now, datetime.max.time())
    today = now.date()
    if evaluations_today(today) >= g.limits.max_ai_evaluations_day:
        return f"daily AI limit reached ({g.limits.max_ai_evaluations_day})"
    share = g.ai_panel.reviews_per_day.get(book)
    usable = book_share_now(book, now)
    if usable is not None and book_evaluations_today(today, book) >= usable:
        reserve = g.ai_panel.reserved_after.get(book)
        if usable < (share or 0) and reserve:
            return f"{book} AI reviews used before {reserve.after} ({usable} of {share}; {reserve.reviews} kept for later)"
        return f"{book} AI reviews used ({share}/day)"
    return None


def _book_segments(segment: str) -> tuple[str, ...]:
    """Position books: every F&O segment shares one book; cash swing and cup & handle have their own."""
    return FNO_BOOK if segment in FNO_BOOK else (segment,)


def _record_risk_pct(row) -> float:
    """% of capital a journal record risks: lots × |entry − stop| × value per point for futures;
    the default per-trade risk otherwise (cash, or no lot size recorded)."""
    g = get_config().global_
    try:
        extra = json.loads(row["extra"] or "{}") if isinstance(row["extra"], str) else (row["extra"] or {})
    except (TypeError, ValueError):
        extra = {}
    lots, vpp = extra.get("lots"), extra.get("value_per_point")
    if lots and vpp and row["entry"] is not None and row["stop"] is not None:
        return 100 * lots * abs(row["entry"] - row["stop"]) * vpp / g.capital
    return g.risk.risk_per_trade_pct


def limit_block(today: date, segment: str = "stock_futures", new_risk_pct: float | None = None) -> str | None:
    """Why no new trade can be approved in ``segment``'s book right now, or None.
    Trades per day/week and the daily loss stop count every book; open positions and
    open risk count the trade's own book (F&O: ``global.risk``; cash swing / cup &
    handle: their instrument block's ``max_open_positions`` / ``max_open_risk_pct`` /
    ``max_new_trades_day`` when set)."""
    g = get_config().global_
    week_start = today - timedelta(days=today.weekday())
    approved_today = _count("decision = ? AND signal_date = ?", (APPROVED, today.isoformat()))
    if approved_today >= g.limits.max_new_trades_day:
        return f"daily trade limit reached ({approved_today}/{g.limits.max_new_trades_day})"
    approved_week = _count("decision = ? AND signal_date >= ?", (APPROVED, week_start.isoformat()))
    if approved_week >= g.limits.max_new_trades_week:
        return f"weekly trade limit reached ({approved_week}/{g.limits.max_new_trades_week})"

    book = _book_segments(segment)
    marks = ", ".join("?" * len(book))
    max_positions, max_risk, book_day = g.risk.max_open_positions, g.risk.max_open_risk_pct, None
    if book != FNO_BOOK:
        extra = get_config().instrument(segment).model_extra or {}
        max_positions = int(extra.get("max_open_positions", max_positions))
        max_risk = float(extra.get("max_open_risk_pct", max_risk))
        book_day = extra.get("max_new_trades_day")
    if book_day is not None:
        book_today = _count(f"decision = ? AND signal_date = ? AND segment IN ({marks})",
                            (APPROVED, today.isoformat(), *book))
        if book_today >= int(book_day):
            return f"{segment} daily trade limit reached ({book_today}/{book_day})"
    open_now = _count(f"decision = ? AND track_status IN ('pending', 'open') AND segment IN ({marks})",
                      (APPROVED, *book))
    if open_now >= max_positions:
        return f"open position limit reached ({open_now}/{max_positions})"
    with get_db() as conn:   # actual risk of open positions (an MCX lot may risk up to 3.5%, 25 Nov 2026)
        rows = conn.execute(f"SELECT entry, stop, extra FROM trade_journal WHERE decision = ? "
                            f"AND track_status IN ('pending', 'open') AND segment IN ({marks})",
                            (APPROVED, *book)).fetchall()
    open_risk = sum(_record_risk_pct(r) for r in rows)
    new_risk = g.risk.risk_per_trade_pct if new_risk_pct is None else new_risk_pct
    if open_risk + new_risk > max_risk + 1e-9:
        return f"open risk would exceed {max_risk}% ({open_risk:.1f}% open + {new_risk:.1f}% new)"
    with get_db() as conn:
        realised = conn.execute("SELECT COALESCE(SUM(pnl_rupees), 0) FROM trade_journal WHERE decision = ? "
                                "AND track_status = 'closed' AND substr(exit_ts, 1, 10) = ?",
                                (APPROVED, today.isoformat())).fetchone()[0]
    stop = g.capital * g.drawdown.daily_loss_pct / 100
    if realised <= -stop:
        return f"daily loss stop hit (₹{realised:,.0f})"
    return None


# --- prompt and AI call ------------------------------------------------------------------

def _recent_bars(rec: dict) -> dict:
    from backend.marketdata.candles import get_candles
    out = {}
    try:
        daily = get_candles(rec["instrument_token"], "day", limit=20)
        if daily:
            out["daily_last_20"] = [[b["ts"][:10], round(b["open"], 2), round(b["high"], 2), round(b["low"], 2),
                                     round(b["close"], 2)] for b in daily]
        if rec["segment"] not in CASH_SEGMENTS:
            interval = (rec.get("extra") or {}).get("bar_interval", "60minute")
            intraday = get_candles(rec["instrument_token"], interval, limit=12)
            out[f"{interval}_last_12"] = [[b["ts"][:16], round(b["open"], 2), round(b["high"], 2), round(b["low"], 2),
                                           round(b["close"], 2), b.get("volume")] for b in intraday]
    except Exception as exc:   # price context is a help, not a requirement
        log.debug("panel bars unavailable: %s", exc)
    return out


def _trade_plan(rec: dict) -> dict:
    keep = ("segment", "symbol", "setup", "direction", "regime", "score", "entry", "stop", "target", "rr", "atr",
            "signal_ts", "fill_until", "time_stop_date", "factors", "soft_checks")
    extra = rec.get("extra") or {}
    return {**{k: rec.get(k) for k in keep},
            "extra": {k: extra.get(k) for k in ("tradingsymbol", "expiry", "lots", "vol_ratio", "htf_trend",
                                                "rs_vs_nifty_pct", "oi_change_pct", "spread_pct", "context",
                                                "next_level", "stop_atr") if extra.get(k) is not None},
            "stages": [{k: s.get(k) for k in ("stage", "decision", "reason")} for s in (rec.get("stages") or [])]}


def build_prompt(rec: dict) -> str:
    return (
        "You are the risk reviewer for a rules-based Indian market paper-trading system. The rules engine has "
        "already found and shortlisted the trade below; entry, stop, target and size are fixed by the rules and "
        "you cannot change them. Decide whether this specific trade should be taken now.\n\n"
        "Check: does the price context support the setup; is the stop at a sensible structural level; is the "
        "target reachable within the holding period; anything in the recent bars that argues against it "
        "(exhaustion, a gap, a nearby level, a choppy market). Be strict: rejecting is fine. Do not invent news "
        "or fundamentals you were not given.\n\n"
        "Answer with ONLY a JSON object, no other text:\n"
        '{"decision": "approve" | "reject", "confidence": 0-100, '
        '"reasons": ["max 3 short reasons"], "risks": ["max 2 short risks"]}\n\n'
        f"TRADE PLAN (JSON):\n{json.dumps(_trade_plan(rec), default=str)[:12000]}\n\n"
        f"RECENT PRICES (JSON, [time, open, high, low, close(, volume)]):\n"
        f"{json.dumps(_recent_bars(rec), default=str)[:8000]}\n"
    )


def _saved_api_key(provider: str) -> str | None:
    import os
    from backend.db import get_setting
    from backend.settings_manager import PROVIDER_ENV_KEYS
    env_var = PROVIDER_ENV_KEYS.get(provider)
    if not env_var:
        return None
    return get_setting(f"api_key_{provider}") or os.environ.get(env_var) or None


def _default_llm_call(prompt: str) -> tuple[str, str]:
    from backend.settings_manager import get_llm_config
    from tradingagents.llm_clients import create_llm_client

    cfg = get_llm_config()
    model = cfg["deep_think_llm"]
    extra = {}
    key = _saved_api_key(cfg["llm_provider"])
    if key:                       # the key saved in Settings, not only what the process env had at startup
        extra["api_key"] = key
    llm = create_llm_client(provider=cfg["llm_provider"], model=model,
                            timeout=get_config().global_.ai_panel.timeout_seconds, **extra).get_llm()
    reply = llm.invoke(prompt)
    content = reply.content if hasattr(reply, "content") else str(reply)
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return content, model


def parse_reply(text: str) -> dict:
    """Pull the JSON object out of the reply; raises ValueError when it isn't usable."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("no JSON in the reply")
    data = json.loads(match.group(0))
    decision = str(data.get("decision", "")).lower().strip()
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
        raise ValueError("confidence must be 0-100")
    return {"decision": decision, "confidence": int(confidence),
            "reasons": [str(r)[:200] for r in (data.get("reasons") or [])][:3],
            "risks": [str(r)[:200] for r in (data.get("risks") or [])][:2]}


# --- alerts ------------------------------------------------------------------------------

def _size_text(rec: dict) -> str:
    extra = rec.get("extra") or {}
    if extra.get("lots"):
        text = f"{extra['lots']} lot{'s' if extra['lots'] != 1 else ''}"
        return f"{text} — {extra['size_note']}" if extra.get("size_note") else text
    if rec["segment"] in CASH_SEGMENTS:
        from backend.journal.tracker import _cash_max_value, _risk_per_trade
        risk = abs(rec["entry"] - rec["stop"])
        if risk:
            qty = min(math.floor(_risk_per_trade() / risk), math.floor(_cash_max_value(rec["segment"]) / rec["entry"]))
            return f"{qty} shares"
    return "size per your risk"


def _exit_line(rec: dict) -> str:
    from backend.decision.config import get_config
    cfg = get_config()
    inst = cfg.instruments.get(rec["segment"])
    last = html.escape(str(rec.get("time_stop_date")))
    if inst is None or inst.time_stop:
        return f"Time stop: close of {last}"
    rule = cfg.exit_rule(rec["segment"])
    if rule["mode"] == "breakeven_trail":
        how = (f"stop to breakeven at {rule['breakeven_at_r']:g}R, then trail {rule['trail_atr']:g} ATR "
               f"behind the best close")
    elif rule["mode"] == "breakeven":
        how = f"stop to breakeven at {rule['breakeven_at_r']:g}R"
    else:
        how = "fixed stop"
    safety = "before expiry" if rec["segment"] in ("stock_futures", "index_futures", "mcx") \
        else f"safety exit after {inst.max_hold_trading_days} trading days"
    return f"Exit: target, stop or trailing stop ({how}); no time stop · latest exit {last} ({safety})"


def build_alert(rec: dict, panel: dict, kind: str) -> str:
    """``kind``: approved / unreviewed / ai_error."""
    e = html.escape
    extra = rec.get("extra") or {}
    arrow = "BUY" if rec["direction"] == "long" else "SELL"
    title = {"approved": "AI-approved trade", "unreviewed": "Shortlisted (rules only, AI limit reached)",
             "ai_error": "Shortlisted (rules only, AI review failed)"}[kind]
    name = extra.get("tradingsymbol") or rec["symbol"]
    until = (rec.get("fill_until") or "").replace("T", " ")[:16]
    lines = [
        f"<b>{e(title)}</b>",
        f"<b>{arrow} {e(name)}</b> · {e(SEGMENT_LABEL.get(rec['segment'], rec['segment']))} · {e(SETUP_LABEL.get(rec['setup'], rec['setup']))}",
        f"Entry (limit): {rec['entry']} · valid until {e(until)}",
        f"Stop: {rec['stop']} · Target: {rec['target']} · R:R {rec.get('rr')}",
        f"Size: {e(_size_text(rec))} · Score {round(rec.get('score') or 0)} · Regime {e(str(rec.get('regime') or '-'))}",
        _exit_line(rec),
    ]
    if kind == "ai_error" and panel.get("error"):
        lines.append(e(panel["error"]))
    if kind == "approved":
        lines.append(f"AI confidence: {panel.get('confidence')}")
        lines += [f"• {e(r)}" for r in panel.get("reasons", [])]
        if panel.get("risks"):
            lines.append("Risks: " + e("; ".join(panel["risks"])))
    lines.append("Paper trading only: mark Taken/Skipped on the Trade Journal.")
    return "\n".join(lines)


def _send(text: str) -> str:
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(text)
        return "sent"
    except TelegramConfigError as exc:
        return f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("telegram alert failed: %s", exc)
        return f"failed: {exc}"


def _notify(rec: dict, panel: dict, kind: str, store: bool = False) -> str:
    status = _send(build_alert(rec, panel, kind))
    if store:   # remembered so the stop-move messages follow only trades you were told about
        update_record(rec["id"], {"panel": {**panel, "alert": status, "alert_kind": kind}})
    return status


def was_alerted(rec: dict) -> bool:
    return (rec.get("panel") or {}).get("alert") == "sent" or rec.get("decision") == APPROVED


def build_stop_move(rec: dict, old_level: float, stop_now: dict) -> str:
    e = html.escape
    extra = rec.get("extra") or {}
    name = extra.get("tradingsymbol") or rec["symbol"]
    arrow = "↑ long" if rec["direction"] == "long" else "↓ short"
    state = {"breakeven": "breakeven", "trailing": "trailing stop"}.get(stop_now["state"], stop_now["state"])
    lines = [
        "<b>Move your stop-loss</b>",
        f"<b>{e(name)}</b> {arrow} · {e(SEGMENT_LABEL.get(rec['segment'], rec['segment']))} · {e(SETUP_LABEL.get(rec['setup'], rec['setup']))}",
        f"SL {old_level:g} → <b>{stop_now['level']:g}</b> ({e(state)}) · locks {stop_now['locked_r']:+.2f}R",
    ]
    if stop_now.get("last_close") is not None:
        lines.append(f"Last close {stop_now['last_close']:g} · best so far {stop_now.get('mfe_r', 0):+.2f}R · "
                     f"target {rec['target']:g}")
    lines.append("Paper trading only: update your Kite SL order if you took this trade.")
    return "\n".join(lines)


def stop_move_alert(rec: dict, stop_now: dict, risk: float) -> dict:
    """Send a "move your stop" message when the managed stop of an alerted trade has moved far
    enough since the last message (or left the initial stop). Returns ``stop_now`` with the
    alert bookkeeping (``alerted_level``, ``alert``) added."""
    cfg = get_config().global_.stop_alerts
    prev = (rec.get("extra") or {}).get("stop_now") or {}
    out = {**stop_now, "alerted_level": prev.get("alerted_level"), "alert": prev.get("alert")}
    if not cfg.enabled or not was_alerted(rec) or stop_now["state"] == "initial" or risk <= 0:
        return out
    last = prev.get("alerted_level") if prev.get("alerted_level") is not None else rec["stop"]
    s = 1 if rec["direction"] == "long" else -1
    first_move = prev.get("alerted_level") is None
    if not first_move and s * (stop_now["level"] - last) < cfg.min_move_r * risk:
        return out
    if s * (stop_now["level"] - last) <= 0:
        return out
    status = _send(build_stop_move(rec, last, stop_now))
    out.update(alert=status, alert_at=datetime.now().isoformat(timespec="seconds"))
    if status == "sent":
        out["alerted_level"] = stop_now["level"]
    return out


# --- the panel ---------------------------------------------------------------------------

def _set_stages(rec: dict, panel_stage: dict, final_stage: dict) -> list[dict]:
    stages = [s for s in (rec.get("stages") or []) if s.get("stage") not in ("ai_panel", "final_approval")]
    return [*stages, panel_stage, final_stage]


def pending_records(now: datetime, retry_errors: bool = False) -> list[dict]:
    """Shortlisted, not yet reviewed (or, with ``retry_errors``, whose AI call failed),
    entry window still open; best score first."""
    reviewed = "(panel_decision IS NULL OR panel_decision = 'error')" if retry_errors else "panel_decision IS NULL"
    rows = list_records(f"decision = 'shortlisted' AND {reviewed} AND track_status = 'pending' "
                        "AND fill_until > ?", (now.isoformat(timespec="seconds"),), limit=200)
    return sorted(rows, key=lambda r: (r["signal_ts"], -(r.get("score") or 0)))


def _notice_key(reason: str) -> str:
    """'daily trade limit reached (5/5)' -> 'daily trade limit reached': one notice per reason a day."""
    return re.sub(r"\s+", " ", re.sub(r"[\d.,₹%/()+-]+", " ", reason)).strip()


def limit_notice(day: date, reason: str, rec: dict) -> str | None:
    """Telegram note the first time today a shortlisted trade is blocked for ``reason`` by a
    portfolio limit (asked 23 Nov 2026: such trades used to be dropped silently). None when
    already sent today for this reason."""
    key = _notice_key(reason)
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS panel_notices (day TEXT NOT NULL, key TEXT NOT NULL, "
                     "status TEXT, at TEXT, PRIMARY KEY (day, key))")
        row = conn.execute("SELECT status FROM panel_notices WHERE day = ? AND key = ?",
                           (day.isoformat(), key)).fetchone()
    if row and row[0] == "sent":
        return None
    e = html.escape
    name = (rec.get("extra") or {}).get("tradingsymbol") or rec["symbol"]
    text = "\n".join([
        "<b>Trade not reviewed: portfolio limit</b>",
        f"{e(reason[:1].upper() + reason[1:])}.",
        f"{e(name)} ({e(SEGMENT_LABEL.get(rec['segment'], rec['segment']))} · {e(SETUP_LABEL.get(rec['setup'], rec['setup']))} "
        f"{'↑' if rec['direction'] == 'long' else '↓'}) was shortlisted but blocked.",
        "Other trades blocked for the same reason today are not messaged; see the Trade Journal (not_sent).",
    ])
    status = _send(text)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO panel_notices (day, key, status, at) VALUES (?, ?, ?, ?)",
                     (day.isoformat(), key, status, datetime.now().isoformat(timespec="seconds")))
    return status


def usage_today(today: date) -> dict:
    """What is left today: AI reviews and trade limits (Trade Journal header)."""
    g = get_config().global_
    week_start = today - timedelta(days=today.weekday())
    marks = ", ".join("?" * len(FNO_BOOK))
    return {
        "date": today.isoformat(),
        "ai_reviews": evaluations_today(today), "ai_limit": g.limits.max_ai_evaluations_day,
        "ai_books": {book: {"used": book_evaluations_today(today, book), "limit": g.ai_panel.reviews_per_day.get(book),
                            "reserved": (g.ai_panel.reserved_after[book].model_dump()
                                         if book in g.ai_panel.reserved_after else None)}
                     for book in REVIEW_BOOKS},
        "shadow_strategies": list(g.shadow_strategies),
        "approved_today": _count("decision = ? AND signal_date = ?", (APPROVED, today.isoformat())),
        "max_new_trades_day": g.limits.max_new_trades_day,
        "approved_week": _count("decision = ? AND signal_date >= ?", (APPROVED, week_start.isoformat())),
        "max_new_trades_week": g.limits.max_new_trades_week,
        "fno_open": _count(f"decision = ? AND track_status IN ('pending', 'open') AND segment IN ({marks})",
                           (APPROVED, *FNO_BOOK)),
        "fno_max_open": g.risk.max_open_positions,
        "limit_block": limit_block(today, "stock_futures"),
    }


def _skip(rec: dict, reason: str, out: dict) -> None:
    """Not sent to the AI and not alerted; the record stays shortlisted and shadow-tracked."""
    from backend.signals.setups import stage
    update_record(rec["id"], {"panel": {"evaluated_at": None, "skipped": reason}, "panel_decision": "not_sent",
                              "stages": _set_stages(rec, stage("ai_panel", "not_sent", reason),
                                                    stage("final_approval", "pending", "rules only"))})
    out["not_sent"].append({"symbol": rec["symbol"], "reason": reason})


def _select_for_review(records: list[dict], cfg, out: dict) -> list[dict]:
    """Drop shadow strategies, then keep the ``top_per_scan`` best-scoring trades of each
    (review book, scan) — a scan is one ``signal_ts``. The others are skipped quietly."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        if cfg.is_shadow(rec["segment"], rec.get("setup")):
            _skip(rec, "shadow strategy: tracked, not reviewed", out)
            continue
        groups.setdefault((review_book(rec["segment"]), str(rec["signal_ts"])), []).append(rec)
    chosen = []
    for _key, recs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        recs.sort(key=lambda r: -(r.get("score") or 0))
        top = recs[:cfg.ai_panel.top_per_scan]
        chosen.extend(top)
        best = top[0]
        for rec in recs[cfg.ai_panel.top_per_scan:]:
            _skip(rec, f"lower score than {best['symbol']} ({best.get('score')}) in the same scan", out)
    return chosen


def review_pending(now: datetime | None = None, llm_call: Callable[[str], tuple[str, str]] | None = None,
                   retry_errors: bool = False) -> dict:
    """Review every pending shortlisted trade. Safe to call after every scan."""
    from backend.signals.setups import stage
    from backend.system_state import is_paused

    cfg = get_config().global_
    if not cfg.ai_panel.enabled:
        return {"status": "skipped", "reason": "AI panel disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    now = now or datetime.now()
    out = {"status": "ok", "approved": [], "rejected": [], "not_sent": [], "errors": [], "alerts": []}
    for rec in _select_for_review(pending_records(now, retry_errors), cfg, out):
        evaluated_at = now.isoformat(timespec="seconds")
        blocked = limit_block(now.date(), rec["segment"], _record_risk_pct(rec))
        if blocked:
            panel = {"evaluated_at": None, "skipped": blocked}
            update_record(rec["id"], {"panel": panel, "panel_decision": "not_sent", "stages": _set_stages(
                rec, stage("ai_panel", "not_sent", blocked), stage("final_approval", "fail", blocked))})
            out["not_sent"].append({"symbol": rec["symbol"], "reason": blocked})
            if cfg.ai_panel.notify_telegram and cfg.ai_panel.notify_limit_blocks:
                status = limit_notice(now.date(), blocked, rec)
                if status:
                    out["alerts"].append({"symbol": rec["symbol"], "kind": "limit_block", "status": status})
            continue
        reason = review_budget_block(now, review_book(rec["segment"]))
        if reason:
            panel = {"evaluated_at": None, "skipped": reason}
            update_record(rec["id"], {"panel": panel, "panel_decision": "not_sent", "stages": _set_stages(
                rec, stage("ai_panel", "not_sent", reason), stage("final_approval", "pending", "rules only"))})
            out["not_sent"].append({"symbol": rec["symbol"], "reason": reason})
            if cfg.ai_panel.notify_telegram and cfg.ai_panel.notify_unreviewed:
                out["alerts"].append({"symbol": rec["symbol"], "kind": "unreviewed",
                                      "status": _notify(rec, panel, "unreviewed", store=True)})
            continue
        try:
            text, model = (llm_call or _default_llm_call)(build_prompt(rec))
            verdict = parse_reply(text)
        except Exception as exc:
            log.warning("AI panel call failed for %s", rec["symbol"], exc_info=True)
            detail = " ".join(str(exc).split())[:160]
            reason = f"AI review failed: {type(exc).__name__}" + (f": {detail}" if detail else "")
            panel = {"evaluated_at": evaluated_at, "error": reason}
            update_record(rec["id"], {"panel": panel, "panel_decision": "error", "stages": _set_stages(
                rec, stage("ai_panel", "fail", reason), stage("final_approval", "pending", "rules only"))})
            out["errors"].append({"symbol": rec["symbol"], "reason": reason})
            if cfg.ai_panel.notify_telegram:
                out["alerts"].append({"symbol": rec["symbol"], "kind": "ai_error",
                                      "status": _notify(rec, panel, "ai_error", store=True)})
            continue
        approve = verdict["decision"] == "approve" and verdict["confidence"] >= cfg.ai_panel.min_confidence
        panel = {**verdict, "evaluated_at": evaluated_at, "model": model,
                 "min_confidence": cfg.ai_panel.min_confidence, "approved": approve}
        why = "; ".join(verdict["reasons"])[:300]
        if approve:
            update_record(rec["id"], {"panel": panel, "panel_decision": "approve", "decision": APPROVED,
                                      "is_paper": 1, "stages": _set_stages(
                rec, stage("ai_panel", "pass", why, confidence=verdict["confidence"]),
                stage("final_approval", "pass", "AI approved"))})
            out["approved"].append(rec["symbol"])
            if cfg.ai_panel.notify_telegram:
                out["alerts"].append({"symbol": rec["symbol"], "kind": "approved",
                                      "status": _notify(rec, panel, "approved", store=True)})
        else:
            reason = why if verdict["decision"] == "reject" else \
                f"confidence {verdict['confidence']} < {cfg.ai_panel.min_confidence}"
            update_record(rec["id"], {"panel": panel, "panel_decision": "reject", "decision": REJECTED,
                                      "reject_reason": f"AI panel: {reason}"[:300], "stages": _set_stages(
                rec, stage("ai_panel", "fail", reason, confidence=verdict["confidence"]),
                stage("final_approval", "fail", "AI rejected"))})
            out["rejected"].append(rec["symbol"])
    return out


def review_after_scan(now: datetime | None = None) -> dict:
    """Hook for the scans: never lets a panel problem fail a scan.
    ``TRADINGAGENTS_AI_PANEL=off`` turns the hook off (tests set it)."""
    if os.getenv("TRADINGAGENTS_AI_PANEL", "on").strip().lower() in {"off", "0", "false", "no"}:
        return {"status": "skipped", "reason": "TRADINGAGENTS_AI_PANEL=off"}
    try:
        return review_pending(now)
    except Exception as exc:
        log.exception("AI panel failed")
        return {"status": "error", "reason": f"{type(exc).__name__}"}
