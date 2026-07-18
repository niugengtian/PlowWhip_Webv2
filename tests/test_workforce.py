from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import ResourceBusyError
from plow_whip_web.runtime.task_service import TaskService


def _project(client: TestClient, path: Path, name: str) -> dict[str, object]:
    response = client.post("/api/projects", json={"name": name, "path": str(path)})
    assert response.status_code == 201
    return response.json()


def _task(client: TestClient, project_id: str, key: str, filename: str) -> dict[str, object]:
    response = client.post(
        "/api/tasks",
        headers={"Idempotency-Key": key},
        json={
            "title": f"write {filename}",
            "objective": "produce verified project output",
            "project_id": project_id,
            "role": "fullstack",
            "command": {
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({filename!r}).write_text('ok')",
                ]
            },
            "verification": [{"kind": "file_contains", "path": filename, "contains": "ok"}],
        },
    )
    assert response.status_code == 201
    return response.json()


def _drive(client: TestClient, task: dict[str, object], key: str) -> dict[str, object]:
    response = client.post(
        f"/api/tasks/{task['id']}/drive",
        headers={"Idempotency-Key": key},
        json={"expected_revision": task["revision"]},
    )
    assert response.status_code == 200
    return response.json()


def test_manual_task_workers_are_ephemeral_and_released_per_terminal() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = _project(client, project_path, "alpha")
            first = _drive(client, _task(client, project["id"], "create-alpha-1", "one.txt"), "drive-alpha-1")
            first_worker = first["worker_id"]
            workforce_after_first = client.get(f"/api/projects/{project['id']}").json()["workers"]
            assert workforce_after_first[0]["status"] == "released"

            second = _drive(client, _task(client, project["id"], "create-alpha-2", "two.txt"), "drive-alpha-2")
            workforce_after_second = client.get(f"/api/projects/{project['id']}").json()["workers"]

            assert second["worker_id"] != first_worker
            assert len(workforce_after_second) == 2
            assert all(worker["status"] == "released" for worker in workforce_after_second)

            rotated = client.post(
                f"/api/workers/{first_worker}/rotate", json={"reason": "context_limit"}
            )
            assert rotated.status_code == 409

            released = client.post(f"/api/projects/{project['id']}/release")
            assert released.status_code == 200
            assert released.json()["status"] == "completed"
            assert all(worker["status"] == "released" for worker in released.json()["workers"])

        connection = app.state.database.connect()
        try:
            assert connection.execute("SELECT COUNT(*) FROM worker_session_archives").fetchone()[0] == 2
            assert connection.execute("SELECT COUNT(*) FROM task_leases").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM resource_locks").fetchone()[0] == 0
        finally:
            connection.close()


def test_sessions_never_cross_project_boundary() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            paths = [root / "a", root / "b"]
            for path in paths:
                path.mkdir()
            projects = [_project(client, paths[0], "a"), _project(client, paths[1], "b")]
            for index, project in enumerate(projects):
                task = _task(client, project["id"], f"create-cross-{index}", f"{index}.txt")
                _drive(client, task, f"drive-cross-{index}")
            sessions = [
                client.get(f"/api/projects/{project['id']}").json()["workers"][0]["session_id"]
                for project in projects
            ]
        assert sessions[0] != sessions[1]


def test_amended_terminal_task_gets_a_fresh_ephemeral_worker() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        projects = app.state.project_repository
        repository = app.state.task_repository
        project = projects.create(name="amend", path=str(project_path))
        role_id = projects.resolve_role(project["id"], "fullstack")["role_id"]
        task = repository.create(
            title="amend",
            objective="run twice with a fresh execution worker",
            project_path=str(project_path),
            project_id=project["id"],
            role_id=role_id,
            resource_key="repo:amend",
            command={"argv": [sys.executable, "-c", "pass"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="amend-create",
        )
        service = TaskService(repository)
        first = service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="amend-first-drive",
        )
        amended = repository.amend_spec(
            task.id,
            spec=first.spec,
            reason="new execution episode",
            expected_revision=first.revision,
            idempotency_key="amend-spec",
        )
        second = service.drive(
            task.id,
            expected_revision=amended.revision,
            idempotency_key="amend-second-drive",
        )

        assert first.status.value == "completed"
        assert second.status.value == "completed"
        assert second.worker_id != first.worker_id
        assert second.role_id != first.role_id
        connection = app.state.database.connect()
        try:
            workers = connection.execute(
                """
                SELECT status, released_at FROM workers
                WHERE id IN (?, ?) ORDER BY created_at
                """,
                (first.worker_id, second.worker_id),
            ).fetchall()
            assert len(workers) == 2
            assert all(row["status"] == "released" for row in workers)
            assert all(row["released_at"] is not None for row in workers)
        finally:
            connection.close()


def test_worker_and_resource_leases_prevent_brain_split() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        repository = app.state.task_repository
        projects = app.state.project_repository
        paths = [root / "a", root / "b"]
        for path in paths:
            path.mkdir()
        project_a = projects.create(name="a", path=str(paths[0]))
        project_b = projects.create(name="b", path=str(paths[1]))
        role_a = projects.resolve_role(project_a["id"], "fullstack")["role_id"]
        role_b = projects.resolve_role(project_b["id"], "fullstack")["role_id"]

        def create(project: dict[str, object], role_id: str, key: str, resource: str):
            return repository.create(
                title=key, objective=key, project_path=project["path"],
                project_id=project["id"], role_id=role_id, resource_key=resource,
                command={"argv": [sys.executable, "-c", "pass"]},
                verification=[{"kind": "exit_code", "expected": 0}],
                max_attempts=1, idempotency_key=key,
            )

        first = create(project_a, role_a, "lease-first", "port:3000")
        same_role = create(project_a, role_a, "lease-same-role", "port:3001")
        same_resource = create(project_b, role_b, "lease-same-resource", "port:3000")
        repository.claim(first.id, expected_revision=0, idempotency_key="claim-first")
        with pytest.raises(ResourceBusyError, match="role worker is busy"):
            repository.claim(same_role.id, expected_revision=0, idempotency_key="claim-same-role")
        with pytest.raises(ResourceBusyError, match="resource is busy"):
            repository.claim(same_resource.id, expected_revision=0, idempotency_key="claim-same-resource")


def test_two_projects_execute_in_parallel() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        projects = app.state.project_repository
        repository = app.state.task_repository
        tasks = []
        for index in range(2):
            path = root / str(index)
            path.mkdir()
            project = projects.create(name=str(index), path=str(path))
            role_id = projects.resolve_role(project["id"], "fullstack")["role_id"]
            tasks.append(repository.create(
                title=str(index), objective="parallel", project_path=str(path),
                project_id=project["id"], role_id=role_id, resource_key=f"repo:{index}",
                command={"argv": [sys.executable, "-c", "import time; time.sleep(.05)"]},
                verification=[{"kind": "exit_code", "expected": 0}],
                max_attempts=1, idempotency_key=f"parallel-{index}",
            ))
        service = TaskService(repository)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda item: service.drive(item.id, expected_revision=0, idempotency_key=f"drive-{item.id}"),
                tasks,
            ))
        assert [result.status.value for result in results] == ["completed", "completed"]
        assert results[0].worker_id != results[1].worker_id
