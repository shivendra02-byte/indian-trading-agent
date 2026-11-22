"""Backups of the app's data: list, back up now, download."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import backup

router = APIRouter(prefix="/api/backups", tags=["backups"])


class BackupRequest(BaseModel):
    kind: Literal["essentials", "full"] = "essentials"
    to_telegram: bool = False


@router.get("")
def list_backups():
    last = backup.last_backup_at("essentials")
    return {
        "enabled": backup.enabled(),
        "folder": backup.backup_dir(),
        "keep": backup.KEEP,
        "last_essentials_at": last.isoformat(timespec="seconds") if last else None,
        "schedule": "Sundays from 02:00 IST (catches up if missed)",
        "backups": backup.list_backups(),
    }


@router.post("")
def back_up_now(data: BackupRequest):
    result = backup.run_backup_now(data.kind, data.to_telegram)
    if result.get("status") == "busy":
        raise HTTPException(409, "a backup is already running")
    return result


@router.get("/{name}/download")
def download(name: str):
    path = backup.backup_path(name)
    if not path:
        raise HTTPException(404, "backup not found")
    return FileResponse(path, media_type="application/zip", filename=name)
