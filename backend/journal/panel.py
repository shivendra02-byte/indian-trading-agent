"""AI panel: one Claude review per shortlisted trade (agreed 22 Nov 2026).

After every scan, shortlisted journal records whose entry window is still open
go to the panel, best score first:

1. Portfolio limits are checked first (trades today / this week, open positions,
   open risk, daily loss stop). A trade over a limit is not sent; it stays
   shortlisted with the reason.
2. At most ``global.limits.max_ai_evaluations_day`` AI calls a day. Past that,
   trades are marked ``not_sent`` (and alerted as "rules only" when
   ``ai_panel.notify_unreviewed`` is on).
3. The AI sees the rules' trade plan and recent price context and answers
   approve/reject with a confidence. It can't change entry, stop, target or size.
   Approve needs ``ai_panel.min_confidence``.
4. Approved → journal ``decision = approved``, ``is_paper = 1``, Telegram alert.
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
SEGMENT_LABEL = {"stock_futures": "Stock futures", "index_futures": "Index futures", "mcx": "MCX",
                 "cash_swing": "Cash swing", "cup_handle": "Cup & handle", "wolfe": "Wolfe Wave"}


# --- limits ------------------------------------------------------------------------------

def _count(where: str, params: tuple) -> int:
    ensure_journal_table()
    with get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM trade_journal WHERE {where}", params).fetchone()[0]


def evaluations_today(today: date) -> int:
    """AI calls made today (approve, reject or error; not limit skips), trade journal
    plus option spreads: one shared daily budget."""
    from backend.options.spreads import evaluations_today as spread_evaluations
    journal = _count("panel_decision IN ('approve', 'reject', 'error') AND substr(json_extract(panel, '$.evaluated_at'), 1, 10) = ?",
                     (today.isoformat(),))
    return journal + spread_evaluations(today)


FNO_BOOK = ("stock_futures", "index_futures", "mcx", "index_options", "stock_options")


def _book_segments(segment: str) -> tuple[str, ...]:
    """Position books: every F&O segment shares one book; cash swing and cup & handle have their own."""
    return FNO_BOOK if segment in FNO_BOOK else (segment,)


def limit_block(today: date, segment: str = "stock_futures") -> str | None:
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
    if (open_now + 1) * g.risk.risk_per_trade_pct > max_risk + 1e-9:
        return f"open risk would exceed {max_risk}%"
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


def _default_llm_call(prompt: str) -> tuple[str, str]:
    from backend.settings_manager import get_llm_config
    from tradingagents.llm_clients import create_llm_client

    cfg = get_llm_config()
    model = cfg["deep_think_llm"]
    llm = create_llm_client(provider=cfg["llm_provider"], model=model,
                            timeout=get_config().global_.ai_panel.timeout_seconds).get_llm()
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
        return f"{extra['lots']} lot{'s' if extra['lots'] != 1 else ''}"
    if rec["segment"] in CASH_SEGMENTS:
        from backend.journal.tracker import _cash_max_value, _risk_per_trade
        risk = abs(rec["entry"] - rec["stop"])
        if risk:
            qty = min(math.floor(_risk_per_trade() / risk), math.floor(_cash_max_value(rec["segment"]) / rec["entry"]))
            return f"{qty} shares"
    return "size per your risk"


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
        f"<b>{arrow} {e(name)}</b> · {e(SEGMENT_LABEL.get(rec['segment'], rec['segment']))} · {e(rec['setup'])}",
        f"Entry (limit): {rec['entry']} · valid until {e(until)}",
        f"Stop: {rec['stop']} · Target: {rec['target']} · R:R {rec.get('rr')}",
        f"Size: {e(_size_text(rec))} · Score {round(rec.get('score') or 0)} · Regime {e(str(rec.get('regime') or '-'))}",
        f"Time stop: close of {e(str(rec.get('time_stop_date')))}",
    ]
    if kind == "approved":
        lines.append(f"AI confidence: {panel.get('confidence')}")
        lines += [f"• {e(r)}" for r in panel.get("reasons", [])]
        if panel.get("risks"):
            lines.append("Risks: " + e("; ".join(panel["risks"])))
    lines.append("Paper trading only: mark Taken/Skipped on the Trade Journal.")
    return "\n".join(lines)


def _notify(rec: dict, panel: dict, kind: str) -> str:
    from backend.notifications.telegram import TelegramConfigError, TelegramSendError, send_html_message
    try:
        send_html_message(build_alert(rec, panel, kind))
        return "sent"
    except TelegramConfigError as exc:
        return f"not sent: {exc}"
    except TelegramSendError as exc:
        log.warning("telegram alert failed: %s", exc)
        return f"failed: {exc}"


# --- the panel ---------------------------------------------------------------------------

def _set_stages(rec: dict, panel_stage: dict, final_stage: dict) -> list[dict]:
    stages = [s for s in (rec.get("stages") or []) if s.get("stage") not in ("ai_panel", "final_approval")]
    return [*stages, panel_stage, final_stage]


def pending_records(now: datetime) -> list[dict]:
    """Shortlisted, not yet reviewed, entry window still open; best score first."""
    rows = list_records("decision = 'shortlisted' AND panel_decision IS NULL AND track_status = 'pending' "
                        "AND fill_until > ?", (now.isoformat(timespec="seconds"),), limit=200)
    return sorted(rows, key=lambda r: (r["signal_ts"], -(r.get("score") or 0)))


def review_pending(now: datetime | None = None, llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
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
    for rec in pending_records(now):
        evaluated_at = now.isoformat(timespec="seconds")
        blocked = limit_block(now.date(), rec["segment"])
        if blocked:
            panel = {"evaluated_at": None, "skipped": blocked}
            update_record(rec["id"], {"panel": panel, "panel_decision": "not_sent", "stages": _set_stages(
                rec, stage("ai_panel", "not_sent", blocked), stage("final_approval", "fail", blocked))})
            out["not_sent"].append({"symbol": rec["symbol"], "reason": blocked})
            continue
        if evaluations_today(now.date()) >= cfg.limits.max_ai_evaluations_day:
            reason = f"daily AI limit reached ({cfg.limits.max_ai_evaluations_day})"
            panel = {"evaluated_at": None, "skipped": reason}
            update_record(rec["id"], {"panel": panel, "panel_decision": "not_sent", "stages": _set_stages(
                rec, stage("ai_panel", "not_sent", reason), stage("final_approval", "pending", "rules only"))})
            out["not_sent"].append({"symbol": rec["symbol"], "reason": reason})
            if cfg.ai_panel.notify_telegram and cfg.ai_panel.notify_unreviewed:
                out["alerts"].append({"symbol": rec["symbol"], "kind": "unreviewed",
                                      "status": _notify(rec, panel, "unreviewed")})
            continue
        try:
            text, model = (llm_call or _default_llm_call)(build_prompt(rec))
            verdict = parse_reply(text)
        except Exception as exc:
            reason = f"AI review failed: {type(exc).__name__}"
            panel = {"evaluated_at": evaluated_at, "error": reason}
            update_record(rec["id"], {"panel": panel, "panel_decision": "error", "stages": _set_stages(
                rec, stage("ai_panel", "fail", reason), stage("final_approval", "pending", "rules only"))})
            out["errors"].append({"symbol": rec["symbol"], "reason": reason})
            if cfg.ai_panel.notify_telegram:
                out["alerts"].append({"symbol": rec["symbol"], "kind": "ai_error",
                                      "status": _notify(rec, panel, "ai_error")})
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
                                      "status": _notify(rec, panel, "approved")})
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
