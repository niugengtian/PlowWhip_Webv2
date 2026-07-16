from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.runtime.cron import CronExpression, EmbeddedCronRunner, schedule_view, validate_timezone


def test_settings_are_validated_and_revision_guarded() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            current = client.get("/api/settings")
            assert current.status_code == 200
            payload = current.json()
            assert payload["revision"] == 0
            payload["values"]["max_parallel_workers"] = 8
            updated = client.put(
                "/api/settings",
                json={"expected_revision": 0, "values": payload["values"]},
            )
            assert updated.status_code == 200
            assert updated.json()["revision"] == 1
            assert updated.json()["values"]["max_parallel_workers"] == 8

            conflict = client.put(
                "/api/settings",
                json={"expected_revision": 0, "values": payload["values"]},
            )
            assert conflict.status_code == 409
            invalid = dict(payload["values"])
            invalid["scheduler_interval_seconds"] = 60
            invalid["scheduler_lease_seconds"] = 90
            rejected = client.put(
                "/api/settings",
                json={"expected_revision": 1, "values": invalid},
            )
            assert rejected.status_code == 422


def test_tick_scans_all_projects_and_uses_zero_control_tokens() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        projects = app.state.project_repository
        tasks = app.state.task_repository
        task_ids: list[str] = []
        for index in range(2):
            path = root / f"project-{index}"
            path.mkdir()
            project = projects.create(name=f"project-{index}", path=str(path))
            binding = projects.resolve_role(project["id"], "fullstack")
            task = tasks.create(
                title=f"task-{index}", objective="scheduler must finish it", project_path=str(path),
                project_id=project["id"], role_id=binding["role_id"], resource_key=f"repo:{index}",
                command={"argv": [sys.executable, "-c", f"from pathlib import Path; Path('done').write_text('{index}')"]},
                verification=[{"kind": "file_exists", "path": "done"}],
                max_attempts=1, token_budget=100, idempotency_key=f"scheduled-{index}",
            )
            task_ids.append(task.id)

        result = app.state.scheduler_service.tick(owner="test-scheduler")
        assert result["status"] == "completed"
        assert result["scanned"] == 2
        assert result["selected"] == 2
        assert result["model_tokens"] == 0
        assert {item["status"] for item in result["completed"]} == {"completed"}
        assert all(tasks.get(task_id).tokens_used == 0 for task_id in task_ids)
        status = app.state.scheduler_repository.status()
        assert status["last_result"]["model_tokens"] == 0


def test_global_scheduler_lease_blocks_second_scheduler() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        repository = SchedulerRepository(app.state.database)
        first = repository.acquire("node-a", lease_seconds=90)
        second = repository.acquire("node-b", lease_seconds=90)
        assert first.acquired is True
        assert second.acquired is False
        assert second.fencing_token == first.fencing_token
        assert repository.finish(first, {"model_tokens": 0}) is True


def test_standard_cron_expression_supports_steps_ranges_lists_and_weekday_or_semantics() -> None:
    expression = CronExpression.parse("*/15 8-10 1 1,7 1")
    assert expression.matches(datetime(2026, 7, 1, 8, 30, tzinfo=timezone.utc)) is True
    assert expression.matches(datetime(2026, 7, 6, 9, 45, tzinfo=timezone.utc)) is True
    assert expression.matches(datetime(2026, 7, 7, 9, 45, tzinfo=timezone.utc)) is False
    assert CronExpression.parse("0 0 * * 7").matches(
        datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    ) is True
    assert expression.next_after(datetime(2026, 7, 7, 9, 44, tzinfo=timezone.utc)) == datetime(
        2026, 7, 13, 8, 0, tzinfo=timezone.utc
    )


def test_cron_validation_rejects_malformed_values_and_timezones() -> None:
    for source in ("* * * *", "*/0 * * * *", "61 * * * *", "x * * * *", "10-1 * * * *"):
        try:
            CronExpression.parse(source)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid cron accepted: {source}")
    try:
        validate_timezone("Mars/Olympus_Mons")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid timezone accepted")


def test_schedule_view_reports_next_run_and_disabled_state() -> None:
    values: dict[str, object] = {
        "cron_enabled": True,
        "cron_expression": "*/5 * * * *",
        "cron_timezone": "UTC",
        "cron_misfire_policy": "skip",
    }
    now = datetime(2026, 7, 17, 12, 1, tzinfo=timezone.utc)
    assert schedule_view(values, now=now)["next_run_at"] == "2026-07-17T12:05:00+00:00"
    values["cron_enabled"] = False
    assert schedule_view(values, now=now)["next_run_at"] is None


def test_scheduler_api_exposes_embedded_container_cron_without_host_authorization() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(
            data_dir=Path(directory) / "runtime",
            embedded_cron=True,
            container_loopback=True,
        ))
        with TestClient(app) as client:
            status = client.get("/api/scheduler/status")
        assert status.status_code == 200
        assert status.json()["model_invoked"] is False
        assert status.json()["authorization_required"] is False
        assert status.json()["engine"]["backend"] == "embedded-cron"
        assert status.json()["engine"]["active"] is False
        assert status.json()["engine"]["managed_by"] == "docker"
        assert status.json()["schedule"]["expression"] == "*/1 * * * *"


def test_crontab_settings_are_durable_and_revision_guarded() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            settings = client.get("/api/settings").json()
            settings["values"]["cron_expression"] = "*/5 * * * *"
            settings["values"]["cron_timezone"] = "UTC"
            settings["values"]["cron_misfire_policy"] = "skip"
            updated = client.put(
                "/api/settings",
                json={"expected_revision": settings["revision"], "values": settings["values"]},
            )
            status = client.get("/api/scheduler/status")
        assert updated.status_code == 200
        assert status.json()["authorization_required"] is False
        assert status.json()["schedule"]["expression"] == "*/5 * * * *"
        assert status.json()["schedule"]["timezone"] == "UTC"
        assert status.json()["schedule"]["misfire_policy"] == "skip"


def test_embedded_runner_executes_each_matching_minute_once() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        calls: list[str | None] = []
        app.state.scheduler_service.tick = lambda *, owner=None: calls.append(owner) or {"status": "completed"}
        runner = EmbeddedCronRunner(
            app.state.scheduler_service,
            app.state.scheduler_repository,
            app.state.runtime_settings,
        )
        slot = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
        assert runner.run_due(slot) == {"status": "completed"}
        assert runner.run_due(slot.replace(second=30)) is None
        assert len(calls) == 1
        assert app.state.scheduler_repository.status()["last_cron_slot"] == "2026-07-17T20:00:00+08:00"


def test_embedded_runner_persists_heartbeat_stop_and_error() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        settings = app.state.runtime_settings.get()
        settings["values"]["cron_enabled"] = False
        app.state.runtime_settings.update(settings["values"], expected_revision=settings["revision"])
        runner = EmbeddedCronRunner(
            app.state.scheduler_service,
            app.state.scheduler_repository,
            app.state.runtime_settings,
            poll_seconds=1,
        )
        runner.start()
        runner.start()
        runner.stop()
        status = app.state.scheduler_repository.status()
        assert status["runner_id"] == runner.runner_id
        assert status["runner_started_at"] is not None
        assert status["runner_heartbeat_at"] is not None
        assert status["runner_stopped_at"] is not None
        assert status["runner_active"] is False
        app.state.scheduler_repository.runner_error(runner.runner_id, "x" * 3000)
        assert len(app.state.scheduler_repository.status()["runner_error"]) == 2000


def test_embedded_runner_catches_up_once_after_a_missed_slot() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        current = app.state.runtime_settings.get()
        current["values"].update({
            "cron_expression": "0 12 * * *",
            "cron_timezone": "UTC",
            "cron_misfire_policy": "catch_up_once",
        })
        app.state.runtime_settings.update(current["values"], expected_revision=current["revision"])
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE scheduler_state SET last_tick_at = '2026-07-17 11:30:00' WHERE id = 'global'"
            )
        calls: list[str | None] = []
        app.state.scheduler_service.tick = lambda *, owner=None: calls.append(owner) or {"status": "completed"}
        runner = EmbeddedCronRunner(
            app.state.scheduler_service,
            app.state.scheduler_repository,
            app.state.runtime_settings,
        )
        result = runner.run_due(datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc))
        assert result == {"status": "completed"}
        assert len(calls) == 1
