# Tickover

**A local-first, schedule-aware Pomodoro timer with an API for AI agents.**

Tickover is for people whose day is planned in tasks, not just in timer sessions. Use it as a standalone study timer, connect an AI agent to create or adjust your plan, and carry unfinished work forward without losing its history.

It is free and open source. The default setup stores your database on your own computer; no account or hosted service is required.

## What makes it different

- **Schedule-aware Pomodoro:** plan days as editable blocks with subjects, types, sub-items and durations.
- **Priority and deadlines:** mark work critical, high, normal or low priority and optionally set a due date.
- **Protected blocks:** lock tasks so rollover won't move them unless explicitly overridden.
- **Rollover:** preview unfinished work from the previous 31 days, prioritize urgent work, and move it to today. With the optional daily study limit switched on, it only moves what fits.
- **Preview, apply and undo:** review proposed changes, reject stale proposals, and undo a rollover if the moved tasks haven't subsequently been changed or started.
- **Automatic or manual rollover:** choose automatic, approval-first or manual-only behavior in Settings.
- **Agent activity history:** see API operations and schedule changes made by your assistant. The local log is capped at 2,000 recent events.
- **Optimistic schedule revisions:** agents can include `expected_revision` to avoid overwriting a newer plan. Use a stable `external_id` for each task across plan refreshes so IDs, completion history, notes, sub-items and priorities remain attached to the same work.
- **Timer, breaks, audio and reports:** focus/short/long breaks, ticking sounds, chimes, optional music, focus mode, notifications and weekly analytics. Timer state lives on the server, so reopening the page restores the current session. When auto-advance is enabled, a lightweight backend monitor completes expired phases and starts the next phase even if the browser tab is closed, as long as the local app server remains running.

## Downloads

Download ready-to-run builds from the project's **GitHub Releases** page when a release is published:

- macOS Apple Silicon (`.dmg`)
- macOS Intel (`.dmg`)
- Windows x64 (`.exe`)

The desktop app runs a private local server, opens Tickover in your default browser, and provides a **Quit Tickover** action in Settings. Your database is stored outside the application bundle:

- macOS: `~/Library/Application Support/Tickover/data/`
- Windows: `%APPDATA%\Tickover\data\`

Unsigned builds may trigger operating-system security warnings. Do not distribute a build as signed or notarized unless it has actually been signed/notarized.

## Run from source

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # optional
python app.py
```

Open <http://127.0.0.1:8765>. On localhost or HTTPS, supported browsers can install Tickover as a PWA from the browser menu. The service worker caches only the app shell; API and database requests are never cached.

Alternatively, use the convenience scripts:

- macOS/Linux: `sh start.command`
- Windows: double-click `start.bat`

The scripts use `uv` to provision dependencies. Keep the terminal window open while running; closing it stops the server.

## Run with Docker

```bash
cp .env.example .env                 # optional
docker compose up -d --build
```

Open <http://127.0.0.1:8765>. On localhost or HTTPS, supported browsers can install Tickover as a PWA from the browser menu. The service worker caches only the app shell; API and database requests are never cached. The database persists in `./data/` through the Docker volume mount. The default port mapping binds to localhost only.

## Schedule behavior

The daily study limit is optional and off by default (Settings, then **Limit my daily study time**). When it is on, rollover and AI assistants will not plan more than your limit. Time used is the time you have already studied plus the time still planned, so finishing a block as planned never frees time. Finishing early frees only the minutes you saved, and running over uses up free time. Breaks are not counted. When it is off, nothing about it appears in the app.

Rollover considers unfinished, unlocked tasks from the previous 31 days. It sorts candidates by priority, then nearest deadline, then age and task order. It never moves completed or skipped tasks, and it excludes tasks with an active timer session. Tasks that do not fit remain on their original dates and can be reconsidered the next day.

The default is **Review before applying**. Automatic mode performs one rollover pass when today's plan is first requested. Manual mode disables automatic rollover; you can still use **Review rollover** yourself.

A rollover proposal is protected by a target-day revision. If the target plan changes after the preview, applying the old proposal returns HTTP 409 and you must preview again. Undo is available only while the moved tasks have not been edited or started after the move.

## Connect an AI agent

New to this? Read the beginner guide: [Connect Tickover to an AI assistant](docs/CONNECT-AN-AGENT.md).

1. Open **Settings → Connect an AI agent**.
2. Generate a token and copy the agent instructions.
3. Send it as `Authorization: Bearer YOUR_TOKEN` on each `/api/v1/agent/` request.
4. Use dates in `YYYY-MM-DD` format.

The API is documented interactively at `/docs` and `/redoc`. Use the plan preview endpoint before applying a schedule replacement; it is read-only and returns the revision to include when applying.

### Important endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/agent/plan/{day}` | Read a plan, tasks, timer, capacity and revision |
| `POST` | `/api/v1/agent/plan/{day}/preview` | Preview additions, edits, removals and capacity impact |
| `PUT` | `/api/v1/agent/plan/{day}` | Create or update a day's plan after approval |
| `POST` | `/api/v1/agent/tasks/{day}` | Add a task (requires `external_id`) |
| `PATCH` | `/api/v1/agent/tasks/{task_id}` | Update task metadata or status |
| `DELETE` | `/api/v1/agent/tasks/{task_id}` | Delete a task |
| `GET` | `/api/v1/agent/analytics?days=30` | Focus and completion analytics |
| `GET` | `/api/v1/agent/activity` | Read agent activity history |
| `POST` | `/api/v1/agent/rollover/preview` | Preview capacity-aware rollover |
| `POST` | `/api/v1/agent/rollover/apply` | Apply a proposal (send `proposal_id`) |
| `POST` | `/api/v1/agent/rollover/undo` | Undo an unchanged rollover |
| `POST` | `/api/v1/agent/timer/{action}` | Start, pause, reset, skip, next or complete |

### Example: preview and apply rollover

```bash
curl -X POST http://127.0.0.1:8765/api/v1/agent/rollover/preview \\
  -H "Authorization: Bearer $AGENT_API_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"source_day":"2026-11-06","target_day":"2026-11-07"}'
```

The response includes `proposal_id`, tasks to move, tasks deferred by capacity, locked tasks, active timer tasks and the target schedule revision. Apply only the proposal the user approved:

```json
{"proposal_id":"ID_FROM_PREVIEW","expected_target_revision":0}
```

A day response contains `day.revision`. Before making a whole-plan change, agents should call the preview endpoint, show its additions, updates, removals, capacity impact and any protected-task conflicts to the user, then apply with the returned `expected_revision` only after approval. The API returns HTTP 409 if someone changed the plan in the meantime. Matching tasks retain their IDs and history. `replace_existing: true` removes tasks omitted from the proposed plan. Protected tasks cannot be removed, unlocked or reordered by an agent unless it explicitly sets `override_locked: true` after user approval; a task with an active timer session cannot be removed.

### Task fields

`priority` accepts `critical`, `high`, `normal` or `low`. `deadline` is an optional `YYYY-MM-DD` date. `locked` is a boolean. The existing `duration` remains the task's estimated time in minutes.

## Install as a web app

When opened on localhost or an HTTPS origin in a supported browser, Tickover can be installed as a PWA. The service worker caches the static app shell only. All API requests remain network-only, and the backend/database must still be running.

## Security and hosting

**This release is a single-user, self-hosted application. It is not a multi-user hosted service.** One database and one agent token are shared by the running instance. Do not put one public instance on the internet for unrelated users: they would share schedule data and API access.

The default server binds to `127.0.0.1`. Keep it local, or place it behind a private network such as Tailscale. If you expose it to another device, set `APP_PASSWORD`, use HTTPS, and restrict network access. The agent API uses a bearer token; treat it like a password and never put it into client-side public code.

For a zero-cost public project, use:

- **GitHub** for source code and version history.
- **GitHub Releases** for `.dmg` and `.exe` downloads built by GitHub Actions.
- **GitHub Pages** for a static project website.

A permanently hosted multi-user version would require a separate architecture with user accounts, per-user data isolation, database persistence and stronger operational security. It is intentionally not part of this self-hosted release.

## Backups and data

- Settings includes **Download backup**, which creates a consistent SQLite backup.
- `python backup.py` writes a backup under the configured data directory and retains 14 days.
- Use **Settings → Restore backup** to restore a Tickover SQLite backup. The app validates it, saves a pre-restore safety copy, and then replaces the current database.
- Export your data before replacing or deleting your local database.

## Development and tests

```bash
pip install -r requirements-dev.txt
pytest
```

GitHub Actions runs tests and can build native packages on Windows and macOS runners. Run the workflow manually to download build artifacts, or push a version tag such as `v1.3.0` to create a GitHub Release containing Apple Silicon DMG, Intel DMG and Windows x64 EXE. The packages are unsigned; test them on clean devices before recommending them to others.

## License and third-party assets

The application code is MIT-licensed. Font license notices are bundled in `static/fonts/LICENSES.txt`. Audio redistribution rights have not yet been verified; review [`docs/ASSET-LICENSE-REVIEW.md`](docs/ASSET-LICENSE-REVIEW.md) before making a public release. The project icon is provisional.
