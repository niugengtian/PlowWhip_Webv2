from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.system_scheduler import SystemScheduler


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


def test_os_scheduler_plan_and_authorization_boundary() -> None:
    with TemporaryDirectory() as directory:
        manager = SystemScheduler(Path(directory), python_executable="/usr/bin/python3")
        with patch("plow_whip_web.system_scheduler.platform.system", return_value="Darwin"):
            plan = manager.plan()
            denied = manager.install(interval_seconds=30, authorized=False)
        assert plan.backend == "launchd"
        assert plan.command[:4] == ["/usr/bin/python3", "-m", "plow_whip_web", "scheduler-tick"]
        assert denied["installed"] is False
        assert denied["authorization_required"] is True


def test_scheduler_api_exposes_capability_without_installing() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            status = client.get("/api/scheduler/status")
            install = client.post("/api/scheduler/install")
        assert status.status_code == 200
        assert status.json()["model_invoked"] is False
        assert status.json()["authorization_required"] is True
        assert install.status_code == 200
        assert install.json()["installed"] is False
        assert install.json()["authorization_required"] is True


def test_scheduler_authorization_creates_durable_permission_record() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            settings = client.get("/api/settings").json()
            settings["values"]["system_scheduler_authorized"] = True
            updated = client.put(
                "/api/settings",
                json={"expected_revision": settings["revision"], "values": settings["values"]},
            )
            status = client.get("/api/scheduler/status")
            permissions = client.get("/api/permissions").json()
        assert updated.status_code == 200
        assert status.json()["authorization_required"] is False
        grant = next(item for item in permissions if item["capability"] == "system_scheduler")
        assert grant["decision"] == "allow"
        assert grant["reason"] == "authorized from Settings"
