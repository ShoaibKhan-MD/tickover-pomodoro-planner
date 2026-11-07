from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
import json


def create_task(client, day, label, duration, **extra):
    payload = {"label": label, "duration": duration, **extra}
    response = client.post(f"/api/v1/ui/tasks?day={day}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["task"]


def test_fresh_database_has_no_personal_seed(app_client):
    _, client = app_client
    today = date.today().isoformat()
    response = client.get(f"/api/v1/ui/day?date={today}")
    assert response.status_code == 200
    assert response.json()["tasks"] == []
    assert response.json()["capacity"]["daily_capacity_minutes"] == 480


def test_priority_deadline_lock_and_capacity_fields(app_client):
    _, client = app_client
    day = date.today().isoformat()
    task = create_task(client, day, "Review immunology", 35,
                       priority="critical", deadline=(date.today()+timedelta(days=1)).isoformat(), locked=True)
    assert task["priority"] == "critical"
    assert task["locked"] is True
    assert task["deadline"] == (date.today()+timedelta(days=1)).isoformat()
    updated = client.patch(f"/api/v1/ui/tasks/{task['id']}", json={"priority": "high", "locked": False})
    assert updated.status_code == 200
    assert updated.json()["task"]["priority"] == "high"
    assert updated.json()["task"]["locked"] is False


def test_rollover_respects_capacity_priority_and_locks(app_client):
    _, client = app_client
    source = (date.today()-timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    client.put("/api/v1/ui/settings", json={"capacity_enabled": True, "daily_capacity_minutes": 255})
    create_task(client, target, "Already planned", 180)
    low = create_task(client, source, "Low priority", 70, priority="low")
    high = create_task(client, source, "High priority", 30, priority="high")
    locked = create_task(client, source, "Locked task", 20, locked=True)

    preview = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    # Limit is 255; 180 is already planned, leaving 75 minutes.
    # High-priority task is selected first; then low priority cannot fit.
    assert [t["id"] for t in body["moving"]] == [high["id"]]
    assert [t["id"] for t in body["deferred"]] == [low["id"]]
    assert [t["id"] for t in body["excluded_locked"]] == [locked["id"]]


def test_rollover_apply_rejects_stale_target_revision(app_client):
    _, client = app_client
    source = (date.today()-timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    task = create_task(client, source, "Carry me", 25)
    preview = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    create_task(client, target, "Concurrent edit", 25)
    response = client.post("/api/v1/ui/rollover/apply", json={
        "proposal_id": preview["proposal_id"],
        "expected_target_revision": preview["target_revision"],
    })
    assert response.status_code == 409
    current = client.get(f"/api/v1/ui/day?date={source}").json()
    assert any(t["id"] == task["id"] for t in current["tasks"])


def test_rollover_can_be_undone_and_is_idempotent_guarded(app_client):
    _, client = app_client
    source = (date.today()-timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    task = create_task(client, source, "Carry then restore", 25, priority="high")
    preview = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    applied = client.post("/api/v1/ui/rollover/apply", json={
        "proposal_id": preview["proposal_id"],
        "expected_target_revision": preview["target_revision"],
    })
    assert applied.status_code == 200, applied.text
    assert applied.json()["moved_count"] == 1
    assert applied.json()["proposal_id"] == preview["proposal_id"]
    moved = client.get(f"/api/v1/ui/day?date={target}").json()
    moved_task = next(t for t in moved["tasks"] if t["id"] == task["id"])
    assert moved_task["origin_day"] == source
    assert moved_task["rollover_count"] == 1
    undone = client.post("/api/v1/ui/rollover/undo", json={"proposal_id": preview["proposal_id"]})
    assert undone.status_code == 200, undone.text
    restored = client.get(f"/api/v1/ui/day?date={source}").json()
    restored_task = next(t for t in restored["tasks"] if t["id"] == task["id"])
    assert restored_task["day"] == source
    assert restored_task["rollover_count"] == 0
    again = client.post("/api/v1/ui/rollover/undo", json={"proposal_id": preview["proposal_id"]})
    assert again.status_code == 409


def test_automatic_rollover_runs_once_when_today_is_opened(app_client):
    _, client = app_client
    source = (date.today()-timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    task = create_task(client, source, "Auto carry", 25)
    settings = client.put("/api/v1/ui/settings", json={"rollover_mode": "automatic"})
    assert settings.status_code == 200
    today = client.get(f"/api/v1/ui/day?date={target}")
    assert today.status_code == 200
    assert any(t["id"] == task["id"] for t in today.json()["tasks"])
    # Re-reading the day must not create another proposal or duplicate/move the task again.
    second = client.get(f"/api/v1/ui/day?date={target}")
    assert len([t for t in second.json()["tasks"] if t["id"] == task["id"]]) == 1


def test_agent_api_requires_bearer_token_and_revision_is_checked(app_client):
    _, client = app_client
    day = date.today().isoformat()
    denied = client.get(f"/api/v1/agent/plan/{day}")
    assert denied.status_code == 401
    headers = {"Authorization": "Bearer test-agent-token"}
    plan = client.get(f"/api/v1/agent/plan/{day}", headers=headers).json()
    stale = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": plan["day"]["revision"] + 5,
        "replace_existing": True,
        "tasks": [],
    })
    assert stale.status_code == 409
    good = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": plan["day"]["revision"],
        "tasks": [{"external_id": "agent-test-1", "label": "API task", "duration": 25,
                   "priority": "critical", "deadline": day}],
    })
    assert good.status_code == 200, good.text
    assert good.json()["tasks"][0]["priority"] == "critical"
    activity = client.get("/api/v1/agent/activity?limit=10", headers=headers)
    assert activity.status_code == 200
    assert any(e["actor"] == "agent" for e in activity.json()["events"])


def test_rollover_reconsiders_older_backlog_and_protects_active_timer(app_client):
    _, client = app_client
    target = date.today().isoformat()
    old_day = (date.today() - timedelta(days=4)).isoformat()
    recent_day = (date.today() - timedelta(days=1)).isoformat()
    older = create_task(client, old_day, "Older backlog", 20, priority="high")
    active = create_task(client, recent_day, "Currently running", 25)
    started = client.post("/api/v1/ui/timer/start", json={"date": recent_day})
    assert started.status_code == 200, started.text
    preview = client.post("/api/v1/ui/rollover/preview", json={"source_day": recent_day, "target_day": target})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert any(t["id"] == older["id"] for t in body["moving"])
    assert any(t["id"] == active["id"] for t in body["excluded_active"])


def test_locked_tasks_can_only_move_when_explicitly_included(app_client):
    _, client = app_client
    source = (date.today() - timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    task = create_task(client, source, "Protected deadline", 25, locked=True)
    normal = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target})
    assert any(t["id"] == task["id"] for t in normal.json()["excluded_locked"])
    override = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target, "include_locked": True})
    assert any(t["id"] == task["id"] for t in override.json()["moving"])


def test_undo_refuses_to_revert_a_task_edited_after_rollover(app_client):
    _, client = app_client
    source = (date.today() - timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    task = create_task(client, source, "Keep my later edit", 25)
    proposal = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    applied = client.post("/api/v1/ui/rollover/apply", json={
        "proposal_id": proposal["proposal_id"], "expected_target_revision": proposal["target_revision"]
    })
    assert applied.status_code == 200
    edit = client.patch(f"/api/v1/ui/tasks/{task['id']}", json={"label": "Edited after rollover"})
    assert edit.status_code == 200
    undo = client.post("/api/v1/ui/rollover/undo", json={"proposal_id": proposal["proposal_id"]})
    assert undo.status_code == 409
    today = client.get(f"/api/v1/ui/day?date={target}").json()
    assert any(t["id"] == task["id"] and t["label"] == "Edited after rollover" for t in today["tasks"])


def test_pwa_assets_and_quit_endpoint_are_scoped(app_client):
    _, client = app_client
    assert client.get("/manifest.webmanifest").status_code == 200
    service_worker = client.get("/sw.js")
    assert service_worker.status_code == 200
    assert service_worker.headers.get("service-worker-allowed") == "/"
    assert client.post("/api/desktop/quit", json={}).status_code == 404


def test_invalid_task_data_is_rejected(app_client):
    _, client = app_client
    day = date.today().isoformat()
    blank = client.post(f"/api/v1/ui/tasks?day={day}", json={"label": "   ", "duration": 25})
    bad_type = client.post(f"/api/v1/ui/tasks?day={day}", json={"label": "Bad type", "type": "shell", "duration": 25})
    bad_deadline = client.post(f"/api/v1/ui/tasks?day={day}", json={"label": "Bad date", "deadline": "tomorrow", "duration": 25})
    assert blank.status_code == 422
    assert bad_type.status_code == 422
    assert bad_deadline.status_code == 422


def test_empty_environment_paths_use_safe_defaults(monkeypatch):
    import importlib
    import app as loaded_app
    monkeypatch.setenv("DATA_DIR", "")
    monkeypatch.setenv("DATABASE_PATH", "")
    module = importlib.reload(loaded_app)
    assert module.DATA_DIR == module.BASE_DIR / "data"
    assert module.DB_PATH == module.DATA_DIR / "study.db"


def test_database_backup_can_be_restored_with_safety_copy(app_client, tmp_path):
    _, client = app_client
    day = date.today().isoformat()
    original = create_task(client, day, "Backup snapshot task", 25)
    backup = client.get("/api/v1/ui/backup")
    assert backup.status_code == 200
    create_task(client, day, "Task created after backup", 30)
    restored = client.post("/api/v1/ui/restore", files={"file": ("tickover-backup.db", backup.content, "application/octet-stream")})
    assert restored.status_code == 200, restored.text
    assert restored.json()["safety_backup_created"] is True
    tasks = client.get(f"/api/v1/ui/day?date={day}").json()["tasks"]
    assert [task["id"] for task in tasks] == [original["id"]]
    module_data = tmp_path / "data"
    assert list((module_data / "backups").glob("pre-restore-*.db"))


def test_rollover_rejects_source_changes_and_capacity_changes(app_client):
    _, client = app_client
    source = (date.today() - timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    create_task(client, source, "Preview this", 25)
    first = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    create_task(client, source, "New source task", 15)
    changed_source = client.post("/api/v1/ui/rollover/apply", json={
        "proposal_id": first["proposal_id"], "expected_target_revision": first["target_revision"]
    })
    assert changed_source.status_code == 409

    client.put("/api/v1/ui/settings", json={"capacity_enabled": True})
    second = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    changed_settings = client.put("/api/v1/ui/settings", json={"daily_capacity_minutes": 360})
    assert changed_settings.status_code == 200
    stale_capacity = client.post("/api/v1/ui/rollover/apply", json={
        "proposal_id": second["proposal_id"], "expected_target_revision": second["target_revision"]
    })
    assert stale_capacity.status_code == 409


def test_timer_status_changes_increment_schedule_revision(app_client):
    _, client = app_client
    day = date.today().isoformat()
    create_task(client, day, "Start me", 25)
    before = client.get(f"/api/v1/ui/day?date={day}").json()["day"]["revision"]
    started = client.post("/api/v1/ui/timer/start", json={"date": day})
    assert started.status_code == 200
    after = client.get(f"/api/v1/ui/day?date={day}").json()["day"]["revision"]
    assert after > before


def test_backend_auto_advance_completes_expired_focus_and_starts_break(app_client):
    module, client = app_client
    day = date.today().isoformat()
    task = create_task(client, day, "Backend timer test", 25)
    settings = client.put("/api/v1/ui/settings", json={"auto_advance": True})
    assert settings.status_code == 200
    started = client.post("/api/v1/ui/timer/start", json={"date": day})
    assert started.status_code == 200

    with module.db() as conn:
        row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
        state = json.loads(row["state_json"])
        state["ends_at"] = module.now_ts() - 2
        state["remaining_seconds"] = 0
        module.save_timer(conn, state)

    module.process_expired_timers_once(day_filter=day)
    payload = client.get(f"/api/v1/ui/day?date={day}").json()
    finished_task = next(item for item in payload["tasks"] if item["id"] == task["id"])
    assert finished_task["status"] == "completed"
    assert payload["timer"]["mode"] == "break"
    assert payload["timer"]["break_kind"] == "short"
    assert payload["timer"]["running"] is True
    assert payload["timer"]["completed_pomodoros"] == 1
    with module.db() as conn:
        session = conn.execute("SELECT status FROM sessions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task["id"],)).fetchone()
    assert session["status"] == "completed"


def test_backend_timer_expiration_waits_for_user_when_auto_advance_disabled(app_client):
    module, client = app_client
    day = date.today().isoformat()
    create_task(client, day, "Manual completion test", 25)
    client.post("/api/v1/ui/timer/start", json={"date": day})
    with module.db() as conn:
        row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
        state = json.loads(row["state_json"])
        state["ends_at"] = module.now_ts() - 2
        module.save_timer(conn, state)

    module.process_expired_timers_once(day_filter=day)
    timer = client.get(f"/api/v1/ui/day?date={day}").json()["timer"]
    assert timer["expired"] is True
    assert timer["running"] is False
    assert timer["mode"] == "work"


def _study_session(module, task_id, day, minutes):
    with module.db() as conn:
        conn.execute(
            "INSERT INTO sessions(id,task_id,day,kind,started_at,planned_seconds,focus_seconds,status,created_at) VALUES(?,?,?,'work',0,?,?,'completed','x')",
            (module.new_id(), task_id, day, minutes * 60, minutes * 60))


def _capacity(client, day):
    return client.get(f"/api/v1/ui/capacity?date={day}").json()


def test_capacity_is_off_by_default_and_rollover_is_unlimited(app_client):
    _, client = app_client
    source = (date.today() - timedelta(days=1)).isoformat()
    target = date.today().isoformat()
    assert _capacity(client, target)["enabled"] is False
    for i in range(3):
        create_task(client, source, f"Big block {i}", 240)  # 720 minutes, more than the default limit
    body = client.post("/api/v1/ui/rollover/preview", json={"source_day": source, "target_day": target}).json()
    assert len(body["moving"]) == 3 and body["deferred"] == []


def test_completing_a_block_does_not_free_capacity(app_client):
    _, client = app_client
    day = date.today().isoformat()
    client.put("/api/v1/ui/settings", json={"capacity_enabled": True, "daily_capacity_minutes": 120})
    first = create_task(client, day, "A", 60)
    create_task(client, day, "B", 60)
    before = _capacity(client, day)
    assert (before["planned_minutes"], before["remaining_minutes"]) == (120, 0)
    client.patch(f"/api/v1/ui/tasks/{first['id']}", json={"status": "completed"})
    after = _capacity(client, day)
    assert (after["planned_minutes"], after["remaining_minutes"]) == (120, 0)
    assert after["studied_minutes"] == 60 and after["remaining_planned_minutes"] == 60


def test_finishing_early_frees_only_the_saved_time_and_overruns_use_it(app_client):
    module, client = app_client
    day = date.today().isoformat()
    client.put("/api/v1/ui/settings", json={"capacity_enabled": True, "daily_capacity_minutes": 120})
    first = create_task(client, day, "A", 60)
    second = create_task(client, day, "B", 60)
    _study_session(module, first["id"], day, 40)
    client.patch(f"/api/v1/ui/tasks/{first['id']}", json={"status": "completed"})
    early = _capacity(client, day)
    assert (early["planned_minutes"], early["remaining_minutes"]) == (100, 20)
    _study_session(module, second["id"], day, 100)  # B runs far over its 60 minutes
    over = _capacity(client, day)
    assert over["planned_minutes"] == 140 and over["over_capacity_minutes"] == 20 and over["is_over_capacity"] is True


def test_capacity_limit_range_and_recent_average(app_client):
    module, client = app_client
    assert client.put("/api/v1/ui/settings", json={"daily_capacity_minutes": 20}).status_code == 422
    assert client.put("/api/v1/ui/settings", json={"daily_capacity_minutes": 30}).status_code == 200
    today = date.today()
    for back, minutes in ((1, 120), (2, 180)):
        day = (today - timedelta(days=back)).isoformat()
        _study_session(module, create_task(client, day, "Earlier", 30)["id"], day, minutes)
    assert _capacity(client, today.isoformat())["recent_average_minutes"] == 150


def test_concurrent_timer_expiry_is_advanced_only_once(app_client):
    module, client = app_client
    day = date.today().isoformat()
    create_task(client, day, "Race condition test", 25)
    client.put("/api/v1/ui/settings", json={"auto_advance": True})
    client.post("/api/v1/ui/timer/start", json={"date": day})
    with module.db() as conn:
        row = conn.execute("SELECT state_json FROM timers WHERE day=?", (day,)).fetchone()
        state = json.loads(row["state_json"])
        state["ends_at"] = module.now_ts() - 2
        module.save_timer(conn, state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: module.process_expired_timers_once(day_filter=day), range(2)))

    payload = client.get(f"/api/v1/ui/day?date={day}").json()
    assert payload["timer"]["completed_pomodoros"] == 1
    assert payload["timer"]["mode"] == "break"
    with module.db() as conn:
        events = conn.execute("SELECT COUNT(*) FROM activity_log WHERE action='timer.auto_advanced' AND target_day=?", (day,)).fetchone()[0]
    assert events == 1


def test_background_timer_monitor_does_not_auto_complete_historical_days(app_client):
    module, client = app_client
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    create_task(client, yesterday, "Historical timer", 25)
    client.put("/api/v1/ui/settings", json={"auto_advance": True})
    client.post("/api/v1/ui/timer/start", json={"date": yesterday})
    with module.db() as conn:
        row = conn.execute("SELECT state_json FROM timers WHERE day=?", (yesterday,)).fetchone()
        state = json.loads(row["state_json"])
        state["ends_at"] = module.now_ts() - 2
        module.save_timer(conn, state)

    module.process_expired_timers_once()  # background-worker behavior: today only
    with module.db() as conn:
        stored = json.loads(conn.execute("SELECT state_json FROM timers WHERE day=?", (yesterday,)).fetchone()[0])
    assert stored["running"] is True
    assert stored["mode"] == "work"


def test_agent_plan_preview_reports_diff_and_capacity(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    initial = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [
            {"external_id": "keep-me", "label": "Existing block", "duration": 40, "locked": True},
            {"external_id": "remove-me", "label": "Old block", "duration": 20},
        ]
    })
    assert initial.status_code == 200, initial.text
    revision = initial.json()["day"]["revision"]
    existing_id = next(t["id"] for t in initial.json()["tasks"] if t["external_id"] == "keep-me")
    preview = client.post(f"/api/v1/agent/plan/{day}/preview", headers=headers, json={
        "expected_revision": revision,
        "replace_existing": True,
        "tasks": [
            {"external_id": "keep-me", "label": "Existing block updated", "duration": 50},
            {"external_id": "new-block", "label": "New block", "duration": 30},
        ],
    })
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert len(body["additions"]) == 1
    assert len(body["updates"]) == 1
    assert len(body["removals"]) == 1
    assert body["locked_conflicts"] == []  # omitted locked=true is preserved for the matching task
    assert body["ready_to_apply"] is True
    assert body["capacity_after"]["planned_minutes"] == 80

    applied = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": body["expected_revision"],
        "replace_existing": True,
        "tasks": [
            {"external_id": "keep-me", "label": "Existing block updated", "duration": 50},
            {"external_id": "new-block", "label": "New block", "duration": 30},
        ],
    })
    assert applied.status_code == 200, applied.text
    kept = next(t for t in applied.json()["tasks"] if t["external_id"] == "keep-me")
    assert kept["id"] == existing_id
    assert kept["locked"] is True
    assert {t["external_id"] for t in applied.json()["tasks"]} == {"keep-me", "new-block"}


def test_agent_plan_cannot_remove_locked_task_without_explicit_override(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    created = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [{"external_id": "protected", "label": "Protected block", "duration": 25, "locked": True}]
    })
    revision = created.json()["day"]["revision"]
    preview = client.post(f"/api/v1/agent/plan/{day}/preview", headers=headers, json={
        "expected_revision": revision, "replace_existing": True, "tasks": []
    })
    assert preview.status_code == 200
    assert preview.json()["ready_to_apply"] is False
    assert preview.json()["locked_conflicts"][0]["reasons"] == ["would be removed"]
    blocked = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": revision, "replace_existing": True, "tasks": []
    })
    assert blocked.status_code == 409
    still_there = client.get(f"/api/v1/agent/plan/{day}", headers=headers).json()
    assert len(still_there["tasks"]) == 1
    allowed = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": revision, "replace_existing": True, "override_locked": True, "tasks": []
    })
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["tasks"] == []


def test_agent_cannot_remove_task_with_active_timer_session(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    plan = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [{"external_id": "active", "label": "Current session", "duration": 25}]
    }).json()
    revision = plan["day"]["revision"]
    task_id = plan["tasks"][0]["id"]
    assert client.post("/api/v1/ui/timer/start", json={"date": day}).status_code == 200
    response = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": client.get(f"/api/v1/agent/plan/{day}", headers=headers).json()["day"]["revision"],
        "replace_existing": True,
        "tasks": [],
    })
    assert response.status_code == 409
    still_there = client.get(f"/api/v1/agent/plan/{day}", headers=headers).json()
    assert still_there["tasks"][0]["id"] == task_id


def test_agent_reordering_locked_task_requires_override(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    plan = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [
            {"external_id": "locked-order", "label": "Fixed first", "duration": 25, "locked": True},
            {"external_id": "move-order", "label": "Movable second", "duration": 25},
        ]
    }).json()
    ids = [task["id"] for task in plan["tasks"]]
    denied = client.post(f"/api/v1/agent/tasks/{day}/reorder", headers=headers, json={"ids": list(reversed(ids))})
    assert denied.status_code == 409
    allowed = client.post(f"/api/v1/agent/tasks/{day}/reorder", headers=headers, json={
        "ids": list(reversed(ids)), "override_locked": True
    })
    assert allowed.status_code == 200


def test_agent_plan_preserves_omitted_task_metadata(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    original = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "replace_existing": True,
        "tasks": [{
            "external_id": "stable-task", "label": "Original", "duration": 45,
            "priority": "high", "deadline": day, "locked": True,
            "items": [{"label": "Chapter 1", "done": False}],
            "notes": "Keep this note", "questions_done": 20, "questions_correct": 16,
        }],
    })
    assert original.status_code == 200, original.text
    task_id = original.json()["tasks"][0]["id"]

    preview = client.post(f"/api/v1/agent/plan/{day}/preview", headers=headers, json={
        "replace_existing": True,
        "tasks": [{"external_id": "stable-task", "label": "Renamed", "duration": 50}],
    })
    assert preview.status_code == 200, preview.text
    changes = preview.json()["updates"][0]["changes"]
    assert set(changes) == {"duration", "label"}

    updated = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": preview.json()["expected_revision"],
        "replace_existing": True,
        "tasks": [{"external_id": "stable-task", "label": "Renamed", "duration": 50}],
    })
    assert updated.status_code == 200, updated.text
    task = updated.json()["tasks"][0]
    assert task["id"] == task_id
    assert task["priority"] == "high"
    assert task["deadline"] == day
    assert task["locked"] is True
    assert task["items"] == [{"label": "Chapter 1", "done": False}]
    assert task["notes"] == "Keep this note"
    assert task["questions_done"] == 20
    assert task["questions_correct"] == 16


def test_partial_agent_plan_does_not_false_positive_locked_reorder(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    original = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "replace_existing": True,
        "tasks": [
            {"external_id": "first", "label": "First", "duration": 25},
            {"external_id": "locked-second", "label": "Second", "duration": 25, "locked": True},
        ],
    })
    assert original.status_code == 200, original.text
    revision = original.json()["day"]["revision"]
    preview = client.post(f"/api/v1/agent/plan/{day}/preview", headers=headers, json={
        "expected_revision": revision,
        "tasks": [{"external_id": "locked-second", "label": "Second renamed"}],
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["locked_conflicts"] == []
    applied = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "expected_revision": revision,
        "tasks": [{"external_id": "locked-second", "label": "Second renamed"}],
    })
    assert applied.status_code == 200, applied.text
    tasks = applied.json()["tasks"]
    assert [t["external_id"] for t in tasks] == ["first", "locked-second"]


def test_agent_cannot_unlock_protected_task_without_override(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    task = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [{"external_id": "protected", "label": "Protected", "duration": 25, "locked": True}]
    }).json()["tasks"][0]
    denied = client.patch(f"/api/v1/agent/tasks/{task['id']}", headers=headers, json={"locked": False})
    assert denied.status_code == 409
    allowed = client.patch(f"/api/v1/agent/tasks/{task['id']}?override_locked=true", headers=headers, json={"locked": False})
    assert allowed.status_code == 200
    assert allowed.json()["task"]["locked"] is False


def test_task_with_active_timer_cannot_be_deleted(app_client):
    _, client = app_client
    headers = {"Authorization": "Bearer test-agent-token"}
    day = date.today().isoformat()
    plan = client.put(f"/api/v1/agent/plan/{day}", headers=headers, json={
        "tasks": [{"external_id": "active", "label": "Current session", "duration": 25}]
    }).json()
    task_id = plan["tasks"][0]["id"]
    assert client.post("/api/v1/ui/timer/start", json={"date": day}).status_code == 200
    denied = client.delete(f"/api/v1/agent/tasks/{task_id}", headers=headers)
    assert denied.status_code == 409
    still_there = client.get(f"/api/v1/agent/plan/{day}", headers=headers).json()
    assert still_there["tasks"][0]["id"] == task_id


def test_tray_and_theme_settings(app_client):
    _, client = app_client
    settings = client.get("/api/v1/ui/settings").json()
    assert settings["tray_icon"] is True and settings["theme"] == "rose"
    assert client.put("/api/v1/ui/settings", json={"tray_icon": False, "theme": "forest"}).status_code == 200
    assert client.put("/api/v1/ui/settings", json={"theme_colors": "#112233,#445566,#778899,#aabbcc"}).status_code == 200
    assert client.put("/api/v1/ui/settings", json={"theme_colors": "red,blue"}).status_code == 422
    settings = client.get("/api/v1/ui/settings").json()
    assert settings["tray_icon"] is False and settings["theme"] == "forest"
