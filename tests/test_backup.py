"""Weekly backups: contents, secret blanking, retention, schedule, API."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta

from tests._support import IsolatedStateTestCase, csrf_headers, fresh_test_client, login


class BackupTests(IsolatedStateTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from backend.db import ensure_db, get_db, set_setting
        ensure_db()
        set_setting("api_key_anthropic", "sk-secret")
        set_setting("telegram_bot_token", "123:abc")
        set_setting("kite_access_token", "tok")
        set_setting("llm_provider", "anthropic")
        with get_db() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS candles (token INTEGER, ts TEXT, close REAL)")
            conn.execute("INSERT INTO candles VALUES (1, '2026-11-20', 100.0)")
        from backend.db import get_db_path
        memory = os.path.join(os.path.dirname(get_db_path()), "memory")
        os.makedirs(memory, exist_ok=True)
        with open(os.path.join(memory, "bull_memory.json"), "w") as fh:
            fh.write("{}")

    def _db_from(self, path):
        tmp = tempfile.mkdtemp()
        with zipfile.ZipFile(path) as zf:
            zf.extract("trading_agent.db", tmp)
            names = zf.namelist()
        return sqlite3.connect(os.path.join(tmp, "trading_agent.db")), names

    def test_essentials_blanks_secrets_and_drops_rebuildable(self):
        from backend import backup
        result = backup.make_backup("essentials", datetime(2026, 11, 22, 2, 0))
        self.assertIn("candles", result["dropped_tables"])
        self.assertEqual(result["secrets_blanked"], 3)
        conn, names = self._db_from(os.path.join(backup.backup_dir(), result["name"]))
        settings = dict(conn.execute("SELECT key, value FROM settings").fetchall())
        self.assertIsNone(settings["api_key_anthropic"])
        self.assertIsNone(settings["telegram_bot_token"])
        self.assertEqual(settings["llm_provider"], "anthropic")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("candles", tables)
        self.assertIn("memory/bull_memory.json", names)
        self.assertIn("README.txt", names)

    def test_full_keeps_everything(self):
        from backend import backup
        result = backup.make_backup("full", datetime(2026, 11, 22, 2, 0))
        conn, _ = self._db_from(os.path.join(backup.backup_dir(), result["name"]))
        self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='api_key_anthropic'").fetchone()[0],
                         "sk-secret")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0], 1)

    def test_retention_and_listing(self):
        from backend import backup
        start = datetime(2026, 9, 1, 2, 0)
        for week in range(10):
            backup.make_backup("essentials", start + timedelta(weeks=week))
        rows = [r for r in backup.list_backups() if r["kind"] == "essentials"]
        self.assertEqual(len(rows), backup.KEEP["essentials"])
        self.assertEqual(rows[0]["created_at"], (start + timedelta(weeks=9)).isoformat())
        self.assertIsNone(backup.backup_path("../trading_agent.db"))
        self.assertIsNotNone(backup.backup_path(rows[0]["name"]))

    def test_weekly_run_sends_essentials(self):
        from backend import backup
        sent = []
        result = backup.run_weekly_backup(datetime(2026, 11, 22, 2, 5),
                                          send=lambda path, caption: sent.append(path) or {"status": "sent"})
        self.assertEqual(result["telegram"], {"status": "sent"})
        self.assertIn("essentials", os.path.basename(sent[0]))
        self.assertEqual({r["kind"] for r in backup.list_backups()}, {"essentials", "full"})

    def test_telegram_not_configured(self):
        from backend import backup
        from backend.db import set_setting
        set_setting("telegram_bot_token", None)
        result = backup.make_backup("essentials")
        sent = backup.send_to_telegram(os.path.join(backup.backup_dir(), result["name"]), "x")
        self.assertEqual(sent["status"], "not_sent")

    def test_api_list_run_download(self):
        with fresh_test_client() as client:
            login(client)
            response = client.post("/api/backups", json={"kind": "essentials"}, headers=csrf_headers(client))
            self.assertEqual(response.status_code, 200, response.text)
            name = response.json()["name"]
            listing = client.get("/api/backups").json()
            self.assertEqual(listing["backups"][0]["name"], name)
            download = client.get(f"/api/backups/{name}/download")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content[:2], b"PK")
            self.assertEqual(client.get("/api/backups/..%2Ftrading_agent.db/download").status_code, 404)


class ScheduleTests(unittest.TestCase):
    def test_backup_due(self):
        from backend.backup import backup_due
        sunday = datetime(2026, 11, 22, 2, 0)
        self.assertTrue(backup_due(sunday, None))
        self.assertTrue(backup_due(sunday, sunday - timedelta(days=7)))
        self.assertFalse(backup_due(sunday.replace(hour=1), sunday - timedelta(days=7)))
        self.assertFalse(backup_due(sunday, sunday - timedelta(hours=3)))
        self.assertFalse(backup_due(datetime(2026, 11, 25, 10), sunday))          # Wednesday, 3 days old
        self.assertTrue(backup_due(datetime(2026, 11, 30, 1), datetime(2026, 11, 21, 2)))   # 8+ days old


if __name__ == "__main__":
    unittest.main()
