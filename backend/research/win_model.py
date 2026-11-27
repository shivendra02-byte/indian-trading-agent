"""Win-probability model on past signals (Research Lab) — the "Identifying Trades" paper's idea
(VJTI, NIFTY 500): let the rules find candidates, then train a classifier to say which ones tend
to win, and check it honestly.

Data: closed trade-journal records (every decision: approved, rejected, shadow, below score) or
the trades of one stored rule backtest. Label: R > 0. Inputs are only things known when the
signal fired: setup, direction, regime, score, R:R, volume ratio, stop distance (% of price),
hour and weekday, segment, and how many soft checks failed.

Walk-forward by month: the model for month m is trained only on months before m (at least
``min_train`` rows), so every prediction is out of sample. Reported: AUC (0.5 = no skill),
Brier score vs always predicting the base rate, a calibration table, average R by predicted
probability third vs by the rule score third, and — for the journal — how the AI panel's
approvals and rejections compare. Nothing here changes a rule.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from backend.db import get_db

CATEGORICAL = ("setup", "direction", "regime", "segment")
NUMERIC = ("score", "rr", "vol_ratio", "stop_pct", "hour", "weekday", "soft_failed")


def _features(r: dict) -> dict:
    ts = str(r.get("signal_ts") or "")
    try:
        dt = datetime.fromisoformat(ts[:19])
        hour, wd = dt.hour + dt.minute / 60, dt.weekday()
    except ValueError:
        hour, wd = None, None
    entry, stop = r.get("entry"), r.get("stop")
    soft = r.get("soft_checks") or {}
    if isinstance(soft, str):
        try:
            soft = json.loads(soft)
        except ValueError:
            soft = {}
    failed = sum(1 for v in soft.values() if v is False or (isinstance(v, dict) and v.get("passed") is False)) \
        if isinstance(soft, dict) else None
    return {"setup": r.get("setup"), "direction": r.get("direction"), "regime": r.get("regime") or "?",
            "segment": r.get("segment") or "?", "score": r.get("score"), "rr": r.get("rr"),
            "vol_ratio": r.get("vol_ratio"), "stop_pct": abs(entry - stop) / entry * 100 if entry and stop else None,
            "hour": hour, "weekday": wd, "soft_failed": failed}


def journal_rows() -> list[dict]:
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT segment, setup, direction, regime, score, rr, entry, stop, signal_ts, soft_checks, extra, decision, "
            "panel_decision, pnl_r FROM trade_journal WHERE track_status = 'closed' AND pnl_r IS NOT NULL")]
    for r in rows:
        try:
            r["vol_ratio"] = (json.loads(r.get("extra") or "{}") or {}).get("vol_ratio")
        except ValueError:
            r["vol_ratio"] = None
    return rows


def backtest_rows(run_id: int) -> list[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT segment, trades FROM rule_backtests WHERE id = ? AND status = 'done'", (run_id,)).fetchone()
    if not r or not r["trades"]:
        return []
    trades = json.loads(r["trades"])
    return [{**t, "segment": r["segment"]} for t in trades if t.get("pnl_r") is not None]


def sources() -> dict:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM trade_journal WHERE track_status = 'closed' AND pnl_r IS NOT NULL"
                         ).fetchone()[0] if _has_table(conn, "trade_journal") else 0
        runs = [dict(x) for x in conn.execute(
            "SELECT id, segment, rule_set, start_date, end_date, created_at FROM rule_backtests WHERE status = 'done' "
            "ORDER BY id DESC LIMIT 30")] if _has_table(conn, "rule_backtests") else []
    return {"journal_closed": n, "backtests": runs}


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _matrix(rows: list[dict], columns: list[str] | None = None):
    import numpy as np
    feats = [_features(r) for r in rows]
    if columns is None:
        cats = {c: sorted({str(f[c]) for f in feats}) for c in CATEGORICAL}
        columns = list(NUMERIC) + [f"{c}={v}" for c, vals in cats.items() for v in vals]
    X = np.full((len(feats), len(columns)), np.nan)
    col = {c: k for k, c in enumerate(columns)}
    for i, f in enumerate(feats):
        for c in NUMERIC:
            if f[c] is not None:
                X[i, col[c]] = float(f[c])
        for c in CATEGORICAL:
            k = col.get(f"{c}={f[c]}")
            if k is not None:
                X[i, k] = 1.0
    X[:, len(NUMERIC):] = np.nan_to_num(X[:, len(NUMERIC):])
    return X, columns


def _thirds(pairs: list[tuple[float, float]]) -> dict:
    """Average R and win rate by thirds of a ranking value (low / mid / high)."""
    pairs = sorted(pairs)
    n = len(pairs)
    out = {}
    for name, lo, hi in (("low", 0, n // 3), ("mid", n // 3, 2 * n // 3), ("high", 2 * n // 3, n)):
        part = [r for _, r in pairs[lo:hi]]
        if part:
            out[name] = {"n": len(part), "avg_r": round(sum(part) / len(part), 3),
                         "win_rate": round(100 * sum(1 for r in part if r > 0) / len(part), 1)}
    return out


def run(source: str = "journal", run_id: int | None = None, min_train: int = 300) -> dict:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import brier_score_loss, roc_auc_score

    rows = backtest_rows(run_id) if source == "backtest" and run_id else journal_rows()
    rows = [r for r in rows if r.get("signal_ts")]
    rows.sort(key=lambda r: str(r["signal_ts"]))
    if len(rows) < min_train + 50:
        return {"status": "too few", "rows": len(rows),
                "reason": f"need at least {min_train + 50} closed signals; have {len(rows)}"}
    months = sorted({str(r["signal_ts"])[:7] for r in rows})
    X_all, columns = _matrix(rows)
    y_all = np.array([1 if r["pnl_r"] > 0 else 0 for r in rows])
    month_of = np.array([str(r["signal_ts"])[:7] for r in rows])
    preds = np.full(len(rows), np.nan)
    fitted = 0
    for m in months:
        train = month_of < m
        test = month_of == m
        if train.sum() < min_train or test.sum() == 0 or len(set(y_all[train])) < 2:
            continue
        model = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200,
                                               l2_regularization=1.0, random_state=0)
        model.fit(X_all[train], y_all[train])
        preds[test] = model.predict_proba(X_all[test])[:, 1]
        fitted += 1
    mask = ~np.isnan(preds)
    if mask.sum() < 50:
        return {"status": "too few", "rows": len(rows), "reason": "fewer than 50 out-of-sample predictions"}
    y, p = y_all[mask], preds[mask]
    rs = [rows[i]["pnl_r"] for i in np.where(mask)[0]]
    base = float(y.mean())
    out = {
        "status": "ok", "source": source, "run_id": run_id, "rows": len(rows),
        "out_of_sample": int(mask.sum()), "months_tested": fitted,
        "first_test_month": str(month_of[mask][0]), "base_win_rate": round(100 * base, 1),
        "auc": round(float(roc_auc_score(y, p)), 3) if len(set(y)) > 1 else None,
        "brier": round(float(brier_score_loss(y, p)), 4),
        "brier_base_rate": round(float(brier_score_loss(y, np.full(len(y), base))), 4),
        "by_model_third": _thirds(list(zip(p.tolist(), rs))),
        "by_score_third": _thirds([(float(rows[i].get("score") or 0), rows[i]["pnl_r"]) for i in np.where(mask)[0]]),
        "calibration": [],
        "columns": len(columns),
    }
    for lo in (0.0, 0.3, 0.4, 0.5, 0.6, 0.7):
        hi = 1.01 if lo == 0.7 else lo + 0.1 if lo else 0.3
        sel = (p >= lo) & (p < hi)
        if sel.sum():
            out["calibration"].append({"band": f"{int(lo * 100)}-{int(min(hi, 1) * 100)}%", "n": int(sel.sum()),
                                       "predicted": round(100 * float(p[sel].mean()), 1),
                                       "actual": round(100 * float(y[sel].mean()), 1)})
    if source == "journal":
        groups = defaultdict(list)
        for k, i in enumerate(np.where(mask)[0]):
            r = rows[i]
            label = ("AI approved" if r.get("decision") == "approved" else
                     "AI rejected" if r.get("decision") == "rejected_panel" else None)
            if label:
                groups[label].append((float(p[k]), r["pnl_r"]))
        out["ai_panel"] = {g: {"n": len(v), "avg_r": round(sum(r for _, r in v) / len(v), 3),
                               "model_prob": round(100 * sum(q for q, _ in v) / len(v), 1)} for g, v in groups.items()}
    out["verdict"] = ("no skill" if out["auc"] is None or out["auc"] < 0.55 else
                      "weak skill" if out["auc"] < 0.6 else "useful skill — confirm on later months")
    return out
