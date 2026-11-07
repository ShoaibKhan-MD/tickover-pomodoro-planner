#!/usr/bin/env python3
"""Create a consistent SQLite backup in the app's configured data directory."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

base = Path(__file__).resolve().parent

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env(base / ".env")
data_dir = Path(os.getenv("DATA_DIR") or str(base / "data")).expanduser()
source = Path(os.getenv("DATABASE_PATH") or str(data_dir / "study.db")).expanduser()
backup_dir = data_dir / "backups"
backup_dir.mkdir(parents=True, exist_ok=True)
if not source.exists():
    raise SystemExit(f"Database not found: {source}")

try:
    zone = ZoneInfo(os.getenv("APP_TIMEZONE") or "UTC")
except Exception:
    zone = ZoneInfo("UTC")
now = datetime.now(zone)
target = backup_dir / f"study-{now:%Y%m%d-%H%M%S}.db"
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
cutoff = now - timedelta(days=14)
for item in backup_dir.glob("study-*.db"):
    try:
        stamp = datetime.strptime(item.stem.removeprefix("study-"), "%Y%m%d-%H%M%S").replace(tzinfo=zone)
        if stamp < cutoff:
            item.unlink()
    except ValueError:
        continue
print(f"Backup created: {target}")
