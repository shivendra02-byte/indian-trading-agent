"""Shortlist selection shared by every scan: one open trade per instrument.

A position "book" is (journal segment, symbol): stock futures from the hourly scan
and from ORB share the ``stock_futures`` book, cash swing and cup & handle each
have their own. A symbol is busy while a shortlisted or approved record of that
book is still pending (entry window) or open. Busy symbols are not shortlisted:
the candidate stays valid, is journaled as ``not_selected`` with the reason, and is
shadow-tracked like any other. Within one scan a symbol is shortlisted once.
Switch: ``global.limits.one_open_trade_per_instrument``.
"""

from __future__ import annotations

from typing import Callable, Iterable

from backend.decision.config import get_config
from backend.journal.store import ensure_journal_table
from backend.signals.setups import stage
from backend.db import get_db

POSITION_DECISIONS = ("shortlisted", "approved")
LIVE_STATUSES = ("pending", "open")


def open_positions(segments: Iterable[str], live_only: bool = False) -> set[tuple[str, str]]:
    """(segment, symbol) pairs with a shortlisted/approved record still pending or open.
    ``live_only`` (25 Nov 2026): ignore records of shadow strategies/setups (e.g. ORB, the NSE
    trend pullback) — they are only tracked and must not block a live setup on the same symbol."""
    segments = tuple(segments)
    if not segments:
        return set()
    ensure_journal_table()
    q = (f"SELECT DISTINCT segment, symbol, setup FROM trade_journal "
         f"WHERE segment IN ({', '.join('?' * len(segments))}) "
         f"AND decision IN ({', '.join('?' * len(POSITION_DECISIONS))}) "
         f"AND track_status IN ({', '.join('?' * len(LIVE_STATUSES))})")
    with get_db() as conn:
        rows = conn.execute(q, (*segments, *POSITION_DECISIONS, *LIVE_STATUSES)).fetchall()
    g = get_config().global_
    return {(r[0], r[1]) for r in rows if not (live_only and g.is_shadow(r[0], r[2]))}


def choose_shortlist(ranked: list[dict], limit: int | None, segment_of: Callable[[dict], str],
                     busy: set[tuple[str, str]] | None = None) -> list[dict]:
    """Mark ``shortlisted`` on the best ``limit`` rows of ``ranked`` (already sorted
    best first; None = no limit), skipping symbols already in a trade and repeats of
    a symbol within this scan. Skipped rows get a ``selection`` stage and a reason."""
    enabled = get_config().global_.limits.one_open_trade_per_instrument
    if busy is None:
        busy = open_positions({segment_of(r) for r in ranked}) if enabled else set()
    chosen: list[dict] = []
    picked: set[tuple[str, str]] = set()
    for r in ranked:
        key = (segment_of(r), r["symbol"])
        if limit is not None and len(chosen) >= limit:
            break
        if enabled and key in busy:
            _skip(r, f"already in a trade on {r['symbol']}")
            continue
        if enabled and key in picked:
            _skip(r, f"another setup on {r['symbol']} ranked higher in this scan")
            continue
        r["shortlisted"] = True
        picked.add(key)
        chosen.append(r)
    return chosen


def _skip(r: dict, reason: str) -> None:
    r["shortlisted"] = False
    r["selection_skip"] = reason
    r["reject_reason"] = reason
    r.setdefault("stages", []).append(stage("selection", "fail", reason))
