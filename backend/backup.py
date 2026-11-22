"""Weekly backups of the app's own data (journal, settings, history).

Two kinds, written as zip files to ``<TRADINGAGENTS_HOME>/backups`` (0700):

- ``essentials``: the database without the tables that can be downloaded
  again (candles, option prices, instrument master), plus the agent memory
  files. API keys, Kite/Telegram credentials and other secrets are blanked,
  so this copy is also sent to your Telegram chat as the off-server copy.
- ``full``: the whole database (secrets included) plus agent memories. Stays
  on the server; download it from Settings to keep a complete copy.

The database is copied with SQLite's online backup API, so the app keeps
running. Restore by stopping the backend, putting ``trading_agent.db`` from
the zip back in ``~/.tradingagents/`` and starting it again.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta

from backend.db import get_db_path

KINDS = ("essentials", "full")
KEEP = {"essentials": 8, "full": 2}          # files kept per kind (8 weeks / 2 weeks)
REBUILDABLE_TABLES = ("candles", "option_eod", "instruments")
SECRET_KEY = re.compile(r"(api_key|secret|token|password|chat_id|oauth_state|kite_profile)", re.I)
TELEGRAM_MAX_BYTES = 45_000_000              # bot uploads are limited to 50 MB
NAME_PATTERN = re.compile(r"^ita-(essentials|full)-\d{8}-\d{6}\.zip$")
DUE_WEEKDAY, DUE_HOUR = 6, 2                 # Sunday from 02:00 IST

_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("TRADINGAGENTS_BACKUP", "on").lower() != "off"


def home_dir() -> str:
    return os.path.dirname(get_db_path())


def backup_dir() -> str:
    path = os.environ.get("TRADINGAGENTS_BACKUP_DIR") or os.path.join(home_dir(), "backups")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def list_backups() -> list[dict]:
    folder = backup_dir()
    rows = []
    for name in os.listdir(folder):
        if not NAME_PATTERN.match(name):
            continue
        path = os.path.join(folder, name)
        stamp = datetime.strptime(name.rsplit("-", 2)[-2] + name.rsplit("-", 1)[-1][:6], "%Y%m%d%H%M%S")
        rows.append({"name": name, "kind": name.split("-")[1], "created_at": stamp.isoformat(timespec="seconds"),
                     "bytes": os.path.getsize(path)})
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


def backup_path(name: str) -> str | None:
    """Absolute path of a stored backup, or None for any other name."""
    if not NAME_PATTERN.match(name or ""):
        return None
    path = os.path.join(backup_dir(), name)
    return path if os.path.isfile(path) else None


def last_backup_at(kind: str = "essentials") -> datetime | None:
    rows = [r for r in list_backups() if r["kind"] == kind]
    return datetime.fromisoformat(rows[0]["created_at"]) if rows else None


def backup_due(now: datetime, last: datetime | None) -> bool:
    """Pure: Sunday from 02:00 once a week; any day once the last one is 8+ days old."""
    if last is None:
        return True
    age = now - last
    if age >= timedelta(days=8):
        return True
    return now.weekday() == DUE_WEEKDAY and now.hour >= DUE_HOUR and age >= timedelta(days=6)


def _copy_database(target: str) -> None:
    source = sqlite3.connect(get_db_path())
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _strip_for_essentials(db_file: str) -> dict:
    conn = sqlite3.connect(db_file)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        dropped = [t for t in REBUILDABLE_TABLES if t in tables]
        for table in dropped:
            conn.execute(f'DROP TABLE "{table}"')
        blanked = 0
        if "settings" in tables:
            for (key,) in conn.execute("SELECT key FROM settings").fetchall():
                if SECRET_KEY.search(key or ""):
                    conn.execute("UPDATE settings SET value = NULL WHERE key = ?", (key,))
                    blanked += 1
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return {"dropped_tables": dropped, "secrets_blanked": blanked}


def _prune(kind: str) -> list[str]:
    removed = []
    for row in [r for r in list_backups() if r["kind"] == kind][KEEP[kind]:]:
        os.remove(os.path.join(backup_dir(), row["name"]))
        removed.append(row["name"])
    return removed


def make_backup(kind: str, now: datetime | None = None) -> dict:
    """Write one backup zip; returns its name, size and what was left out."""
    if kind not in KINDS:
        raise ValueError(f"unknown backup kind {kind}")
    now = now or datetime.now()
    name = f"ita-{kind}-{now:%Y%m%d-%H%M%S}.zip"
    target = os.path.join(backup_dir(), name)
    with tempfile.TemporaryDirectory(dir=backup_dir()) as tmp:
        db_copy = os.path.join(tmp, "trading_agent.db")
        _copy_database(db_copy)
        details = _strip_for_essentials(db_copy) if kind == "essentials" else {}
        partial = target + ".part"
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.write(db_copy, "trading_agent.db")
            memory = os.path.join(home_dir(), "memory")
            if os.path.isdir(memory):
                for file in sorted(os.listdir(memory)):
                    if file.endswith(".json"):
                        zf.write(os.path.join(memory, file), f"memory/{file}")
            zf.writestr("README.txt", _readme(kind, now, details))
        os.chmod(partial, 0o600)
        os.replace(partial, target)
    return {"name": name, "kind": kind, "bytes": os.path.getsize(target), **details, "removed": _prune(kind)}


def _readme(kind: str, now: datetime, details: dict) -> str:
    lines = [f"Trading agent {kind} backup, {now:%Y-%m-%d %H:%M} IST.", "",
             "Restore: stop the backend (sudo systemctl stop ita-backend), copy trading_agent.db",
             "to ~/.tradingagents/ (and memory/*.json to ~/.tradingagents/memory/), then start it again.", ""]
    if kind == "essentials":
        lines += ["Left out (download again from the app): " + (", ".join(details.get("dropped_tables") or []) or "none"),
                  "API keys and Kite/Telegram credentials are blank: enter them again in Settings."]
    else:
        lines += ["Complete copy including API keys and credentials: keep it private."]
    return "\n".join(lines) + "\n"


def send_to_telegram(path: str, caption: str) -> dict:
    """Upload a backup file to the configured Telegram chat (sendDocument)."""
    import json
    import urllib.request
    import uuid

    from backend.notifications import telegram

    size = os.path.getsize(path)
    if size > TELEGRAM_MAX_BYTES:
        return {"status": "skipped", "reason": f"file is {size // 1_000_000} MB, Telegram allows 50 MB"}
    try:
        token, chat_id = telegram._settings()
    except telegram.TelegramConfigError as exc:
        return {"status": "not_sent", "reason": str(exc)}
    boundary = uuid.uuid4().hex
    with open(path, "rb") as fh:
        payload = fh.read()
    parts = []
    for field, value in (("chat_id", chat_id), ("caption", caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                  f'filename="{os.path.basename(path)}"\r\nContent-Type: application/zip\r\n\r\n').encode())
    body = b"".join(parts) + payload + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            ok = json.loads(response.read().decode()).get("ok")
    except Exception:  # never expose the token-bearing URL
        return {"status": "error", "reason": "Telegram upload failed"}
    return {"status": "sent"} if ok else {"status": "error", "reason": "Telegram rejected the file"}


def run_weekly_backup(now: datetime | None = None, send=send_to_telegram) -> dict:
    """Both backups; the essentials copy also goes to Telegram."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        now = now or datetime.now()
        essentials = make_backup("essentials", now)
        full = make_backup("full", now)
        caption = (f"Trading agent weekly backup {now:%d %b %Y}. Journal, settings and history "
                   f"(credentials blank). {essentials['bytes'] / 1_000_000:.1f} MB.")
        telegram_result = send(os.path.join(backup_dir(), essentials["name"]), caption)
        return {"status": "ok", "essentials": essentials, "full": full, "telegram": telegram_result}
    finally:
        _lock.release()


def run_backup_now(kind: str, to_telegram: bool = False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        result = make_backup(kind)
        if to_telegram and kind == "essentials":
            result["telegram"] = send_to_telegram(os.path.join(backup_dir(), result["name"]),
                                                  "Trading agent backup (manual, credentials blank).")
        return {"status": "ok", **result}
    finally:
        _lock.release()
