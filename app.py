# /// script
# requires-python = ">=3.10"
# dependencies = ["fastapi>=0.115", "uvicorn[standard]>=0.30", "pydantic>=2.7", "tzdata", "python-multipart>=0.0.20,<1.0"]
# ///
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import webbrowser
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import base64
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

BASE_DIR = Path(__file__).resolve().parent
# PyInstaller places macOS app resources in Contents/Resources while Python
# modules can resolve from Contents/Frameworks. Search both layouts.
_resource_candidates = [
    BASE_DIR / "static",
    Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "static",
    Path(sys.executable).resolve().parent.parent / "Resources" / "static",
    Path(sys.executable).resolve().parent / "static",
]
STATIC_DIR = next((candidate for candidate in _resource_candidates if candidate.is_dir()), BASE_DIR / "static")
APP_VERSION = "1.3.0"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Load configuration before deriving any paths or timezone values. This matters
# for DATABASE_PATH, DATA_DIR and APP_TIMEZONE in self-hosted installations.
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("DATA_DIR") or str(BASE_DIR / "data")).expanduser()
DB_PATH = Path(os.getenv("DATABASE_PATH") or str(DATA_DIR / "study.db")).expanduser()
TZ = ZoneInfo(os.getenv("APP_TIMEZONE") or "UTC")
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "")
TOKEN_FILE = DATA_DIR / "agent_token"
DB_LOCK = threading.RLock()


def current_token() -> str:
    """Token from .env if set, otherwise the one generated from Settings."""
    if AGENT_API_TOKEN:
        return AGENT_API_TOKEN
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        return ""
APP_PASSWORD = os.getenv("APP_PASSWORD", "")  # optional: password-protects the web UI
security = HTTPBearer(auto_error=False)

@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    load_saved_timezone()
    stop_event = threading.Event()
    monitor = threading.Thread(target=timer_monitor_loop, args=(stop_event,), name="tickover-timer", daemon=True)
    monitor.start()
    try:
        yield
    finally:
        stop_event.set()
        monitor.join(timeout=2)


app = FastAPI(
    lifespan=lifespan,
    title="Tickover API",
    version=APP_VERSION,
    description="Local-first schedule-aware Pomodoro app with a task API, rollover proposals, capacity checks, activity history and analytics. Designed for a single self-hosted user.",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.middleware("http")
async def support_private_subpath(request: Request, call_next):
    # Tailscale Serve can mount this app under /pomodoro alongside another
    # dashboard on the same HTTPS hostname. Accept both prefixed and root paths.
    prefix = os.getenv("APP_BASE_PATH", "/pomodoro").rstrip("/")
    path = request.scope.get("path", "")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        stripped = path[len(prefix):] or "/"
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")
        request.scope["root_path"] = request.scope.get("root_path", "") + prefix
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def optional_password(request: Request, call_next):
    """If APP_PASSWORD is set, the web UI asks for it (HTTP Basic; any username).
    The agent API keeps its own bearer token, and /api/health stays open."""
    path = request.url.path
    if APP_PASSWORD and "/api/v1/agent/" not in path and not path.endswith("/api/health"):
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
                ok = secrets.compare_digest(supplied, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Password required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Tickover"'})
    return await call_next(request)


def now_ts() -> float:
    return time.time()


def local_now() -> datetime:
    return datetime.now(TZ)


def iso_now() -> str:
    return local_now().isoformat(timespec="microseconds")


def date_today() -> str:
    return local_now().date().isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


@contextmanager
def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_days (
                day TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL DEFAULT '',
                q_target INTEGER NOT NULL DEFAULT 0,
                qbank TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                day TEXT NOT NULL REFERENCES study_days(day) ON DELETE CASCADE,
                external_id TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'study',
                subject TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 25,
                items_json TEXT NOT NULL DEFAULT '[]',
                total_runtime INTEGER NOT NULL DEFAULT 0,
                real_min INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                notes TEXT NOT NULL DEFAULT '',
                questions_done INTEGER NOT NULL DEFAULT 0,
                questions_correct INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(day, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_day_sort ON tasks(day, sort_order);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                day TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'work',
                label TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL,
                planned_seconds INTEGER NOT NULL DEFAULT 0,
                focus_seconds INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'in_progress',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions(day);
            CREATE TABLE IF NOT EXISTS timers (
                day TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Forward-compatible, additive migrations for existing local databases.
        migrations = {
            "tasks": {
                "priority": "TEXT NOT NULL DEFAULT 'normal'",
                "deadline": "TEXT NOT NULL DEFAULT ''",
                "locked": "INTEGER NOT NULL DEFAULT 0",
                "origin_day": "TEXT NOT NULL DEFAULT ''",
                "rollover_count": "INTEGER NOT NULL DEFAULT 0",
            },
            "study_days": {
                "revision": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        for table, columns in migrations.items():
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_at TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                action TEXT NOT NULL,
                target_day TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_activity_event ON activity_log(event_at DESC);
            CREATE TABLE IF NOT EXISTS schedule_proposals (
                proposal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source_day TEXT NOT NULL,
                target_day TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                applied_at TEXT,
                undone_at TEXT
            );
            """
        )
        proposal_columns = {row[1] for row in conn.execute("PRAGMA table_info(schedule_proposals)").fetchall()}
        if "undone_at" not in proposal_columns:
            conn.execute("ALTER TABLE schedule_proposals ADD COLUMN undone_at TEXT")
        defaults = {
            "pomodoro_min": 25,
            "short_break_min": 5,
            "long_break_min": 15,
            "tick_enabled": True,
            "tick_volume": 22,
            "chime_enabled": True,
            "chime_volume": 30,
            "chime_start_volume": 12,
            "chime_end_volume": 35,
            "music_enabled": False,
            "music_volume": 35,
            "music_id": "k9ts6p63ns0",
            "music_source": "lofi1",
            "tick_sound": "wall",
            "font_style": "classic",
            "theme": "rose",
            "theme_colors": "",
            "tray_icon": True,
            "timezone": "",
            "focus_on_start": True,
            "auto_advance": False,
            "capacity_enabled": False,
            "daily_capacity_minutes": 480,
            "rollover_mode": "approval",
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES(?,?,?)", (key, json.dumps(value), iso_now()))


def undo_rollover_proposal(conn: sqlite3.Connection, proposal_id: str, actor: str = "user") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM schedule_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row or not row["applied_at"]:
        raise HTTPException(404, "Applied rollover proposal not found.")
    if row["undone_at"]:
        raise HTTPException(409, "This rollover has already been undone.")
    payload = json_load(row["payload_json"], {})
    source_day, target_day = row["source_day"], row["target_day"]
    snapshots = payload.get("applied_snapshots", [])
    moved_ids = {snap["id"] for snap in snapshots}
    target_timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (target_day,)).fetchone()
    target_state = json_load(target_timer_row["state_json"], {}) if target_timer_row else {}
    if target_state.get("session_id") and target_state.get("task_id") in moved_ids:
        raise HTTPException(409, "A carried-over task is currently running. Pause it before undoing rollover.")
    undone, impacted_source_days = [], set()
    for snapshot in snapshots:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (snapshot["id"],)).fetchone()
        original_day = snapshot["source_day"]
        if not task or task["day"] != target_day or task["updated_at"] != snapshot["applied_updated_at"]:
            label = task["label"] if task else snapshot["id"]
            raise HTTPException(409, f"Task {label} changed after rollover. Undo was stopped to protect your edits.")
        collision = conn.execute("SELECT 1 FROM tasks WHERE day=? AND external_id=? AND id<>?", (original_day, snapshot["external_id"], snapshot["id"])).fetchone()
        if collision:
            raise HTTPException(409, f"Cannot restore task {task['label']}: its original external ID is now in use on the source day.")
        conn.execute(
            "UPDATE tasks SET day=?,external_id=?,status=?,origin_day=?,rollover_count=?,sort_order=?,updated_at=? WHERE id=?",
            (original_day, snapshot["external_id"], snapshot["status"], snapshot["origin_day"], snapshot["rollover_count"], snapshot["sort_order"], iso_now(), snapshot["id"]),
        )
        undone.append(snapshot["id"])
        impacted_source_days.add(original_day)
    if undone:
        if target_state and target_state.get("task_id") in moved_ids and not target_state.get("session_id"):
            first = conn.execute("SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1", (target_day,)).fetchone()
            save_timer(conn, initial_timer_state(target_day, first, get_settings(conn)))
        for original_day in impacted_source_days:
            source_timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (original_day,)).fetchone()
            source_state = json_load(source_timer_row["state_json"], {}) if source_timer_row else {}
            if source_state and source_state.get("task_id") is None:
                first = conn.execute("SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1", (original_day,)).fetchone()
                save_timer(conn, initial_timer_state(original_day, first, get_settings(conn)))
            bump_revision(conn, original_day)
        bump_revision(conn, target_day)
    conn.execute("UPDATE schedule_proposals SET undone_at=? WHERE proposal_id=?", (iso_now(), proposal_id))
    log_activity(conn, actor, "rollover.undone", target_day, {"proposal_id": proposal_id, "source_day": source_day, "undone_ids": undone})
    return {"ok": True, "undone_count": len(undone), "undone_ids": undone,
            "source_capacities": [capacity_summary(conn, d) for d in sorted(impacted_source_days)],
            "target_capacity": capacity_summary(conn, target_day)}


def load_saved_timezone() -> None:
    """Use the timezone the browser reported on first run, unless .env fixes one."""
    global TZ
    if os.getenv("APP_TIMEZONE"):
        return
    with db() as conn:
        row = conn.execute("SELECT value_json FROM settings WHERE key='timezone'").fetchone()
    try:
        name = json.loads(row[0]) if row else ""
        if name:
            TZ = ZoneInfo(name)
        else:
            local_zone = datetime.now().astimezone().tzinfo
            if getattr(local_zone, "key", None):
                TZ = ZoneInfo(local_zone.key)
            else:
                # Fall back to the system's fixed offset when no IANA key is
                # available (common on macOS/Windows) so "today" matches the
                # user's local calendar date instead of UTC.
                TZ = local_zone
    except Exception:
        pass


def auto_rollover_for_day(day: str) -> Optional[dict[str, Any]]:
    """Apply one capacity-aware rollover from the last 31 days when enabled.

    A proposal is applied at most once per source/target date pair. Tasks that
    do not fit remain pending on their original dates and can be reconsidered
    during the next day's rollover.
    """
    try:
        target = date.fromisoformat(day)
    except ValueError:
        return None
    source_day = (target - timedelta(days=1)).isoformat()
    with db() as conn:
        settings = get_settings(conn)
        if settings.get("rollover_mode", "approval") != "automatic":
            return None
        applied = conn.execute(
            "SELECT 1 FROM schedule_proposals WHERE source_day=? AND target_day=? AND applied_at IS NOT NULL LIMIT 1",
            (source_day, day),
        ).fetchone()
        if applied:
            return None
        proposal = build_rollover_proposal(conn, RolloverRequest(source_day=source_day, target_day=day))
        return apply_rollover_proposal(conn, proposal["proposal_id"], proposal["target_revision"], "system")


async def require_agent(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> None:
    token = current_token()
    if not token:
        raise HTTPException(status_code=503, detail="Agent API token is not configured on the server.")
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not supplied or not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Invalid or missing agent API token.")


class TaskInput(BaseModel):
    external_id: str = Field(default="", max_length=200)
    type: str = Field(default="study", pattern=r"^(study|video|anki|questions|build|review)$")
    subject: str = Field(default="", max_length=120)
    label: str = Field(min_length=1, max_length=240)
    duration: int = Field(default=25, ge=1, le=720)
    priority: str = Field(default="normal", pattern=r"^(critical|high|normal|low)$")
    deadline: str = Field(default="", max_length=10)
    locked: bool = False
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    total_runtime: int = Field(default=0, ge=0, le=1440)
    real_min: int = Field(default=0, ge=0, le=1440)
    sort_order: int = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=5000)
    questions_done: int = Field(default=0, ge=0, le=10000)
    questions_correct: Optional[int] = Field(default=None, ge=0, le=10000)

    @model_validator(mode="after")
    def validate_question_counts(self):
        self.label = self.label.strip()
        if not self.label:
            raise ValueError("Task label cannot be blank.")
        if self.questions_correct is not None and self.questions_correct > self.questions_done:
            raise ValueError("questions_correct cannot exceed questions_done")
        if self.deadline:
            try:
                date.fromisoformat(self.deadline)
            except ValueError as exc:
                raise ValueError("deadline must use YYYY-MM-DD format.") from exc
        return self


class PlanInput(BaseModel):
    expected_revision: Optional[int] = Field(default=None, ge=0)
    phase: Optional[str] = None
    target_date: Optional[str] = None
    q_target: Optional[int] = Field(default=None, ge=0, le=10000)
    qbank: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    tasks: list[TaskInput] = Field(default_factory=list, max_length=500)
    replace_existing: bool = False
    override_locked: bool = False


class TaskPatch(BaseModel):
    type: Optional[str] = Field(default=None, pattern=r"^(study|video|anki|questions|build|review)$")
    subject: Optional[str] = Field(default=None, max_length=120)
    label: Optional[str] = Field(default=None, min_length=1, max_length=240)
    duration: Optional[int] = Field(default=None, ge=1, le=720)
    priority: Optional[str] = Field(default=None, pattern=r"^(critical|high|normal|low)$")
    deadline: Optional[str] = Field(default=None, max_length=10)
    locked: Optional[bool] = None
    items: Optional[list[dict[str, Any]]] = Field(default=None, max_length=50)
    total_runtime: Optional[int] = Field(default=None, ge=0, le=1440)
    real_min: Optional[int] = Field(default=None, ge=0, le=1440)
    sort_order: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=5000)
    questions_done: Optional[int] = Field(default=None, ge=0, le=10000)
    questions_correct: Optional[int] = Field(default=None, ge=0, le=10000)


class SettingsInput(BaseModel):
    pomodoro_min: Optional[int] = Field(default=None, ge=1, le=180)
    short_break_min: Optional[int] = Field(default=None, ge=1, le=60)
    long_break_min: Optional[int] = Field(default=None, ge=1, le=120)
    tick_enabled: Optional[bool] = None
    tick_volume: Optional[int] = Field(default=None, ge=0, le=100)
    chime_enabled: Optional[bool] = None
    chime_volume: Optional[int] = Field(default=None, ge=0, le=100)
    chime_start_volume: Optional[int] = Field(default=None, ge=0, le=100)
    chime_end_volume: Optional[int] = Field(default=None, ge=0, le=100)
    music_enabled: Optional[bool] = None
    music_volume: Optional[int] = Field(default=None, ge=0, le=100)
    music_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{6,20}$")
    music_source: Optional[str] = Field(default=None, pattern=r"^[a-z0-9]{3,12}$")
    tick_sound: Optional[str] = Field(default=None, pattern=r"^[a-z0-9]{3,12}$")
    font_style: Optional[str] = Field(default=None, pattern=r"^[a-z]{3,12}$")
    theme: Optional[str] = Field(default=None, pattern=r"^[a-z]{3,12}$")
    theme_colors: Optional[str] = Field(default=None, pattern=r"^(#[0-9a-fA-F]{6}(,#[0-9a-fA-F]{6}){3})?$")
    tray_icon: Optional[bool] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    focus_on_start: Optional[bool] = None
    auto_advance: Optional[bool] = None
    capacity_enabled: Optional[bool] = None
    daily_capacity_minutes: Optional[int] = Field(default=None, ge=30, le=1440)
    rollover_mode: Optional[str] = Field(default=None, pattern=r"^(automatic|approval|manual)$")


class RolloverRequest(BaseModel):
    source_day: str
    target_day: str
    include_locked: bool = False

    @model_validator(mode="after")
    def validate_days(self):
        try:
            source = date.fromisoformat(self.source_day)
            target = date.fromisoformat(self.target_day)
        except ValueError as exc:
            raise ValueError("Dates must use YYYY-MM-DD format.") from exc
        if target <= source:
            raise ValueError("target_day must be after source_day.")
        if (target - source).days > 31:
            raise ValueError("Rollover is limited to 31 days ahead.")
        return self


class ApplyProposalInput(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)
    expected_target_revision: Optional[int] = Field(default=None, ge=0)


class UndoProposalInput(BaseModel):
    proposal_id: str = Field(min_length=1, max_length=64)


def bump_revision(conn: sqlite3.Connection, day: str) -> None:
    conn.execute("UPDATE study_days SET revision=revision+1,updated_at=? WHERE day=?", (iso_now(), day))


def log_activity(conn: sqlite3.Connection, actor: str, action: str, target_day: str = "", details: Optional[dict[str, Any]] = None) -> None:
    conn.execute(
        "INSERT INTO activity_log(event_at,actor,action,target_day,details_json) VALUES(?,?,?,?,?)",
        (iso_now(), actor[:80], action[:100], target_day[:10], json.dumps(details or {}, ensure_ascii=False)),
    )
    # Keep a bounded, useful audit trail on local installs.
    conn.execute("DELETE FROM activity_log WHERE id NOT IN (SELECT id FROM activity_log ORDER BY id DESC LIMIT 2000)")


def capacity_summary(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    """Optional daily study limit.

    Used time = time already studied + time still planned, so finishing a block on
    time never frees capacity. Finishing early frees only the minutes saved, and
    running over uses up free time.
    """
    settings = get_settings(conn)
    enabled = bool(settings.get("capacity_enabled", False))
    capacity = int(settings.get("daily_capacity_minutes", 480))
    studied_by_task = {
        r["task_id"]: (r["secs"] or 0) / 60
        for r in conn.execute("SELECT task_id, SUM(focus_seconds) AS secs FROM sessions WHERE day=? AND kind='work' GROUP BY task_id", (day,))
    }
    studied = float(studied_by_task.pop(None, 0))  # time spent on blocks that were deleted since
    still_planned, open_count = 0.0, 0
    for t in conn.execute("SELECT id, duration, status FROM tasks WHERE day=?", (day,)):
        done = studied_by_task.get(t["id"], 0.0)
        if t["status"] == "completed":
            studied += done if done > 0 else int(t["duration"])  # ticked off without the timer: assume as planned
        else:
            studied += done
            if t["status"] != "skipped":
                still_planned += max(0.0, int(t["duration"]) - done)
                open_count += 1
    used = round(studied + still_planned)
    start = (date.fromisoformat(day) - timedelta(days=14)).isoformat()
    earlier = conn.execute(
        "SELECT SUM(focus_seconds) AS secs FROM sessions WHERE kind='work' AND day>=? AND day<? GROUP BY day HAVING SUM(focus_seconds)>0", (start, day)
    ).fetchall()
    average = round(sum(r["secs"] for r in earlier) / 60 / len(earlier)) if earlier else None
    return {
        "date": day,
        "enabled": enabled,
        "daily_capacity_minutes": capacity,
        "effective_capacity_minutes": capacity,
        "studied_minutes": round(studied),
        "remaining_planned_minutes": round(still_planned),
        "planned_minutes": used,
        "used_minutes": used,
        "remaining_minutes": max(0, capacity - used),
        "over_capacity_minutes": max(0, used - capacity) if enabled else 0,
        "utilization_percent": round(used / capacity * 100, 1) if capacity else 0.0,
        "task_count": open_count,
        "is_over_capacity": enabled and used > capacity,
        "recent_average_minutes": average,
    }

def priority_rank(value: str) -> int:
    return {"critical": 0, "high": 1, "normal": 2, "low": 3}.get(value, 2)


def build_rollover_proposal(conn: sqlite3.Connection, request: RolloverRequest) -> dict[str, Any]:
    ensure_day(conn, request.source_day)
    ensure_day(conn, request.target_day)
    oldest_day = (date.fromisoformat(request.target_day) - timedelta(days=31)).isoformat()
    target_capacity = capacity_summary(conn, request.target_day)
    available = target_capacity["remaining_minutes"] if target_capacity["enabled"] else 10**9
    timer_rows = conn.execute(
        "SELECT state_json FROM timers WHERE day BETWEEN ? AND ?", (oldest_day, request.source_day)
    ).fetchall()
    active_task_ids = set()
    for timer_row in timer_rows:
        state = json_load(timer_row["state_json"], {})
        if state.get("session_id") and state.get("task_id"):
            active_task_ids.add(state["task_id"])
    candidates = conn.execute(
        "SELECT * FROM tasks WHERE day BETWEEN ? AND ? AND status IN ('pending','in_progress') ORDER BY day,sort_order,id",
        (oldest_day, request.source_day),
    ).fetchall()
    eligible, excluded_locked, excluded_active = [], [], []
    for task in candidates:
        item = task_row(task)
        if bool(task["locked"]) and not request.include_locked:
            excluded_locked.append(item)
        elif task["id"] in active_task_ids:
            excluded_active.append(item)
        else:
            eligible.append(task)
    # Urgency first, then deadline, then oldest origin day and user-defined order.
    eligible.sort(key=lambda t: (priority_rank(t["priority"]), t["deadline"] or "9999-12-31", t["day"], t["sort_order"], t["created_at"]))
    move, defer = [], []
    for task in eligible:
        item = task_row(task)
        if int(task["duration"]) <= available:
            move.append(item)
            available -= int(task["duration"])
        else:
            defer.append(item)
    proposal_id = new_id()
    target_row = conn.execute("SELECT revision FROM study_days WHERE day=?", (request.target_day,)).fetchone()
    source_revisions = {}
    for source_day in sorted({t["day"] for t in candidates}):
        revision_row = conn.execute("SELECT revision FROM study_days WHERE day=?", (source_day,)).fetchone()
        source_revisions[source_day] = int(revision_row["revision"] if revision_row else 0)
    payload = {
        "proposal_id": proposal_id, "source_day": request.source_day, "target_day": request.target_day,
        "created_at": iso_now(), "target_revision": int(target_row["revision"] if target_row else 0),
        "source_revisions": source_revisions,
        "include_locked": request.include_locked, "lookback_days": 31,
        "capacity": target_capacity, "moving": move, "deferred": defer,
        "excluded_locked": excluded_locked, "excluded_active": excluded_active,
        "moving_minutes": sum(int(t["duration"]) for t in move),
        "deferred_minutes": sum(int(t["duration"]) for t in defer),
        "resulting_planned_minutes": target_capacity["planned_minutes"] + sum(int(t["duration"]) for t in move),
        "source_days": sorted({t["day"] for t in candidates}),
    }
    conn.execute(
        "INSERT INTO schedule_proposals(proposal_id,created_at,source_day,target_day,payload_json) VALUES(?,?,?,?,?)",
        (proposal_id, iso_now(), request.source_day, request.target_day, json.dumps(payload, ensure_ascii=False)),
    )
    return payload


def apply_rollover_proposal(conn: sqlite3.Connection, proposal_id: str, expected_target_revision: Optional[int] = None, actor: str = "user") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM schedule_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Rollover proposal not found.")
    if row["applied_at"]:
        raise HTTPException(409, "This rollover proposal has already been applied.")
    payload = json_load(row["payload_json"], {})
    source_day, target_day = row["source_day"], row["target_day"]
    target_rev_row = conn.execute("SELECT revision FROM study_days WHERE day=?", (target_day,)).fetchone()
    current_revision = int(target_rev_row["revision"] if target_rev_row else 0)
    required_revision = expected_target_revision if expected_target_revision is not None else payload.get("target_revision")
    if required_revision is not None and current_revision != int(required_revision):
        raise HTTPException(409, "The target schedule changed after this preview. Create a new preview.")
    for source_day, expected_revision in payload.get("source_revisions", {}).items():
        source_row = conn.execute("SELECT revision FROM study_days WHERE day=?", (source_day,)).fetchone()
        source_revision = int(source_row["revision"] if source_row else 0)
        if source_revision != int(expected_revision):
            raise HTTPException(409, "A source-day schedule changed after this preview. Create a new preview.")
    refreshed_capacity = capacity_summary(conn, target_day)
    saved_capacity = payload.get("capacity", {})
    if (refreshed_capacity["enabled"] != saved_capacity.get("enabled")
            or (refreshed_capacity["enabled"]
                and refreshed_capacity["daily_capacity_minutes"] != saved_capacity.get("daily_capacity_minutes"))):
        raise HTTPException(409, "The daily study limit changed after this preview. Create a new preview.")
    moving_minutes = sum(int(item.get("duration", 0)) for item in payload.get("moving", []))
    if refreshed_capacity["enabled"] and moving_minutes > refreshed_capacity["remaining_minutes"]:
        raise HTTPException(409, "The proposed tasks no longer fit the target day's capacity. Create a new preview.")
    moved_ids, applied_snapshots, impacted_source_days = [], [], set()
    next_order = int(conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM tasks WHERE day=?", (target_day,)).fetchone()[0])
    for item in payload.get("moving", []):
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (item["id"],)).fetchone()
        original_day = item.get("day", source_day)
        if not task or task["day"] != original_day or task["day"] > source_day or task["status"] not in {"pending", "in_progress"}:
            raise HTTPException(409, f"Task {item.get('label', item['id'])} changed after preview. Create a new preview.")
        if bool(task["locked"]) and not payload.get("include_locked", False):
            raise HTTPException(409, f"Task {task['label']} is now locked. Create a new preview.")
        if item.get("updated_at") and task["updated_at"] != item["updated_at"]:
            raise HTTPException(409, f"Task {task['label']} changed after preview. Create a new preview.")
        external_id = task["external_id"]
        collision = conn.execute("SELECT 1 FROM tasks WHERE day=? AND external_id=?", (target_day, external_id)).fetchone()
        if collision:
            external_id = f"rollover-{original_day}-{external_id}"[:200]
            suffix, candidate = 1, external_id
            while conn.execute("SELECT 1 FROM tasks WHERE day=? AND external_id=?", (target_day, candidate)).fetchone():
                suffix += 1
                candidate = f"{external_id[:185]}-{suffix}"
            external_id = candidate
        applied_stamp = iso_now()
        applied_snapshots.append({
            "id": task["id"], "source_day": original_day, "external_id": task["external_id"],
            "status": task["status"], "origin_day": task["origin_day"],
            "rollover_count": task["rollover_count"], "sort_order": task["sort_order"],
            "applied_updated_at": applied_stamp,
        })
        conn.execute(
            "UPDATE tasks SET day=?,external_id=?,status='pending',origin_day=CASE WHEN origin_day='' THEN ? ELSE origin_day END,"
            "rollover_count=rollover_count+1,sort_order=?,updated_at=? WHERE id=?",
            (target_day, external_id, original_day, next_order, applied_stamp, task["id"]),
        )
        moved_ids.append(task["id"])
        impacted_source_days.add(original_day)
        next_order += 1
    for impacted_day in impacted_source_days:
        timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (impacted_day,)).fetchone()
        state = json_load(timer_row["state_json"], {}) if timer_row else {}
        moved_for_day = {snap["id"] for snap in applied_snapshots if snap["source_day"] == impacted_day}
        if state and state.get("task_id") in moved_for_day and not state.get("session_id"):
            next_row = conn.execute("SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1", (impacted_day,)).fetchone()
            save_timer(conn, initial_timer_state(impacted_day, next_row, get_settings(conn)))
        bump_revision(conn, impacted_day)
    if moved_ids:
        bump_revision(conn, target_day)
    payload["applied_snapshots"] = applied_snapshots
    conn.execute("UPDATE schedule_proposals SET applied_at=?,payload_json=? WHERE proposal_id=?", (iso_now(), json.dumps(payload, ensure_ascii=False), proposal_id))
    log_activity(conn, actor, "rollover.applied", target_day, {
        "proposal_id": proposal_id, "source_day": source_day, "moved_ids": moved_ids,
        "deferred_ids": [t["id"] for t in payload.get("deferred", [])],
    })
    return {"ok": True, "proposal_id": proposal_id, "moved_count": len(moved_ids), "moved_ids": moved_ids,
            "deferred_count": len(payload.get("deferred", [])),
            "source_capacities": [capacity_summary(conn, d) for d in sorted(impacted_source_days)],
            "target_capacity": capacity_summary(conn, target_day)}


def ensure_day(conn: sqlite3.Connection, day: str) -> None:
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD format.")
    conn.execute(
        "INSERT OR IGNORE INTO study_days(day,created_at,updated_at) VALUES(?,?,?)",
        (day, iso_now(), iso_now()),
    )


def task_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "day": row["day"], "external_id": row["external_id"],
        "type": row["type"], "subject": row["subject"], "label": row["label"],
        "duration": row["duration"], "items": json_load(row["items_json"], []),
        "total_runtime": row["total_runtime"], "real_min": row["real_min"],
        "sort_order": row["sort_order"], "status": row["status"], "notes": row["notes"],
        "questions_done": row["questions_done"], "questions_correct": row["questions_correct"],
        "priority": row["priority"], "deadline": row["deadline"], "locked": bool(row["locked"]),
        "origin_day": row["origin_day"], "rollover_count": row["rollover_count"],
        "updated_at": row["updated_at"],
    }


def initial_timer_state(day: str, first_task: Optional[sqlite3.Row], settings: dict[str, Any]) -> dict[str, Any]:
    duration = int(first_task["duration"]) if first_task else int(settings.get("pomodoro_min", 25))
    return {
        "day": day, "task_id": first_task["id"] if first_task else None,
        "mode": "work", "running": False, "remaining_seconds": duration * 60,
        "ends_at": None, "planned_seconds": duration * 60, "session_id": None,
        "focus_seconds": 0, "last_resumed_at": None, "completed_pomodoros": 0,
        "last_event": None, "last_event_at": None,
    }


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key,value_json FROM settings").fetchall()
    return {r["key"]: json_load(r["value_json"], None) for r in rows}


def save_timer(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO timers(day,state_json,updated_at) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
        (state["day"], json.dumps(state), iso_now()),
    )


def active_focus_seconds(state: dict[str, Any], now: float) -> int:
    total = int(state.get("focus_seconds", 0))
    if state.get("running") and state.get("mode") == "work" and state.get("last_resumed_at"):
        total += max(0, int(now - float(state["last_resumed_at"])))
    return total


def get_timer(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
    if row:
        state = json_load(row["state_json"], {})
        if state:
            state = reconcile_timer(conn, state)
            if not state.get("task_id") and state.get("mode") == "work" and not state.get("running"):
                first = conn.execute("SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1", (day,)).fetchone()
                if first:
                    duration = int(first["duration"])
                    state.update({"task_id": first["id"], "remaining_seconds": duration * 60, "planned_seconds": duration * 60})
                    save_timer(conn, state)
            return state
    first = conn.execute("SELECT * FROM tasks WHERE day=? AND status!='completed' AND status!='skipped' ORDER BY sort_order LIMIT 1", (day,)).fetchone()
    state = initial_timer_state(day, first, get_settings(conn))
    save_timer(conn, state)
    return state


def find_task(conn: sqlite3.Connection, task_id: Optional[str]) -> Optional[sqlite3.Row]:
    if not task_id:
        return None
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def next_task(conn: sqlite3.Connection, day: str, current_task_id: Optional[str]) -> Optional[sqlite3.Row]:
    current = find_task(conn, current_task_id)
    if current:
        row = conn.execute(
            "SELECT * FROM tasks WHERE day=? AND sort_order>? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1",
            (day, current["sort_order"]),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order LIMIT 1",
        (day,),
    ).fetchone()


def close_running_segment(state: dict[str, Any], now: float) -> int:
    if state.get("running") and state.get("mode") == "work" and state.get("last_resumed_at"):
        elapsed = max(0, int(now - float(state["last_resumed_at"])))
        remaining_cap = max(0, int(state.get("planned_seconds", 0)) - int(state.get("focus_seconds", 0)))
        state["focus_seconds"] = int(state.get("focus_seconds", 0)) + min(elapsed, remaining_cap)
        state["last_resumed_at"] = None
        return elapsed
    return 0


def finish_session(conn: sqlite3.Connection, state: dict[str, Any], status: str, now: float) -> None:
    if state.get("session_id"):
        close_running_segment(state, now)
        conn.execute(
            "UPDATE sessions SET ended_at=?,focus_seconds=?,status=? WHERE id=?",
            (now, int(state.get("focus_seconds", 0)), status, state["session_id"]),
        )


def record_event(state: dict[str, Any], event: str) -> None:
    state["last_event"] = event
    state["last_event_at"] = now_ts()


def complete_work(conn: sqlite3.Connection, state: dict[str, Any], now: float) -> None:
    task = find_task(conn, state.get("task_id"))
    finish_session(conn, state, "completed", now)
    if task:
        conn.execute("UPDATE tasks SET status='completed',updated_at=? WHERE id=?", (iso_now(), task["id"]))
        bump_revision(conn, task["day"])
    state["completed_pomodoros"] = int(state.get("completed_pomodoros", 0)) + 1
    settings = get_settings(conn)
    is_long = state["completed_pomodoros"] % 4 == 0
    break_min = int(settings.get("long_break_min", 15)) if is_long else int(settings.get("short_break_min", 5))
    state["break_kind"] = "long" if is_long else "short"
    state.update({"mode": "break", "running": False, "remaining_seconds": break_min * 60,
                  "planned_seconds": break_min * 60, "ends_at": None, "session_id": None,
                  "focus_seconds": 0, "last_resumed_at": None})
    record_event(state, "work_complete")


def complete_break(conn: sqlite3.Connection, state: dict[str, Any], now: float) -> None:
    task = next_task(conn, state["day"], state.get("task_id"))
    settings = get_settings(conn)
    duration = int(task["duration"]) if task else int(settings.get("pomodoro_min", 25))
    state.update({"task_id": task["id"] if task else None, "mode": "work", "running": False,
                  "remaining_seconds": duration * 60, "planned_seconds": duration * 60,
                  "ends_at": None, "session_id": None, "focus_seconds": 0,
                  "last_resumed_at": None})
    record_event(state, "break_complete")


def reconcile_timer(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    """When the deadline passes, stop at 0 and wait for the user to press Complete."""
    if state.get("running") and state.get("ends_at") is not None and float(state["ends_at"]) <= now_ts():
        if state.get("mode") == "work":
            close_running_segment(state, float(state["ends_at"]))
            if state.get("session_id"):
                conn.execute("UPDATE sessions SET focus_seconds=? WHERE id=?", (int(state.get("focus_seconds", 0)), state["session_id"]))
        state.update({"remaining_seconds": 0, "running": False, "ends_at": None, "expired": True})
        record_event(state, "time_up")
        save_timer(conn, state)
    return state


def timer_payload(conn: sqlite3.Connection, state: dict[str, Any]) -> dict[str, Any]:
    now = now_ts()
    remaining = max(0, int(float(state["ends_at"]) - now)) if state.get("running") and state.get("ends_at") else int(state.get("remaining_seconds", 0))
    task = find_task(conn, state.get("task_id"))
    return {
        **state,
        "remaining_seconds": remaining,
        "actual_focus_seconds": active_focus_seconds(state, now),
        "task": task_row(task) if task else None,
    }


def day_payload(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    ensure_day(conn, day)
    d = conn.execute("SELECT * FROM study_days WHERE day=?", (day,)).fetchone()
    state = get_timer(conn, day)
    # Timer reconciliation may update a task; query tasks after it runs.
    tasks = conn.execute("SELECT * FROM tasks WHERE day=? ORDER BY sort_order,id", (day,)).fetchall()
    meta = json_load(d["metadata_json"], {})
    completed = sum(1 for t in tasks if t["status"] == "completed")
    skipped = sum(1 for t in tasks if t["status"] == "skipped")
    session_rows = conn.execute("SELECT id,focus_seconds FROM sessions WHERE day=?", (day,)).fetchall()
    focus_total = sum(int(r["focus_seconds"] or 0) for r in session_rows)
    if state.get("session_id"):
        current_row = next((r for r in session_rows if r["id"] == state["session_id"]), None)
        if current_row:
            focus_total += max(0, active_focus_seconds(state, now_ts()) - int(current_row["focus_seconds"] or 0))
    watched_video_items = sum(len(json_load(t["items_json"], [])) for t in tasks if t["type"] == "video" and t["status"] == "completed")
    scored_tasks = [t for t in tasks if t["questions_correct"] is not None]
    scored_done = sum(int(t["questions_done"]) for t in scored_tasks)
    scored_correct = sum(int(t["questions_correct"] or 0) for t in scored_tasks)
    return {
        "day": {"date": d["day"], "phase": d["phase"], "target_date": d["target_date"],
                "q_target": d["q_target"], "qbank": d["qbank"], "metadata": meta,
                "revision": int(d["revision"])},
        "capacity": capacity_summary(conn, day),
        "tasks": [task_row(t) for t in tasks],
        "timer": timer_payload(conn, state),
        "summary": {"total_tasks": len(tasks), "completed": completed, "skipped": skipped,
                    "pending": len(tasks) - completed - skipped,
                    "planned_minutes": sum(int(t["duration"]) for t in tasks),
                    "video_count": sum(len(json_load(t["items_json"], [])) for t in tasks if t["type"] == "video"),
                    "video_items_completed": watched_video_items,
                    "focus_seconds": focus_total,
                    "questions_done": sum(int(t["questions_done"]) for t in tasks),
        "questions_correct": scored_correct,
        "question_accuracy": (round(scored_correct / scored_done * 100, 1) if scored_done else None)},
        "settings": get_settings(conn),
    }


def task_insert(conn: sqlite3.Connection, day: str, item: TaskInput, *, preserve_id: Optional[str] = None) -> str:
    ensure_day(conn, day)
    stamp = iso_now()
    task_id = preserve_id or new_id()
    # UI-created tasks do not send an external ID; give each one a unique
    # local key so multiple manually added tasks can coexist on the same day.
    external = item.external_id.strip() or f"local-{new_id()}"
    if external:
        existing = conn.execute("SELECT id FROM tasks WHERE day=? AND external_id=?", (day, external)).fetchone()
        if existing:
            task_id = existing["id"]
    if item.deadline:
        try:
            date.fromisoformat(item.deadline)
        except ValueError as exc:
            raise HTTPException(400, "deadline must use YYYY-MM-DD format.") from exc
    conn.execute(
        """INSERT INTO tasks(id,day,external_id,type,subject,label,duration,items_json,total_runtime,real_min,sort_order,status,notes,questions_done,questions_correct,priority,deadline,locked,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,type=excluded.type,subject=excluded.subject,label=excluded.label,
             duration=excluded.duration,items_json=excluded.items_json,total_runtime=excluded.total_runtime,real_min=excluded.real_min,
             sort_order=excluded.sort_order,priority=excluded.priority,deadline=excluded.deadline,locked=excluded.locked,updated_at=excluded.updated_at""",
        (task_id, day, external, item.type, item.subject, item.label, item.duration, json.dumps(item.items), item.total_runtime,
         item.real_min, item.sort_order, "pending", item.notes, item.questions_done, item.questions_correct,
         item.priority, item.deadline, int(item.locked), stamp, stamp),
    )
    optional_updates = {}
    for field in ("notes", "questions_done", "questions_correct"):
        if field in item.model_fields_set:
            optional_updates[field] = getattr(item, field)
    if optional_updates:
        assignments = ",".join(f"{name}=?" for name in optional_updates)
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", [*optional_updates.values(), task_id])
    bump_revision(conn, day)
    return task_id


def analytics_data(conn: sqlite3.Connection, start: str, end: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT s.*,t.subject AS task_subject,t.type AS task_type FROM sessions s LEFT JOIN tasks t ON t.id=s.task_id WHERE s.day BETWEEN ? AND ? ORDER BY s.day,s.started_at",
        (start, end),
    ).fetchall()
    day_map: dict[str, dict[str, Any]] = {}
    subject_map: dict[str, int] = {}
    focus_total = 0
    for r in rows:
        seconds = int(r["focus_seconds"] or 0)
        if r["status"] == "in_progress":
            timers = conn.execute("SELECT state_json FROM timers WHERE day=?", (r["day"],)).fetchone()
            if timers:
                st = json_load(timers["state_json"], {})
                if st.get("session_id") == r["id"]:
                    seconds = active_focus_seconds(st, now_ts())
        focus_total += seconds
        day_map.setdefault(r["day"], {"date": r["day"], "focus_seconds": 0, "sessions": 0})
        day_map[r["day"]]["focus_seconds"] += seconds
        day_map[r["day"]]["sessions"] += 1
        subject = (r["task_subject"] or r["subject"] or r["task_type"] or "Uncategorised").strip() or "Uncategorised"
        subject_map[subject] = subject_map.get(subject, 0) + seconds
    day_rows = conn.execute(
        "SELECT day,COUNT(*) total,SUM(duration) planned_minutes,SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) skipped FROM tasks WHERE day BETWEEN ? AND ? GROUP BY day ORDER BY day",
        (start, end),
    ).fetchall()
    for r in day_rows:
        item = day_map.setdefault(r["day"], {"date": r["day"], "focus_seconds": 0, "sessions": 0})
        item.update({"total_tasks": r["total"], "planned_minutes": r["planned_minutes"] or 0,
                     "completed": r["completed"] or 0, "skipped": r["skipped"] or 0})
    ordered_days = []
    cursor = date.fromisoformat(start)
    final = date.fromisoformat(end)
    while cursor <= final:
        key = cursor.isoformat()
        ordered_days.append(day_map.get(key, {"date": key, "focus_seconds": 0, "sessions": 0,
                                               "planned_minutes": 0, "total_tasks": 0, "completed": 0, "skipped": 0}))
        cursor += timedelta(days=1)
    today = date_today()
    completion_map = conn.execute(
        "SELECT day,COUNT(*) total,SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) skipped FROM tasks WHERE day<=? GROUP BY day HAVING total>0 ORDER BY day DESC",
        (today,),
    ).fetchall()
    streak = 0
    cursor = date.fromisoformat(today)
    complete_days = {r["day"] for r in completion_map if r["total"] == r["completed"] and r["skipped"] == 0}
    if cursor.isoformat() not in complete_days:
        cursor -= timedelta(days=1)
    while cursor.isoformat() in complete_days:
        streak += 1
        cursor -= timedelta(days=1)
    planned_minutes = sum(int(r["planned_minutes"] or 0) for r in day_rows)
    qstats = conn.execute("SELECT COALESCE(SUM(questions_done),0) total_done,COALESCE(SUM(CASE WHEN questions_correct IS NOT NULL THEN questions_done ELSE 0 END),0) scored_done,COALESCE(SUM(questions_correct),0) correct FROM tasks WHERE day BETWEEN ? AND ?", (start, end)).fetchone()
    question_accuracy = round(qstats["correct"] / qstats["scored_done"] * 100, 1) if qstats["scored_done"] else None
    task_rollup = conn.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN origin_day<>'' THEN 1 ELSE 0 END) AS carried, "
        "SUM(CASE WHEN deadline<>'' AND deadline<? AND status NOT IN ('completed','skipped') THEN 1 ELSE 0 END) AS overdue "
        "FROM tasks WHERE day BETWEEN ? AND ?", (date_today(), start, end)
    ).fetchone()
    return {
        "range": {"start": start, "end": end},
        "questions_done": qstats["total_done"],
        "questions_correct": qstats["correct"],
        "question_accuracy": question_accuracy,
        "focus_seconds": focus_total,
        "focus_minutes": round(focus_total / 60, 1),
        "planned_minutes": planned_minutes,
        "actual_vs_planned_percent": round(focus_total / (planned_minutes * 60) * 100, 1) if planned_minutes else None,
        "sessions": len(rows),
        "subject_breakdown": [{"subject": k, "focus_seconds": v, "focus_minutes": round(v / 60, 1)} for k, v in sorted(subject_map.items(), key=lambda x: x[1], reverse=True)],
        "days": ordered_days,
        "streak_days": streak,
        "task_completion_percent": round((task_rollup["completed"] or 0) / task_rollup["total"] * 100, 1) if task_rollup["total"] else None,
        "carried_over_tasks": int(task_rollup["carried"] or 0),
        "overdue_open_tasks": int(task_rollup["overdue"] or 0),
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> PlainTextResponse:
    return PlainTextResponse("", status_code=204)


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.post("/api/desktop/quit", include_in_schema=False)
def desktop_quit(request: Request) -> dict[str, bool]:
    if os.getenv("TICKOVER_DESKTOP") != "1":
        raise HTTPException(404, "This endpoint is available only in desktop builds.")
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "Desktop shutdown is restricted to the local computer.")
    origin = request.headers.get("origin", "")
    if origin and origin not in {f"http://127.0.0.1:{os.getenv('PORT', '8765')}", f"http://localhost:{os.getenv('PORT', '8765')}"}:
        raise HTTPException(403, "Shutdown request origin was not local.")
    server = getattr(app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(503, "Desktop server handle is unavailable.")
    threading.Timer(0.35, setattr, args=(server, "should_exit", True)).start()
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": APP_VERSION, "date": date_today(), "timezone": str(TZ),
            "desktop_mode": os.getenv("TICKOVER_DESKTOP") == "1"}


# Browser/UI routes: intended to be reached through the private Tailscale Serve URL.
@app.get("/api/v1/ui/day")
def ui_day(day: str = Query(default_factory=date_today, alias="date")) -> dict[str, Any]:
    if day == date_today():
        auto_rollover_for_day(day)
    process_expired_timers_once(day_filter=day)
    with db() as conn:
        return day_payload(conn, day)


@app.post("/api/v1/ui/tasks")
def ui_create_task(day: str, item: TaskInput) -> dict[str, Any]:
    with db() as conn:
        ensure_day(conn, day)
        top = conn.execute("SELECT COALESCE(MAX(sort_order),-1) FROM tasks WHERE day=?", (day,)).fetchone()[0]
        item.sort_order = int(top) + 1
        task_id = task_insert(conn, day, item)
        return {"task": task_row(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())}


@app.patch("/api/v1/ui/tasks/{task_id}")
def ui_patch_task(task_id: str, patch: TaskPatch) -> dict[str, Any]:
    values = patch.model_dump(exclude_unset=True)
    if "status" in values and values["status"] not in {"pending", "in_progress", "completed", "skipped"}:
        raise HTTPException(400, "Invalid task status.")
    if "label" in values:
        values["label"] = values["label"].strip()
        if not values["label"]:
            raise HTTPException(400, "Task label cannot be blank.")
    if "items" in values:
        values["items_json"] = json.dumps(values.pop("items"))
    if values.get("deadline"):
        try:
            date.fromisoformat(values["deadline"])
        except ValueError as exc:
            raise HTTPException(400, "deadline must use YYYY-MM-DD format.") from exc
    if "locked" in values:
        values["locked"] = int(bool(values["locked"]))
    with db() as check_conn:
        existing = check_conn.execute("SELECT questions_done,questions_correct FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Task not found.")
        next_done = values.get("questions_done", existing["questions_done"])
        next_correct = values.get("questions_correct", existing["questions_correct"])
        if next_correct is not None and int(next_correct) > int(next_done):
            raise HTTPException(400, "Correct answers cannot exceed questions done.")
    if not values:
        return {"ok": True}
    values["updated_at"] = iso_now()
    clause = ",".join(f"{key}=?" for key in values)
    with db() as conn:
        cur = conn.execute(f"UPDATE tasks SET {clause} WHERE id=?", [*values.values(), task_id])
        if not cur.rowcount:
            raise HTTPException(404, "Task not found.")
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        bump_revision(conn, task["day"])
        return {"task": task_row(task)}


def delete_task_record(task_id: str, actor: str) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT day,label FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found.")
        timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (row["day"],)).fetchone()
        timer_state = json_load(timer_row["state_json"], {}) if timer_row else {}
        if timer_state.get("session_id") and timer_state.get("task_id") == task_id:
            raise HTTPException(409, "This task has an active timer session. Pause or complete the session before deleting it.")
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        bump_revision(conn, row["day"])
        log_activity(conn, actor, "task.deleted", row["day"], {"task_id": task_id, "label": row["label"]})
        return {"ok": True}


@app.delete("/api/v1/ui/tasks/{task_id}")
def ui_delete_task(task_id: str) -> dict[str, bool]:
    return delete_task_record(task_id, "user")


class ReorderInput(BaseModel):
    ids: list[str] = Field(max_length=500)
    override_locked: bool = False

def apply_order(day: str, ids: list[str], *, allow_locked_override: bool) -> dict[str, bool]:
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "Duplicate task IDs are not allowed.")
    with db() as conn:
        rows = conn.execute("SELECT id,sort_order,locked,label FROM tasks WHERE day=?", (day,)).fetchall()
        existing = {r["id"] for r in rows}
        if set(ids) != existing:
            raise HTTPException(409, "The reorder list must contain every task for this day exactly once. Refresh and try again.")
        current_order = sorted(rows, key=lambda row: (int(row["sort_order"]), row["id"]))
        old_positions = {row["id"]: index for index, row in enumerate(current_order)}
        positions = {task_id: index for index, task_id in enumerate(ids)}
        protected_moves = [r["label"] for r in rows if bool(r["locked"]) and positions[r["id"]] != old_positions[r["id"]]]
        if protected_moves and not allow_locked_override:
            raise HTTPException(409, "Reordering would move protected tasks: " + ", ".join(protected_moves) + ". Set override_locked=true only with explicit user approval.")
        for i, tid in enumerate(ids):
            conn.execute("UPDATE tasks SET sort_order=?,updated_at=? WHERE id=? AND day=?", (i, iso_now(), tid, day))
        bump_revision(conn, day)
    return {"ok": True}


@app.post("/api/v1/ui/tasks/reorder")
def ui_reorder_tasks(day: str, body: ReorderInput) -> dict[str, bool]:
    # A drag-and-drop action made directly in the UI is explicit user intent.
    return apply_order(day, body.ids, allow_locked_override=True)

@app.get("/api/v1/ui/agent-token")
def ui_token_status() -> dict[str, Any]:
    token = current_token()
    return {"enabled": bool(token), "from_env": bool(AGENT_API_TOKEN), "hint": token[-4:] if token else ""}


@app.post("/api/v1/ui/agent-token")
def ui_token_create() -> dict[str, str]:
    if AGENT_API_TOKEN:
        raise HTTPException(409, "The token is set in the .env file on this server. Change it there.")
    token = secrets.token_urlsafe(32)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return {"token": token}


@app.delete("/api/v1/ui/agent-token")
def ui_token_revoke() -> dict[str, bool]:
    if AGENT_API_TOKEN:
        raise HTTPException(409, "The token is set in the .env file on this server. Remove it there.")
    TOKEN_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/v1/ui/backup")
def ui_backup() -> FileResponse:
    fd, name = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(name)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return FileResponse(name, filename=f"tickover-backup-{date_today()}.db", background=BackgroundTask(os.remove, name))


@app.post("/api/v1/ui/restore")
async def ui_restore(file: UploadFile = File(...)) -> dict[str, Any]:
    """Restore a Tickover SQLite backup, first preserving the current database."""
    max_bytes = 50 * 1024 * 1024
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, upload_name = tempfile.mkstemp(suffix=".upload.db", dir=DB_PATH.parent)
    os.close(fd)
    restore_name = str(DB_PATH) + ".restore.tmp"
    try:
        total = 0
        with open(upload_name, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Backup file is larger than 50 MB.")
                output.write(chunk)
        if total == 0:
            raise HTTPException(400, "The uploaded backup is empty.")
        source = None
        destination = None
        try:
            source = sqlite3.connect(f"file:{upload_name}?mode=ro", uri=True)
            check = source.execute("PRAGMA quick_check").fetchone()
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"study_days", "tasks", "timers", "sessions", "settings"}
            if not check or check[0] != "ok" or not required.issubset(tables):
                raise HTTPException(400, "This is not a valid Tickover database backup.")
            destination = sqlite3.connect(restore_name)
            source.backup(destination)
        except sqlite3.DatabaseError as exc:
            raise HTTPException(400, "The uploaded file is not a readable SQLite backup.") from exc
        finally:
            if source is not None:
                source.close()
            if destination is not None:
                destination.close()

        current_backup = None
        with DB_LOCK:
            if DB_PATH.exists():
                backup_dir = DATA_DIR / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                current_backup = backup_dir / f"pre-restore-{local_now():%Y%m%d-%H%M%S-%f}.db"
                current = sqlite3.connect(DB_PATH)
                backup = sqlite3.connect(current_backup)
                try:
                    current.backup(backup)
                finally:
                    backup.close()
                    current.close()
            for suffix in ("-wal", "-shm"):
                Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
            os.replace(restore_name, DB_PATH)
            try:
                init_db()
                load_saved_timezone()
            except Exception:
                if current_backup and current_backup.exists():
                    os.replace(current_backup, DB_PATH)
                    init_db()
                    load_saved_timezone()
                raise
        log_message = {"filename": (file.filename or "backup.db")[:120], "bytes": total}
        with db() as conn:
            log_activity(conn, "user", "backup.restored", date_today(), log_message)
        return {"ok": True, "bytes": total, "safety_backup_created": bool(current_backup)}
    finally:
        await file.close()
        Path(upload_name).unlink(missing_ok=True)
        Path(restore_name).unlink(missing_ok=True)


@app.get("/api/v1/ui/settings")
def ui_get_settings() -> dict[str, Any]:
    with db() as conn:
        return get_settings(conn)


@app.put("/api/v1/ui/settings")
def ui_save_settings(settings: SettingsInput) -> dict[str, Any]:
    global TZ
    values = settings.model_dump(exclude_unset=True)
    if values.get("timezone"):
        try:
            zone = ZoneInfo(values["timezone"])
        except Exception:
            raise HTTPException(400, "Unknown timezone.")
        if not os.getenv("APP_TIMEZONE"):
            TZ = zone
    with db() as conn:
        for key, value in values.items():
            conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, json.dumps(value), iso_now()))
        return get_settings(conn)


def process_expired_timers_once(day_filter: Optional[str] = None) -> int:
    """Reconcile elapsed timers and perform configured automatic phase advancement."""
    now = now_ts()
    # The background worker only advances today's timer. Historical timers can
    # remain intentionally open for review and must never auto-complete on app
    # startup merely because their old deadline is in the past. A user viewing
    # a different date can still reconcile that date through the UI/agent read.
    if day_filter is None:
        day_filter = date_today()
    with db() as conn:
        rows = conn.execute("SELECT day,state_json FROM timers WHERE day=?", (day_filter,)).fetchall()
        expired_days = [r["day"] for r in rows
                        if (state := json_load(r["state_json"], {})).get("running")
                        and state.get("ends_at") is not None and float(state["ends_at"]) <= now]
    advanced = 0
    for day in expired_days:
        # Keep the process-wide re-entrant DB lock across the check + complete +
        # start sequence. The timer monitor and HTTP requests can race to notice
        # the same expiry; without this guard they could complete two phases.
        with DB_LOCK:
            with db() as conn:
                state = get_timer(conn, day)  # reconciles to an expired, stopped state
                should_advance = bool(state.get("expired") and get_settings(conn).get("auto_advance", False))
            if should_advance:
                ui_timer_action("complete", {"date": day})
                ui_timer_action("start", {"date": day})
                with db() as conn:
                    log_activity(conn, "system", "timer.auto_advanced", day, {"next_mode": get_timer(conn, day).get("mode")})
                advanced += 1
    return advanced


def timer_monitor_loop(stop_event: threading.Event) -> None:
    """Keep timer state current even when the browser tab is closed."""
    while not stop_event.wait(1.0):
        try:
            process_expired_timers_once()
        except Exception:
            # A transient SQLite or shutdown error should not kill the monitor.
            continue


@app.post("/api/v1/ui/timer/{action}")
def ui_timer_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    day = payload.get("date", date_today())
    now = now_ts()
    with db() as conn:
        ensure_day(conn, day)
        state = get_timer(conn, day)
        task = find_task(conn, state.get("task_id"))
        if action in ("skip", "next", "reset", "select", "mode", "complete", "reset-day"):
            state["expired"] = False
        if action == "start":
            if not state.get("running") and not state.get("expired"):
                if state.get("mode") == "work" and not state.get("session_id") and task:
                    sid = new_id()
                    conn.execute("INSERT INTO sessions(id,task_id,day,kind,label,subject,started_at,planned_seconds,focus_seconds,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                 (sid, task["id"], day, "work", task["label"], task["subject"], now, int(state["remaining_seconds"]), 0, "in_progress", iso_now()))
                    state["session_id"] = sid
                    state["focus_seconds"] = 0
                    if task["status"] == "pending":
                        conn.execute("UPDATE tasks SET status='in_progress',updated_at=? WHERE id=?", (iso_now(), task["id"]))
                        bump_revision(conn, day)
                state["running"] = True
                state["last_resumed_at"] = now if state.get("mode") == "work" else None
                state["ends_at"] = now + int(state["remaining_seconds"])
                record_event(state, "started")
        elif action == "pause":
            if state.get("running"):
                state["remaining_seconds"] = max(0, int(float(state["ends_at"]) - now))
                close_running_segment(state, now)
                state["running"] = False
                state["ends_at"] = None
                record_event(state, "paused")
                if state.get("session_id"):
                    conn.execute("UPDATE sessions SET focus_seconds=? WHERE id=?", (int(state["focus_seconds"]), state["session_id"]))
        elif action == "skip":
            if state.get("mode") == "work":
                finish_session(conn, state, "skipped", now)
                if task:
                    conn.execute("UPDATE tasks SET status='skipped',updated_at=? WHERE id=?", (iso_now(), task["id"]))
                    bump_revision(conn, day)
            nxt = next_task(conn, day, state.get("task_id"))
            duration = int(nxt["duration"]) if nxt else int(get_settings(conn).get("pomodoro_min", 25))
            state.update({"task_id": nxt["id"] if nxt else None, "mode": "work", "running": False,
                          "remaining_seconds": duration * 60, "planned_seconds": duration * 60,
                          "ends_at": None, "session_id": None, "focus_seconds": 0, "last_resumed_at": None})
            record_event(state, "skipped")
        elif action == "next":
            if state.get("mode") == "work" and state.get("session_id"):
                finish_session(conn, state, "interrupted", now)
                if task and task["status"] == "in_progress":
                    conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE id=?", (iso_now(), task["id"]))
                    bump_revision(conn, day)
            nxt = next_task(conn, day, state.get("task_id"))
            duration = int(nxt["duration"]) if nxt else int(get_settings(conn).get("pomodoro_min", 25))
            state.update({"task_id": nxt["id"] if nxt else None, "mode": "work", "running": False,
                          "remaining_seconds": duration * 60, "planned_seconds": duration * 60,
                          "ends_at": None, "session_id": None, "focus_seconds": 0, "last_resumed_at": None})
            record_event(state, "next")
        elif action == "reset":
            if state.get("session_id"):
                finish_session(conn, state, "interrupted", now)
                if task and task["status"] == "in_progress":
                    conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE id=?", (iso_now(), task["id"]))
                    bump_revision(conn, day)
            duration = int(task["duration"]) if task and state.get("mode") == "work" else int(state.get("planned_seconds", 1500)) // 60
            state.update({"running": False, "remaining_seconds": duration * 60, "planned_seconds": duration * 60,
                          "ends_at": None, "session_id": None, "focus_seconds": 0, "last_resumed_at": None})
            record_event(state, "reset")
        elif action == "select":
            task_id = payload.get("task_id")
            selected = find_task(conn, task_id)
            if not selected or selected["day"] != day:
                raise HTTPException(404, "Task not found for this date.")
            if state.get("session_id"):
                finish_session(conn, state, "interrupted", now)
                old = find_task(conn, state.get("task_id"))
                if old and old["status"] == "in_progress":
                    conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE id=?", (iso_now(), old["id"]))
                    bump_revision(conn, day)
            duration = int(selected["duration"])
            state.update({"task_id": selected["id"], "mode": "work", "running": False,
                          "remaining_seconds": duration * 60, "planned_seconds": duration * 60,
                          "ends_at": None, "session_id": None, "focus_seconds": 0, "last_resumed_at": None})
            record_event(state, "selected")
        elif action == "complete":
            expect = payload.get("expect_mode")
            if expect and expect != state.get("mode"):
                pass  # stale or duplicate request: nothing to complete
            elif state.get("mode") == "work":
                complete_work(conn, state, now)
            else:
                complete_break(conn, state, now)
        elif action == "mode":
            target = payload.get("mode")
            if target not in ("work", "short", "long"):
                raise HTTPException(400, "mode must be work, short or long.")
            if state.get("session_id"):
                finish_session(conn, state, "interrupted", now)
                if task and task["status"] == "in_progress":
                    conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE id=?", (iso_now(), task["id"]))
                    bump_revision(conn, day)
            cfg = get_settings(conn)
            if target == "work":
                minutes = int(task["duration"]) if task else int(cfg.get("pomodoro_min", 25))
                state.update({"mode": "work", "break_kind": None})
            else:
                minutes = int(cfg.get("short_break_min", 5) if target == "short" else cfg.get("long_break_min", 15))
                state.update({"mode": "break", "break_kind": target})
            state.update({"running": False, "remaining_seconds": minutes * 60, "planned_seconds": minutes * 60,
                          "ends_at": None, "session_id": None, "focus_seconds": 0, "last_resumed_at": None})
            record_event(state, "mode")
        elif action == "reset-day":
            conn.execute("UPDATE tasks SET status='pending',updated_at=? WHERE day=?", (iso_now(), day))
            bump_revision(conn, day)
            conn.execute("DELETE FROM sessions WHERE day=?", (day,))
            first = conn.execute("SELECT * FROM tasks WHERE day=? ORDER BY sort_order LIMIT 1", (day,)).fetchone()
            state = initial_timer_state(day, first, get_settings(conn))
            record_event(state, "day_reset")
        else:
            raise HTTPException(404, "Unknown timer action.")
        save_timer(conn, state)
        return {"timer": timer_payload(conn, state), "day": day_payload(conn, day)["summary"]}


@app.get("/api/v1/ui/analytics")
def ui_analytics(days: int = Query(default=7, ge=1, le=90), end: str = Query(default_factory=date_today)) -> dict[str, Any]:
    try:
        end_date = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "End date must use YYYY-MM-DD.")
    start = (end_date - timedelta(days=days - 1)).isoformat()
    with db() as conn:
        return analytics_data(conn, start, end)


@app.get("/api/v1/ui/export/{day}")
def ui_export_notes(day: str, format: str = Query(default="txt", pattern="^(txt|md)$")) -> PlainTextResponse:
    with db() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE day=? ORDER BY sort_order", (day,)).fetchall()
        d = conn.execute("SELECT * FROM study_days WHERE day=?", (day,)).fetchone()
    phase = d["phase"] if d else "Study day"
    if format == "md":
        lines = [f"# Study notes — {day}", "", f"**Phase:** {phase}", ""]
    else:
        lines = [f"STUDY NOTES — {day}", f"Phase: {phase}", "=" * 60, ""]
    for row in rows:
        if format == "md":
            lines += [f"## {row['label']}", f"- Subject: {row['subject'] or 'Uncategorised'}", f"- Status: {row['status']}"]
        else:
            lines += [row["label"], f"Subject: {row['subject'] or 'Uncategorised'}", f"Status: {row['status']}", "-" * 40]
        if row["questions_done"]:
            result = f"Questions: {row['questions_done']}"
            if row["questions_correct"] is not None:
                result += f" ({row['questions_correct']} correct)"
            lines.append(f"- {result}" if format == "md" else result)
        if row["notes"].strip():
            lines += ["", row["notes"].strip()]
        lines.append("")
    extension = "md" if format == "md" else "txt"
    media_type = "text/markdown" if format == "md" else "text/plain"
    filename = f"study-notes-{day}.{extension}"
    return PlainTextResponse("\n".join(lines), media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/v1/ui/capacity")
def ui_capacity(day: str = Query(default_factory=date_today, alias="date")) -> dict[str, Any]:
    with db() as conn:
        ensure_day(conn, day)
        return capacity_summary(conn, day)


@app.post("/api/v1/ui/rollover/preview")
def ui_rollover_preview(request: RolloverRequest) -> dict[str, Any]:
    with db() as conn:
        proposal = build_rollover_proposal(conn, request)
        log_activity(conn, "user", "rollover.previewed", request.target_day, {
            "proposal_id": proposal["proposal_id"], "source_day": request.source_day,
            "moving_count": len(proposal["moving"]), "deferred_count": len(proposal["deferred"]),
        })
        return proposal


@app.post("/api/v1/ui/rollover/apply")
def ui_rollover_apply(request: ApplyProposalInput) -> dict[str, Any]:
    with db() as conn:
        return apply_rollover_proposal(conn, request.proposal_id, request.expected_target_revision, "user")


@app.post("/api/v1/ui/rollover/undo")
def ui_rollover_undo(request: UndoProposalInput) -> dict[str, Any]:
    with db() as conn:
        return undo_rollover_proposal(conn, request.proposal_id, "user")


@app.get("/api/v1/ui/activity")
def ui_activity(limit: int = Query(default=30, ge=1, le=200), day: str = "") -> dict[str, Any]:
    with db() as conn:
        if day:
            try:
                date.fromisoformat(day)
            except ValueError:
                raise HTTPException(400, "Date must use YYYY-MM-DD format.")
            rows = conn.execute("SELECT * FROM activity_log WHERE target_day=? ORDER BY id DESC LIMIT ?", (day, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {"events": [{"id": r["id"], "event_at": r["event_at"], "actor": r["actor"], "action": r["action"], "target_day": r["target_day"], "details": json_load(r["details_json"], {})} for r in rows]}


def normalize_agent_plan(
    day: str,
    items: list[TaskInput],
    existing_rows: list[sqlite3.Row],
    *,
    replace_existing: bool,
) -> list[TaskInput]:
    """Normalize agent plans while preserving omitted metadata for matched tasks.

    Whole-plan replacements take the incoming list order as authoritative.
    Partial upserts preserve existing task order and append new tasks unless an
    explicit sort_order is provided.
    """
    existing_by_external = {row["external_id"]: row for row in existing_rows}
    max_order = max((int(row["sort_order"]) for row in existing_rows), default=-1)
    seen: set[str] = set()
    preserved_fields = {
        "type": "type",
        "subject": "subject",
        "duration": "duration",
        "priority": "priority",
        "deadline": "deadline",
        "locked": "locked",
        "items": "items_json",
        "total_runtime": "total_runtime",
        "real_min": "real_min",
        "notes": "notes",
        "questions_done": "questions_done",
        "questions_correct": "questions_correct",
    }
    for index, item in enumerate(items):
        external_id = item.external_id.strip()
        if not external_id:
            canonical = f"{day}|{item.type}|{item.subject}|{item.label}|{index}"
            external_id = "agent-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
        if external_id in seen:
            raise HTTPException(422, f"Duplicate external_id in plan: {external_id}")
        seen.add(external_id)
        item.external_id = external_id
        previous = existing_by_external.get(external_id)
        if previous:
            for field, column in preserved_fields.items():
                if field not in item.model_fields_set:
                    value = previous[column]
                    if field == "items":
                        value = json_load(value, [])
                    elif field == "locked":
                        value = bool(value)
                    setattr(item, field, value)
        if "sort_order" not in item.model_fields_set:
            if previous and not replace_existing:
                item.sort_order = int(previous["sort_order"])
            elif previous and replace_existing:
                item.sort_order = index
            else:
                item.sort_order = max_order + index + 1 if not replace_existing else index
    return items


# Agent routes: bearer token required. All writes are idempotent when external_id is supplied.
@app.get("/api/v1/agent/plan/{day}", dependencies=[Depends(require_agent)])
def agent_get_plan(day: str) -> dict[str, Any]:
    if day == date_today():
        auto_rollover_for_day(day)
    process_expired_timers_once(day_filter=day)
    with db() as conn:
        return day_payload(conn, day)


@app.post("/api/v1/agent/plan/{day}/preview", dependencies=[Depends(require_agent)])
def agent_preview_plan(day: str, plan: PlanInput) -> dict[str, Any]:
    """Return a read-only diff and capacity impact before an agent changes a day."""
    with db() as conn:
        ensure_day(conn, day)
        day_row = conn.execute("SELECT revision FROM study_days WHERE day=?", (day,)).fetchone()
        revision = int(day_row["revision"] if day_row else 0)
        if plan.expected_revision is not None and revision != plan.expected_revision:
            raise HTTPException(409, "Schedule revision conflict. Read the latest plan before previewing changes.")
        existing_rows = conn.execute("SELECT * FROM tasks WHERE day=? ORDER BY sort_order,id", (day,)).fetchall()
        normalized = normalize_agent_plan(day, plan.tasks, existing_rows, replace_existing=plan.replace_existing)
        existing_by_external = {row["external_id"]: row for row in existing_rows}
        proposed_by_external = {item.external_id: item for item in normalized}
        existing_order = sorted(existing_rows, key=lambda row: (int(row["sort_order"]), row["id"]))
        existing_rank = {row["external_id"]: index for index, row in enumerate(existing_order)}
        proposed_by_order = {item.external_id: item for item in normalized}
        if plan.replace_existing:
            proposed_rows = list(normalized)
        else:
            proposed_rows = [proposed_by_order.get(row["external_id"]) or row for row in existing_order]
            proposed_rows.extend(item for item in normalized if item.external_id not in existing_by_external)
        proposed_order = sorted(
            enumerate(proposed_rows),
            key=lambda pair: (int(pair[1]["sort_order"] if isinstance(pair[1], sqlite3.Row) else pair[1].sort_order), pair[0]),
        )
        proposed_rank = {
            (item["external_id"] if isinstance(item, sqlite3.Row) else item.external_id): index
            for index, (_, item) in enumerate(proposed_order)
        }
        additions, updates = [], []
        compare_fields = ("type", "subject", "label", "duration", "items", "priority", "deadline", "locked", "notes", "questions_done", "questions_correct")
        for item in normalized:
            previous = existing_by_external.get(item.external_id)
            proposed = item.model_dump()
            if not previous:
                additions.append(proposed)
                continue
            before = task_row(previous)
            changes = {field: {"from": before.get(field), "to": proposed.get(field)}
                       for field in compare_fields if before.get(field) != proposed.get(field)}
            old_position = existing_rank.get(item.external_id)
            new_position = proposed_rank.get(item.external_id)
            if old_position is not None and new_position != old_position:
                changes["position"] = {"from": old_position, "to": new_position}
            if changes:
                updates.append({"id": before["id"], "external_id": item.external_id, "label": item.label, "changes": changes})
        removals = []
        if plan.replace_existing:
            for row in existing_rows:
                if row["external_id"] not in proposed_by_external:
                    removals.append(task_row(row))
        locked_conflicts = []
        for row in existing_rows:
            if not bool(row["locked"]):
                continue
            incoming = proposed_by_external.get(row["external_id"])
            reasons = []
            if incoming is None and plan.replace_existing:
                reasons.append("would be removed")
            elif incoming is not None:
                if proposed_rank.get(row["external_id"]) != existing_rank.get(row["external_id"]):
                    reasons.append("would be moved")
                if not incoming.locked:
                    reasons.append("would be unlocked")
            if reasons:
                locked_conflicts.append({"id": row["id"], "label": row["label"], "reasons": reasons})
        timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
        timer_state = json_load(timer_row["state_json"], {}) if timer_row else {}
        active_conflicts = []
        active_task_id = timer_state.get("task_id") if timer_state.get("session_id") else None
        if active_task_id and plan.replace_existing:
            active_row = next((row for row in existing_rows if row["id"] == active_task_id), None)
            if active_row and active_row["external_id"] not in proposed_by_external:
                active_conflicts.append({"id": active_row["id"], "label": active_row["label"], "reason": "currently has an active timer session"})
        current_capacity = capacity_summary(conn, day)
        effective = current_capacity["effective_capacity_minutes"]
        studied = current_capacity["studied_minutes"]
        after_planned = current_capacity["remaining_planned_minutes"]
        if plan.replace_existing:
            after_planned = sum(int(item.duration) for item in normalized
                                if existing_by_external.get(item.external_id) is None
                                or existing_by_external[item.external_id]["status"] not in {"completed", "skipped"})
        else:
            for item in normalized:
                previous = existing_by_external.get(item.external_id)
                if previous is None:
                    after_planned += int(item.duration)
                elif previous["status"] not in {"completed", "skipped"}:
                    after_planned += int(item.duration) - int(previous["duration"])
        after_used = studied + after_planned
        capacity_after = {
            "enabled": current_capacity["enabled"],
            "planned_minutes": after_used,
            "remaining_minutes": max(0, effective - after_used),
            "over_capacity_minutes": max(0, after_used - effective) if current_capacity["enabled"] else 0,
            "is_over_capacity": current_capacity["enabled"] and after_used > effective,
            "effective_capacity_minutes": effective,
            "daily_capacity_minutes": effective,
        }
        response = {
            "date": day, "expected_revision": revision, "replace_existing": plan.replace_existing,
            "override_locked": plan.override_locked, "additions": additions, "updates": updates,
            "removals": removals, "locked_conflicts": locked_conflicts,
            "active_conflicts": active_conflicts, "capacity_before": current_capacity,
            "capacity_after": capacity_after,
            "ready_to_apply": (not active_conflicts and (plan.override_locked or not locked_conflicts)),
        }
        log_activity(conn, "agent", "plan.previewed", day, {
            "expected_revision": revision, "additions": len(additions), "updates": len(updates),
            "removals": len(removals), "locked_conflicts": len(locked_conflicts),
        })
        return response


@app.put("/api/v1/agent/plan/{day}", dependencies=[Depends(require_agent)])
def agent_put_plan(day: str, plan: PlanInput) -> dict[str, Any]:
    with db() as conn:
        ensure_day(conn, day)
        current_day = conn.execute("SELECT * FROM study_days WHERE day=?", (day,)).fetchone()
        if plan.expected_revision is not None and int(current_day["revision"]) != plan.expected_revision:
            raise HTTPException(409, "Schedule revision conflict. Read the latest plan before writing changes.")
        existing_rows = conn.execute("SELECT * FROM tasks WHERE day=? ORDER BY sort_order,id", (day,)).fetchall()
        normalize_agent_plan(day, plan.tasks, existing_rows, replace_existing=plan.replace_existing)
        incoming_ids = {item.external_id for item in plan.tasks}
        removed_rows = [row for row in existing_rows if plan.replace_existing and row["external_id"] not in incoming_ids]
        existing_order = sorted(existing_rows, key=lambda row: (int(row["sort_order"]), row["id"]))
        existing_rank = {row["external_id"]: index for index, row in enumerate(existing_order)}
        incoming_by_external = {item.external_id: item for item in plan.tasks}
        if plan.replace_existing:
            proposed_rows = list(plan.tasks)
        else:
            proposed_rows = [incoming_by_external.get(row["external_id"]) or row for row in existing_order]
            proposed_rows.extend(item for item in plan.tasks if item.external_id not in existing_rank)
        proposed_order = sorted(
            enumerate(proposed_rows),
            key=lambda pair: (int(pair[1]["sort_order"] if isinstance(pair[1], sqlite3.Row) else pair[1].sort_order), pair[0]),
        )
        proposed_rank = {
            (item["external_id"] if isinstance(item, sqlite3.Row) else item.external_id): index
            for index, (_, item) in enumerate(proposed_order)
        }
        locked_conflicts = []
        for row in existing_rows:
            if not bool(row["locked"]):
                continue
            incoming = next((item for item in plan.tasks if item.external_id == row["external_id"]), None)
            is_removed = incoming is None and plan.replace_existing
            is_moved = incoming is not None and proposed_rank.get(row["external_id"]) != existing_rank.get(row["external_id"])
            is_unlocked = incoming is not None and not incoming.locked
            if is_removed or is_moved or is_unlocked:
                locked_conflicts.append(row["label"])
        if locked_conflicts and not plan.override_locked:
            raise HTTPException(409, "This plan would remove, move or unlock protected tasks: " + ", ".join(locked_conflicts) + ". Preview the plan and pass override_locked=true only with explicit user approval.")
        if plan.replace_existing:
            old_timer_row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
            old_state = json_load(old_timer_row["state_json"], {}) if old_timer_row else {}
            if old_state.get("session_id") and old_state.get("task_id") in {row["id"] for row in removed_rows}:
                raise HTTPException(409, "The replacement would remove the task with an active timer session. Pause or finish it first.")
            if removed_rows:
                conn.executemany("DELETE FROM tasks WHERE id=?", [(row["id"],) for row in removed_rows])
        conn.execute("UPDATE study_days SET phase=?,target_date=?,q_target=?,qbank=?,metadata_json=?,updated_at=?,revision=revision+1 WHERE day=?",
                     (plan.phase if plan.phase is not None else current_day["phase"],
                      plan.target_date if plan.target_date is not None else current_day["target_date"],
                      plan.q_target if plan.q_target is not None else current_day["q_target"],
                      plan.qbank if plan.qbank is not None else current_day["qbank"],
                      json.dumps(plan.metadata if plan.metadata is not None else json_load(current_day["metadata_json"], {})),
                      iso_now(), day))
        for item in plan.tasks:
            task_insert(conn, day, item)
        if plan.replace_existing and removed_rows:
            removed_ids = {row["id"] for row in removed_rows}
            if old_state.get("task_id") in removed_ids and not old_state.get("session_id"):
                first = conn.execute("SELECT * FROM tasks WHERE day=? AND status NOT IN ('completed','skipped') ORDER BY sort_order,id LIMIT 1", (day,)).fetchone()
                save_timer(conn, initial_timer_state(day, first, get_settings(conn)))
        log_activity(conn, "agent", "plan.updated", day, {
            "task_count": len(plan.tasks), "replace_existing": plan.replace_existing,
            "override_locked": plan.override_locked, "removed_count": len(removed_rows),
            "expected_revision": plan.expected_revision,
        })
        return day_payload(conn, day)

@app.post("/api/v1/agent/tasks/{day}", dependencies=[Depends(require_agent)])
def agent_create_task(day: str, item: TaskInput) -> dict[str, Any]:
    with db() as conn:
        if not item.external_id:
            raise HTTPException(400, "external_id is required for idempotent agent task creation.")
        task_id = task_insert(conn, day, item)
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        log_activity(conn, "agent", "task.created", day, {"task_id": task_id, "label": task["label"], "external_id": item.external_id})
        return {"task": task_row(task)}


@app.patch("/api/v1/agent/tasks/{task_id}", dependencies=[Depends(require_agent)])
def agent_patch_task(task_id: str, patch: TaskPatch, override_locked: bool = Query(default=False)) -> dict[str, Any]:
    with db() as conn:
        existing = conn.execute("SELECT day,label,locked,sort_order FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "Task not found.")
    if bool(existing["locked"]) and not override_locked:
        if patch.sort_order is not None and patch.sort_order != int(existing["sort_order"]):
            raise HTTPException(409, "This task is protected. Pass override_locked=true only with explicit user approval.")
        if patch.locked is False:
            raise HTTPException(409, "This task is protected. Unlocking it requires explicit user approval (override_locked=true).")
    result = ui_patch_task(task_id, patch)
    with db() as conn:
        log_activity(conn, "agent", "task.updated", existing["day"], {"task_id": task_id, "label": existing["label"], "fields": list(patch.model_dump(exclude_unset=True).keys())})
    return result


@app.delete("/api/v1/agent/tasks/{task_id}", dependencies=[Depends(require_agent)])
def agent_delete_task(task_id: str, override_locked: bool = Query(default=False)) -> dict[str, bool]:
    with db() as conn:
        existing = conn.execute("SELECT day,label,locked FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "Task not found.")
    if bool(existing["locked"]) and not override_locked:
        raise HTTPException(409, "This task is protected. Pass override_locked=true only with explicit user approval.")
    return delete_task_record(task_id, "agent")


@app.post("/api/v1/agent/tasks/{day}/reorder", dependencies=[Depends(require_agent)])
def agent_reorder_tasks(day: str, body: ReorderInput) -> dict[str, bool]:
    result = apply_order(day, body.ids, allow_locked_override=body.override_locked)
    with db() as conn:
        log_activity(conn, "agent", "tasks.reordered", day, {"count": len(body.ids)})
    return result


@app.post("/api/v1/agent/rollover/preview", dependencies=[Depends(require_agent)])
def agent_rollover_preview(request: RolloverRequest) -> dict[str, Any]:
    with db() as conn:
        proposal = build_rollover_proposal(conn, request)
        log_activity(conn, "agent", "rollover.previewed", request.target_day, {
            "proposal_id": proposal["proposal_id"], "source_day": request.source_day,
            "moving_count": len(proposal["moving"]), "deferred_count": len(proposal["deferred"]),
        })
        return proposal


@app.post("/api/v1/agent/rollover/apply", dependencies=[Depends(require_agent)])
def agent_rollover_apply(request: ApplyProposalInput) -> dict[str, Any]:
    with db() as conn:
        return apply_rollover_proposal(conn, request.proposal_id, request.expected_target_revision, "agent")


@app.post("/api/v1/agent/rollover/undo", dependencies=[Depends(require_agent)])
def agent_rollover_undo(request: UndoProposalInput) -> dict[str, Any]:
    with db() as conn:
        return undo_rollover_proposal(conn, request.proposal_id, "agent")


@app.get("/api/v1/agent/activity", dependencies=[Depends(require_agent)])
def agent_activity(limit: int = Query(default=30, ge=1, le=200), day: str = "") -> dict[str, Any]:
    return ui_activity(limit, day)


@app.get("/api/v1/agent/analytics", dependencies=[Depends(require_agent)])
def agent_analytics(days: int = Query(default=30, ge=1, le=365), end: str = Query(default_factory=date_today)) -> dict[str, Any]:
    try:
        end_date = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "End date must use YYYY-MM-DD.")
    start = (end_date - timedelta(days=days - 1)).isoformat()
    with db() as conn:
        return analytics_data(conn, start, end)


@app.post("/api/v1/agent/timer/{action}", dependencies=[Depends(require_agent)])
def agent_timer_action(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    result = ui_timer_action(action, payload)
    target_day = (payload or {}).get("date", date_today())
    with db() as conn:
        log_activity(conn, "agent", f"timer.{action}", target_day, {})
    return result


if __name__ == "__main__":
    import uvicorn
    if os.getenv("OPEN_BROWSER"):
        threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{os.getenv('PORT', '8765')}"]).start()
    uvicorn.run("app:app", host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8765")), reload=False)
