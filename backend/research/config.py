"""Settings for the research strategies: config/research.yaml (re-read when it changes).

Kept apart from config/decision_engine.yaml on purpose: research strategies are shadow
only and must never change the live rule version stored with every live record.
``TRADINGAGENTS_RESEARCH_CONFIG`` points at another file (tests).
"""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "research.yaml"
_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "data": None}
_override: dict | None = None


def _path() -> Path:
    return Path(os.getenv("TRADINGAGENTS_RESEARCH_CONFIG") or DEFAULT_PATH)


def load() -> dict:
    """The whole research config (a copy, safe to modify)."""
    if _override is not None:
        return copy.deepcopy(_override)
    path = _path()
    mtime = path.stat().st_mtime if path.exists() else None
    with _lock:
        if _cache["path"] != path or _cache["mtime"] != mtime:
            data = yaml.safe_load(path.read_text()) if path.exists() else {}
            _validate(data or {})
            _cache.update(path=path, mtime=mtime, data=data or {})
        return copy.deepcopy(_cache["data"])


def _validate(data: dict) -> None:
    if float(data.get("risk_pct", 1.0)) <= 0 or float(data.get("capital", 1)) <= 0:
        raise ValueError("research.yaml: capital and risk_pct must be positive")
    for key, s in (data.get("strategies") or {}).items():
        if not isinstance(s, dict):
            raise ValueError(f"research.yaml: strategy {key} must be a mapping")
        if key == "trend_ewma" and int(s.get("fast", 1)) >= int(s.get("slow", 2)):
            raise ValueError("research.yaml: trend_ewma fast span must be shorter than slow")


def set_override(data: dict | None) -> None:
    """Tests: use ``data`` instead of the file (None = back to the file)."""
    global _override
    _override = copy.deepcopy(data) if data is not None else None


def strategy(key: str) -> dict:
    s = (load().get("strategies") or {}).get(key)
    if s is None:
        raise KeyError(f"unknown research strategy {key}")
    return s


def capital() -> float:
    return float(load().get("capital", 1_000_000))


def one_r_rupees() -> float:
    cfg = load()
    return float(cfg.get("capital", 1_000_000)) * float(cfg.get("risk_pct", 1.0)) / 100


def cost_bps(kind: str) -> float:
    return float((load().get("cost_bps") or {}).get(kind, 0.0))


def live_start() -> str:
    return str(load().get("live_start") or "2026-11-27")
