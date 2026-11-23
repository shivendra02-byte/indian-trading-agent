"""Index option credit spreads (NIFTY, BANKNIFTY, SENSEX) on end-of-day chains.

Config ``instruments.option_spreads``. Agreed spec (22 Nov 2026):

* strategy by the index's daily regime: STRONG_UP → bull put spread, STRONG_DOWN →
  bear call spread, SIDEWAYS → iron condor; nothing in HIGH_VOL / UNSTABLE
  (``regime_strategy``). SENSEX uses the NIFTY regime (no stored SENSEX candles).
* short leg = the option whose |delta| is inside ``short_delta`` (0.15–0.20),
  closest to the middle, with open interest ≥ ``min_oi``; long leg ``wing`` points
  further out (per symbol).
* entry window per symbol: weekdays and calendar days to expiry (NIFTY Wed–Fri
  before the Tuesday expiry, SENSEX Fri–Mon before Thursday, BANKNIFTY monthly
  10–20 days out); credit ≥ ``min_credit_ratio`` × width; India VIX ≤ ``max_vix``;
  no RBI/Budget day before expiry; one open spread per index, ``max_open`` in all.
* exits on daily closes: take profit when the spread can be bought back for
  ``take_profit_pct`` % of the credit or less; stop when it costs
  ``stop_multiple`` × the credit; otherwise close on the last trading day before
  expiry.
* 1R = the loss at the stop (credit × (stop_multiple − 1)); lots so 1R ≤
  ``risk_per_trade_pct`` of capital and the worst case (width − credit) ≤
  ``max_loss_pct``; 0 lots → not traded.

Live: ``run_eod()`` after the evening option files are stored — follows open
spreads, then builds today's candidates, sends shortlisted ones to the AI
(sharing the daily AI budget) and alerts Telegram on approval. Paper only:
prices are the day's closing prices; the real fill is next morning.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from typing import Callable

from backend.db import get_db
from backend.decision.config import get_config

STRATEGIES = ("bull_put", "bear_call", "iron_condor")


def settings() -> dict:
    cfg = get_config()
    base = {
        "enabled": False, "short_delta": [0.15, 0.20], "min_oi": 0, "min_credit_ratio": 0.15,
        "take_profit_pct": 50, "stop_multiple": 2.0, "max_vix": 25.0, "max_open": 3,
        "regime_strategy": {"STRONG_UP": "bull_put", "STRONG_DOWN": "bear_call", "SIDEWAYS": "iron_condor"},
        "symbols": {
            "NIFTY": {"wing": 200, "lot_size": 65, "weekdays": [2, 3, 4], "min_days": 4, "max_days": 6},
            "BANKNIFTY": {"wing": 500, "lot_size": 30, "weekdays": [0, 1, 2, 3, 4], "min_days": 10, "max_days": 20},
            "SENSEX": {"wing": 500, "lot_size": 20, "weekdays": [4, 0], "min_days": 3, "max_days": 6},
        },
        "regime_source": {"SENSEX": "NIFTY"}, "max_loss_pct": 3.0,
        "risk_per_trade_pct": cfg.global_.risk.risk_per_trade_pct, "capital": cfg.global_.capital,
    }
    try:
        inst = cfg.instrument("option_spreads")
    except KeyError:
        return base
    extra = dict(inst.model_extra or {})
    if "symbols" in extra:
        extra["symbols"] = {k: {**base["symbols"].get(k, {}), **v} for k, v in extra["symbols"].items()}
    return {**base, **extra, "enabled": inst.enabled}


# --- pure construction -------------------------------------------------------------------

def pick_expiry(day: date, expiries: list[str], rule: dict) -> str | None:
    if day.weekday() not in rule["weekdays"]:
        return None
    ok = [e for e in sorted(expiries) if rule["min_days"] <= (date.fromisoformat(e) - day).days <= rule["max_days"]]
    return ok[0] if ok else None


def _price(row: dict) -> float | None:
    p = row.get("close") or row.get("settle")
    return float(p) if p and p > 0 else None


def pick_short(chain: list[dict], opt_type: str, band: list[float], min_oi: float) -> dict | None:
    lo, hi = band
    mid = (lo + hi) / 2
    rows = [r for r in chain if r["opt_type"] == opt_type and r.get("delta") is not None and _price(r)
            and lo <= abs(r["delta"]) <= hi and (r.get("oi") or 0) >= min_oi]
    return min(rows, key=lambda r: abs(abs(r["delta"]) - mid)) if rows else None


def build(chain: list[dict], strategy: str, rule: dict, cfg: dict) -> tuple[dict | None, str]:
    """Legs, credit and width for one strategy from one day's chain of one expiry."""
    by_key = {(r["strike"], r["opt_type"]): r for r in chain}
    sides = {"bull_put": ["PE"], "bear_call": ["CE"], "iron_condor": ["PE", "CE"]}[strategy]
    legs, credit = [], 0.0
    for t in sides:
        short = pick_short(chain, t, cfg["short_delta"], cfg["min_oi"])
        if not short:
            return None, f"no {t} with delta {cfg['short_delta'][0]}–{cfg['short_delta'][1]}"
        far = short["strike"] - rule["wing"] if t == "PE" else short["strike"] + rule["wing"]
        long = by_key.get((far, t))
        if not long or not _price(long):
            return None, f"no priced {t} {far:g} for the wing"
        c = _price(short) - _price(long)
        if c <= 0:
            return None, f"{t} side has no credit"
        credit += c
        legs += [{"type": t, "strike": short["strike"], "side": "short", "price": _price(short),
                  "delta": round(short["delta"], 3), "iv": short.get("iv")},
                 {"type": t, "strike": far, "side": "long", "price": _price(long)}]
    width = float(rule["wing"])
    if credit < cfg["min_credit_ratio"] * width:
        return None, f"credit {credit:.1f} < {cfg['min_credit_ratio']:g} × width {width:g}"
    # an iron condor can only lose on one side at expiry: worst case is one width
    return {"strategy": strategy, "legs": legs, "credit": round(credit, 2), "width": width,
            "max_loss_points": round(width - credit, 2)}, ""


def value(legs: list[dict], prices: dict) -> float | None:
    """Cost to buy the spread back: short legs minus long legs, at ``prices[(strike, type)]``."""
    total = 0.0
    for leg in legs:
        p = prices.get((leg["strike"], leg["type"]))
        if p is None:
            return None
        total += p if leg["side"] == "short" else -p
    return max(0.0, total)


def size(credit: float, width: float, lot_size: int, cfg: dict) -> tuple[int, str]:
    stop_loss = credit * (cfg["stop_multiple"] - 1) * lot_size
    worst = (width - credit) * lot_size
    budget = cfg["capital"] * cfg["risk_per_trade_pct"] / 100
    cap = cfg["capital"] * cfg["max_loss_pct"] / 100
    lots = min(int(budget // stop_loss) if stop_loss > 0 else 0, int(cap // worst) if worst > 0 else 0)
    if lots < 1:
        return 0, f"1 lot: stop loss ₹{stop_loss:,.0f} / worst case ₹{worst:,.0f} above the limits"
    return lots, ""


def exit_check(sp: dict, val: float | None, day: str, last_day: str, cfg: dict) -> str | None:
    if val is None:
        return None
    if val <= sp["credit"] * (1 - cfg["take_profit_pct"] / 100):
        return "take_profit"
    if val >= sp["credit"] * cfg["stop_multiple"]:
        return "stop"
    if day >= last_day:
        return "expiry_exit"
    return None


def pnl(sp: dict, exit_value: float, cfg: dict) -> dict:
    points = sp["credit"] - exit_value
    one_r = sp["credit"] * (cfg["stop_multiple"] - 1)
    return {"pnl_points": round(points, 2), "pnl_r": round(points / one_r, 3) if one_r else 0.0,
            "pnl_inr": round(points * sp["lot_size"] * sp["lots"], 2)}


def last_trading_day_before(expiry: str) -> str:
    from tradingagents.utils.market_calendar import is_trading_day
    d = date.fromisoformat(expiry) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d.isoformat()


# --- context: regime, events, VIX -----------------------------------------------------------

def _regime_on(symbol: str, day: str, cfg: dict, cache: dict) -> str:
    from backend.marketdata.candles import get_candles
    from backend.marketdata.instruments import spot_instrument
    from backend.signals.regime import current_regime

    src = cfg["regime_source"].get(symbol, symbol)
    if src not in cache:
        inst = spot_instrument(src)
        cache[src] = get_candles(inst["instrument_token"], "day", limit=5000) if inst else []
    bars = [b for b in cache[src] if b["ts"][:10] <= day]
    return current_regime(bars[-300:])["regime"] if bars else "UNSTABLE"


def _blocked(day: date, expiry: str) -> str | None:
    from backend.signals import events
    m = events.market_events_between(day + timedelta(days=1), date.fromisoformat(expiry), kinds=("RBI", "BUDGET"))
    if not m["verified"]:
        return "event calendar not verified to expiry"
    if m["events"]:
        return "event before expiry: " + ", ".join(f"{e['event']} {e['date']}" for e in m["events"])
    return None


def candidate(symbol: str, day: date, cfg: dict, chain_for: Callable[[str, str], list[dict]],
              expiries_for: Callable[[str], list[str]], regime: str, vix: float | None) -> dict:
    """One index on one day: a buildable spread, or why not."""
    rule = cfg["symbols"][symbol]
    base = {"symbol": symbol, "signal_date": day.isoformat(), "regime": regime, "vix": vix}
    expiry = pick_expiry(day, expiries_for(day.isoformat()), rule)
    if not expiry:
        return {**base, "ok": False, "reason": "outside the entry window"}
    strategy = cfg["regime_strategy"].get(regime)
    if not strategy:
        return {**base, "ok": False, "reason": f"no spread in a {regime} regime", "expiry": expiry}
    if vix is not None and vix > cfg["max_vix"]:
        return {**base, "ok": False, "reason": f"India VIX {vix:.1f} > {cfg['max_vix']:g}", "expiry": expiry}
    blocked = _blocked(day, expiry)
    if blocked:
        return {**base, "ok": False, "reason": blocked, "expiry": expiry}
    built, why = build(chain_for(day.isoformat(), expiry), strategy, rule, cfg)
    if not built:
        return {**base, "ok": False, "reason": why, "expiry": expiry, "strategy": strategy}
    lots, why = size(built["credit"], built["width"], rule["lot_size"], cfg)
    if not lots:
        return {**base, "ok": False, "reason": why, "expiry": expiry, **built}
    return {**base, "ok": True, "reason": "", "expiry": expiry, "lot_size": rule["lot_size"], "lots": lots,
            "exit_by": last_trading_day_before(expiry), **built}


def _prices(chain: list[dict]) -> dict:
    return {(r["strike"], r["opt_type"]): _price(r) for r in chain if _price(r) is not None}


# --- backtest -----------------------------------------------------------------------------------

def _stats(rows: list[dict]) -> dict:
    rs = [r["pnl_r"] for r in rows]
    if not rs:
        return {"trades": 0}
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    mean = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs), 1), "avg_r": round(mean, 3),
            "total_r": round(sum(rs), 2), "max_drawdown_r": round(dd, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "sharpe_trade": round(mean / sd, 3) if sd else None,
            "total_inr_per_lot": round(sum(r["pnl_points"] * r["lot_size"] for r in rows), 0)}


def backtest(start: date, end: date, symbols: list[str] | None = None, split_date: date | None = None,
             cfg: dict | None = None) -> dict:
    """Replay the rules over stored EOD chains, one spread per index at a time."""
    from backend.options import store
    from tradingagents.utils.market_calendar import is_trading_day

    cfg = cfg or settings()
    symbols = symbols or list(cfg["symbols"])
    cache: dict = {}
    trades, skipped = [], {}
    for symbol in symbols:
        open_sp = None
        d = start
        while d <= end:
            iso = d.isoformat()
            if not is_trading_day(d):
                d += timedelta(days=1)
                continue
            exps = store.expiries(symbol, iso)
            if not exps:
                d += timedelta(days=1)
                continue
            if open_sp:
                val = value(open_sp["legs"], _prices(store.chain(symbol, iso, open_sp["expiry"])))
                reason = exit_check(open_sp, val, iso, open_sp["exit_by"], cfg)
                if reason:
                    trades.append({**open_sp, "exit_date": iso, "exit_value": round(val, 2), "exit_reason": reason,
                                   **pnl(open_sp, val, cfg)})
                    open_sp = None
            if not open_sp:
                c = candidate(symbol, d, cfg, lambda day, e: store.chain(symbol, day, e), lambda day: exps,
                              _regime_on(symbol, iso, cfg, cache), store.vix_on(iso))
                if c["ok"]:
                    open_sp = {k: c[k] for k in ("symbol", "signal_date", "expiry", "strategy", "legs", "credit",
                                                 "width", "lot_size", "lots", "regime", "vix", "exit_by")}
                elif c["reason"] != "outside the entry window":
                    key = c["reason"].split(" (")[0].split(":")[0]
                    key = "".join(ch for ch in key if not ch.isdigit()).strip()
                    skipped[key] = skipped.get(key, 0) + 1
            d += timedelta(days=1)
        if open_sp:
            skipped["still open at the end"] = skipped.get("still open at the end", 0) + 1
    out = {"status": "ok", "trades": trades, "skipped": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
           "all": _stats(trades),
           "by_symbol": {s: _stats([t for t in trades if t["symbol"] == s]) for s in symbols},
           "by_strategy": {s: _stats([t for t in trades if t["strategy"] == s]) for s in STRATEGIES},
           "by_exit": {k: _stats([t for t in trades if t["exit_reason"] == k])
                       for k in ("take_profit", "stop", "expiry_exit")},
           "by_year": {y: _stats([t for t in trades if t["signal_date"][:4] == y])
                       for y in sorted({t["signal_date"][:4] for t in trades})},
           "limitations": ["Exits are checked on daily closes only: a stop can be passed during the day.",
                           "Fills at the day's closing prices; no bid-ask spread, costs or slippage.",
                           "SENSEX uses the NIFTY regime.",
                           "Only days with stored option files are replayed."]}
    if split_date:
        cut = split_date.isoformat()
        out["by_period"] = {"train": _stats([t for t in trades if t["signal_date"] < cut]),
                            "test": _stats([t for t in trades if t["signal_date"] >= cut])}
    return out


# --- live book ------------------------------------------------------------------------------------

def ensure_table() -> None:
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS option_spreads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, signal_date TEXT NOT NULL,
            expiry TEXT NOT NULL, strategy TEXT NOT NULL, legs TEXT NOT NULL, credit REAL, width REAL,
            lot_size INTEGER, lots INTEGER, regime TEXT, vix REAL, exit_by TEXT,
            decision TEXT NOT NULL DEFAULT 'shortlisted', panel_decision TEXT, panel TEXT,
            status TEXT NOT NULL DEFAULT 'open', last_value REAL, exit_date TEXT, exit_value REAL,
            exit_reason TEXT, pnl_points REAL, pnl_r REAL, pnl_inr REAL, alert TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE (symbol, signal_date, expiry, strategy))""")


def evaluations_today(today: date) -> int:
    ensure_table()
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM option_spreads WHERE panel_decision IN ('approve', 'reject', 'error') "
            "AND substr(json_extract(panel, '$.evaluated_at'), 1, 10) = ?", (today.isoformat(),)).fetchone()[0]


def spreads(status: str | None = None) -> list[dict]:
    ensure_table()
    q, args = "SELECT * FROM option_spreads", ()
    if status:
        q, args = q + " WHERE status = ?", (status,)
    with get_db() as conn:
        rows = conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["legs"] = json.loads(item["legs"])
        item["panel"] = json.loads(item["panel"]) if item["panel"] else None
        out.append(item)
    return out


def _legs_text(sp: dict) -> str:
    return " / ".join(f"{'Sell' if l['side'] == 'short' else 'Buy'} {l['strike']:g} {l['type']} @ {l['price']:.1f}"
                      for l in sp["legs"])


def build_prompt(sp: dict) -> str:
    plan = {k: sp[k] for k in ("symbol", "strategy", "expiry", "credit", "width", "lots", "lot_size", "regime", "vix",
                               "exit_by")}
    plan["legs"] = sp["legs"]
    cfg = settings()
    return (
        "You review one index option credit spread for a paper-trading book (advisory only, the user "
        "places orders by hand). The rules already chose it; you can only approve or reject, not change it.\n"
        f"Exits: take profit at {cfg['take_profit_pct']}% of the credit, stop when the spread costs "
        f"{cfg['stop_multiple']:g}× the credit, otherwise close on {sp['exit_by']} (day before expiry).\n"
        f"Plan (closing prices of {sp['signal_date']}):\n{json.dumps(plan, indent=1)}\n"
        'Answer with JSON only: {"decision": "approve" | "reject", "confidence": 0-100, '
        '"reasons": [up to 3 short strings], "risks": [up to 2 short strings]}')


def build_alert(sp: dict, panel: dict | None, kind: str) -> str:
    name = {"bull_put": "Bull put spread", "bear_call": "Bear call spread", "iron_condor": "Iron condor"}[sp["strategy"]]
    head = "✅ AI-approved" if kind == "approved" else "⚠️ Rules only (" + kind + ")"
    lines = [f"{head} — {sp['symbol']} {name} (paper)", f"Expiry {sp['expiry']} · {_legs_text(sp)}",
             f"Credit {sp['credit']:.1f} pts · width {sp['width']:g} · {sp['lots']} lot(s) × {sp['lot_size']}",
             f"Take profit ≤ {sp['credit'] * 0.5:.1f} · stop ≥ {sp['credit'] * settings()['stop_multiple']:.1f} · "
             f"exit by {sp['exit_by']}", f"Regime {sp['regime']} · India VIX {sp['vix'] or '—'}",
             "Prices are today's closes; check them at the open."]
    if panel and panel.get("reasons"):
        lines.append("AI: " + "; ".join(panel["reasons"]))
    if panel and panel.get("risks"):
        lines.append("Risks: " + "; ".join(panel["risks"]))
    return "\n".join(lines)


def _send(text: str) -> str:
    try:
        from backend.notifications.telegram import send_message
        send_message(text)
        return "sent"
    except Exception as exc:
        return f"not sent: {type(exc).__name__}"


def run_eod(day: date | None = None, llm_call: Callable[[str], tuple[str, str]] | None = None) -> dict:
    """After the day's option files are stored: follow open spreads, then today's candidates."""
    from backend.journal import panel as journal_panel
    from backend.options import store
    from backend.system_state import is_paused

    ensure_table()
    cfg = settings()
    day = day or date.today()
    iso = day.isoformat()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "option spreads disabled in config"}
    if is_paused():
        return {"status": "skipped", "reason": "paused"}
    closed = []
    for sp in spreads("open"):
        chain = store.chain(sp["symbol"], iso, sp["expiry"])
        if not chain:
            continue
        val = value(sp["legs"], _prices(chain))
        reason = exit_check(sp, val, iso, sp["exit_by"], cfg)
        with get_db() as conn:
            if reason:
                p = pnl(sp, val, cfg)
                conn.execute("UPDATE option_spreads SET status='closed', last_value=?, exit_date=?, exit_value=?, "
                             "exit_reason=?, pnl_points=?, pnl_r=?, pnl_inr=? WHERE id=?",
                             (val, iso, round(val, 2), reason, p["pnl_points"], p["pnl_r"], p["pnl_inr"], sp["id"]))
                if sp["decision"] == "approved":
                    _send(f"🔚 {sp['symbol']} {sp['strategy'].replace('_', ' ')} — close ({reason.replace('_', ' ')}) "
                          f"at {val:.1f} pts · {p['pnl_r']:+.2f}R · ₹{p['pnl_inr']:,.0f} (paper)")
                closed.append(sp["symbol"])
            elif val is not None:
                conn.execute("UPDATE option_spreads SET last_value=? WHERE id=?", (round(val, 2), sp["id"]))
    open_now = spreads("open")
    held = {s["symbol"] for s in open_now if s["decision"] in ("approved", "shortlisted")}
    cache: dict = {}
    results, new = {}, []
    for symbol in cfg["symbols"]:
        if symbol in held:
            results[symbol] = "spread already open"
            continue
        if len([s for s in open_now if s["decision"] == "approved"]) >= cfg["max_open"]:
            results[symbol] = f"{cfg['max_open']} spreads already open"
            continue
        exps = store.expiries(symbol, iso)
        if not exps:
            results[symbol] = "no option file for today"
            continue
        c = candidate(symbol, day, cfg, lambda d, e: store.chain(symbol, d, e), lambda d: exps,
                      _regime_on(symbol, iso, cfg, cache), store.vix_on(iso))
        if not c["ok"]:
            results[symbol] = c["reason"]
            continue
        with get_db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO option_spreads (symbol, signal_date, expiry, strategy, legs, credit, width, "
                "lot_size, lots, regime, vix, exit_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, iso, c["expiry"], c["strategy"], json.dumps(c["legs"]), c["credit"], c["width"],
                 c["lot_size"], c["lots"], c["regime"], c["vix"], c["exit_by"]))
            if not cur.rowcount:
                results[symbol] = "already recorded today"
                continue
            c["id"] = cur.lastrowid
        new.append(c)
        results[symbol] = f"{c['strategy']} shortlisted"
    if llm_call is None and os.environ.get("TRADINGAGENTS_AI_PANEL", "").lower() == "off":
        reviews = [{"symbol": c["symbol"], "decision": "shortlisted", "reason": "AI panel off (env)"} for c in new]
    else:
        reviews = [_review(c, day, llm_call or journal_panel._default_llm_call) for c in new]
    return {"status": "ok", "date": iso, "closed": closed, "results": results, "new": [c["symbol"] for c in new],
            "reviews": reviews}


def _review(sp: dict, day: date, llm_call) -> dict:
    from backend.journal import panel as journal_panel

    g = get_config().global_
    used = journal_panel.evaluations_today(day)          # journal + spreads share one daily budget
    if not g.ai_panel.enabled or used >= g.limits.max_ai_evaluations_day:
        why = "AI panel off" if not g.ai_panel.enabled else f"daily AI limit reached ({g.limits.max_ai_evaluations_day})"
        alert = _send(build_alert(sp, None, "not reviewed")) if g.ai_panel.notify_unreviewed else "off"
        _update(sp["id"], "not_sent", {"evaluated_at": None, "skipped": why}, None, alert)
        return {"symbol": sp["symbol"], "decision": "not_sent", "reason": why}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        text, model = llm_call(build_prompt(sp))
        verdict = journal_panel.parse_reply(text)
    except Exception as exc:
        _update(sp["id"], "error", {"evaluated_at": now, "error": f"{type(exc).__name__}: {exc}"[:200]}, None,
                _send(build_alert(sp, None, "AI review failed")))
        return {"symbol": sp["symbol"], "decision": "error"}
    panel = {**verdict, "evaluated_at": now, "model": model}
    approved = verdict["decision"] == "approve" and verdict["confidence"] >= g.ai_panel.min_confidence
    alert = _send(build_alert(sp, panel, "approved")) if approved and g.ai_panel.notify_telegram else None
    _update(sp["id"], verdict["decision"], panel, "approved" if approved else "rejected_panel", alert)
    return {"symbol": sp["symbol"], "decision": "approved" if approved else "rejected_panel",
            "confidence": verdict["confidence"]}


def _update(sid: int, panel_decision: str, panel: dict, decision: str | None, alert: str | None) -> None:
    with get_db() as conn:
        conn.execute("UPDATE option_spreads SET panel_decision=?, panel=?, decision=COALESCE(?, decision), alert=? "
                     "WHERE id=?", (panel_decision, json.dumps(panel), decision, alert, sid))


def record() -> dict:
    rows = spreads("closed")
    return {"paper": _stats([r for r in rows if r["decision"] == "approved"]), "all": _stats(rows)}
