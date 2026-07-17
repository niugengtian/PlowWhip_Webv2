from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


class ArtifactBridge:
    def __init__(self) -> None:
        self.opened: tuple[str, str, str] | None = None

    def inspect_artifacts(
        self, *, project_path: str, paths: list[str]
    ) -> list[dict[str, object]]:
        return [{
            "relative_path": path,
            "host_path": str(Path(project_path) / path),
            "exists": (Path(project_path) / path).is_file(),
            "bytes": (Path(project_path) / path).stat().st_size,
            "sha256": "a" * 64,
            "modified_at": "2026-07-17T00:00:00+00:00",
            "actions": ["finder", "cursor"],
        } for path in paths]

    def open_artifact(
        self, *, project_path: str, relative_path: str, action: str
    ) -> dict[str, object]:
        self.opened = (project_path, relative_path, action)
        return {"status": "opened"}


def _task_payload(project: Path, *, content: str = "quality-pass") -> dict[str, object]:
    code = f"from pathlib import Path; Path('result.txt').write_text({content!r}, encoding='utf-8')"
    return {
        "title": "Create verified result",
        "objective": "Create result.txt with approved content",
        "project_path": str(project),
        "command": {"argv": [sys.executable, "-c", code], "timeout_seconds": 10},
        "verification": [
            {"kind": "exit_code", "expected": 0},
            {"kind": "file_exists", "path": "result.txt"},
            {"kind": "file_contains", "path": "result.txt", "contains": content},
        ],
    }


def test_create_drive_verify_and_absorb_terminal_state() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            created = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-success-001"},
                json=_task_payload(project),
            )
            assert created.status_code == 201
            task = created.json()
            assert task["status"] == "ready"
            assert task["revision"] == 0

            driven = client.post(
                f"/api/tasks/{task['id']}/drive",
                headers={"Idempotency-Key": "drive-success-001"},
                json={"expected_revision": task["revision"]},
            )
            assert driven.status_code == 200
            completed = driven.json()
            assert completed["status"] == "completed"
            assert completed["revision"] == 3
            assert completed["attempts_used"] == 1
            assert completed["tokens_used"] == 0
            assert completed["last_evidence_hash"]
            assert (project / "result.txt").read_text(encoding="utf-8") == "quality-pass"

            rerun = client.post(
                f"/api/tasks/{task['id']}/drive",
                headers={"Idempotency-Key": "drive-new-after-complete"},
                json={"expected_revision": completed["revision"]},
            )
            assert rerun.status_code == 409
            assert rerun.json()["code"] == "invalid_transition"

            events = client.get(f"/api/tasks/{task['id']}/events").json()
            assert [event["event_type"] for event in events] == [
                "task.created",
                "attempt.started",
                "verification.started",
                "task.completed",
            ]


def test_task_artifacts_point_to_host_project_and_open_only_declared_files() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        container_project = root / "container"
        host_project = root / "host"
        container_project.mkdir()
        host_project.mkdir()
        (host_project / "报告.md").write_text("ready", encoding="utf-8")
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        bridge = ArtifactBridge()
        app.state.provider_pool.bridge = bridge
        project = app.state.project_repository.create(
            name="artifact-project",
            path=str(container_project),
            host_path=str(host_project),
        )
        role = app.state.project_repository.resolve_role(project["id"], "verification")
        task = app.state.task_repository.create(
            title="artifact-index",
            objective="index report",
            project_path=str(container_project),
            project_id=project["id"],
            role_id=role["role_id"],
            provider="cursor",
            command={"argv": ["cursor"], "timeout_seconds": 60},
            verification=[{"kind": "file_exists", "path": "报告.md"}],
            max_attempts=1,
            token_budget=100,
            idempotency_key="artifact-index-create",
        )

        with TestClient(app) as client:
            artifacts = client.get(f"/api/tasks/{task.id}/artifacts")
            opened = client.post(
                f"/api/tasks/{task.id}/artifacts/open",
                json={"relative_path": "报告.md", "action": "cursor"},
            )
            rejected = client.post(
                f"/api/tasks/{task.id}/artifacts/open",
                json={"relative_path": "secrets.env", "action": "cursor"},
            )

        assert artifacts.status_code == 200
        assert artifacts.json()[0]["host_path"] == str(host_project / "报告.md")
        assert opened.status_code == 200
        assert bridge.opened == (str(host_project), "报告.md", "cursor")
        assert rejected.status_code == 403


def test_verification_failure_cannot_complete() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        payload = _task_payload(project, content="actual")
        payload["verification"] = [
            {"kind": "file_contains", "path": "result.txt", "contains": "expected"}
        ]
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            task = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-failure-001"},
                json=payload,
            ).json()
            driven = client.post(
                f"/api/tasks/{task['id']}/drive",
                headers={"Idempotency-Key": "drive-failure-001"},
                json={"expected_revision": 0},
            )

        assert driven.status_code == 200
        assert driven.json()["status"] == "terminal_failed"
        assert "verification failed" in driven.json()["last_error"]


def test_create_and_drive_are_idempotent() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        code = (
            "from pathlib import Path; "
            "p=Path('count.txt'); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        )
        payload = {
            "title": "Increment once",
            "objective": "Prove duplicate drive does not execute twice",
            "project_path": str(project),
            "command": {"argv": [sys.executable, "-c", code]},
            "verification": [{"kind": "file_contains", "path": "count.txt", "contains": "1"}],
        }
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            first = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "same-create-key"},
                json=payload,
            ).json()
            duplicate = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "same-create-key"},
                json=payload,
            ).json()
            assert duplicate["id"] == first["id"]

            once = client.post(
                f"/api/tasks/{first['id']}/drive",
                headers={"Idempotency-Key": "same-drive-key"},
                json={"expected_revision": 0},
            ).json()
            twice = client.post(
                f"/api/tasks/{first['id']}/drive",
                headers={"Idempotency-Key": "same-drive-key"},
                json={"expected_revision": 0},
            ).json()

        assert once["status"] == "completed"
        assert twice["status"] == "completed"
        assert (project / "count.txt").read_text() == "1"


def test_revision_conflict_is_explicit() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            task = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-revision-001"},
                json=_task_payload(project),
            ).json()
            response = client.post(
                f"/api/tasks/{task['id']}/drive",
                headers={"Idempotency-Key": "drive-revision-001"},
                json={"expected_revision": 99},
            )

        assert response.status_code == 409
        assert response.json()["code"] == "revision_conflict"


def test_task_validation_rejects_missing_project_before_run() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        payload = _task_payload(Path(directory))
        payload["project_path"] = str(Path(directory) / "missing")
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-invalid-path"},
                json=payload,
            )

        assert response.status_code == 422
        assert app.state.task_repository.list() == []
