from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.providers.generic_command import GenericCommandProvider
from plow_whip_web.runtime.verification import VerificationEngine


def _payload(project: Path, *, provider: str = "generic-command", quality: str = "balanced"):
    return {
        "title": f"{quality} delivery", "objective": "complete with evidence",
        "project_path": str(project), "provider": provider, "quality_profile": quality,
        "command": {"argv": [sys.executable, "-c", "from pathlib import Path; Path('done').write_text('ok')"]},
        "verification": [{"kind": "file_contains", "path": "done", "contains": "ok"}],
    }


def test_non_loopback_refuses_start_without_token() -> None:
    with TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="non-loopback binding requires"):
            create_app(Settings(data_dir=Path(directory), bind_host="0.0.0.0"))


def test_non_loopback_requires_bearer_and_rejects_cross_origin() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory), bind_host="0.0.0.0", api_token="local-secret"))
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/health").status_code == 401
            headers = {"Authorization": "Bearer local-secret"}
            assert client.get("/health", headers=headers).status_code == 200
            rejected = client.post(
                "/api/scheduler/tick",
                headers={**headers, "Origin": "https://evil.example"},
            )
            assert rejected.status_code == 403


def test_cross_project_absolute_argument_is_rejected_before_claim() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        outside = root / "outside.txt"
        project.mkdir()
        outside.write_text("secret")
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="escape", objective="must be denied", project_path=str(project),
            command={"argv": [sys.executable, str(outside)]},
            verification=[{"kind": "exit_code", "expected": 0}], max_attempts=1,
            token_budget=0, idempotency_key="escape-create",
        )
        with TestClient(app) as client:
            response = client.post(
                f"/api/tasks/{task.id}/drive", headers={"Idempotency-Key": "escape-drive"},
                json={"expected_revision": 0},
            )
        assert response.status_code == 403
        assert response.json()["code"] == "policy_violation"
        unchanged = app.state.task_repository.get(task.id)
        assert unchanged.status.value == "ready" and unchanged.attempts_used == 0


def test_provider_missing_blocks_without_fake_completion() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            task = client.post(
                "/api/tasks", headers={"Idempotency-Key": "missing-provider-create"},
                json=_payload(project, provider="codex"),
            ).json()
            response = client.post(
                f"/api/tasks/{task['id']}/drive", headers={"Idempotency-Key": "missing-provider-drive"},
                json={"expected_revision": 0},
            )
        assert response.status_code == 409
        assert response.json()["code"] == "provider_unavailable"
        current = app.state.task_repository.get(task["id"])
        assert current.status.value == "ready" and current.attempts_used == 0


def test_provider_output_redacts_secrets_and_environment_is_allowlisted() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        secret = "sk-abcdefghijklmnop123456"
        old = os.environ.get("PLOW_WHIP_TEST_SECRET")
        os.environ["PLOW_WHIP_TEST_SECRET"] = "must-not-pass"
        try:
            result = GenericCommandProvider().execute(root, {
                "argv": [
                    sys.executable, "-c",
                    f"import os; print('{secret}'); print(os.environ.get('PLOW_WHIP_TEST_SECRET', 'absent'))",
                ]
            })
        finally:
            if old is None:
                os.environ.pop("PLOW_WHIP_TEST_SECRET", None)
            else:
                os.environ["PLOW_WHIP_TEST_SECRET"] = old
        assert secret not in result.stdout
        assert "[REDACTED]" in result.stdout
        assert "absent" in result.stdout


def test_permissions_and_mutations_are_audited_without_secret_payloads() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            created = client.post("/api/permissions", json={
                "project_id": None, "capability": "secret_reference", "resource": "*",
                "decision": "deny", "reason": "not approved",
            })
            assert created.status_code == 200
            grant = created.json()
            assert client.post(f"/api/permissions/{grant['id']}/revoke").json()["revoked"] is True
            audit = client.get("/api/audit").json()
        paths = {item["path"] for item in audit}
        assert "/api/permissions" in paths
        assert f"/api/permissions/{grant['id']}/revoke" in paths
        assert "not approved" not in json.dumps(audit)


def test_backup_export_diagnostics_and_restore_round_trip() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        first_path, second_path = root / "first", root / "second"
        first_path.mkdir(); second_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.project_repository.create(name="first", path=str(first_path))
        backup = app.state.maintenance.backup()
        app.state.project_repository.create(name="second", path=str(second_path))
        exported = app.state.maintenance.export_metadata()
        diagnostics = app.state.maintenance.diagnostics()
        assert exported["secrets_included"] is False
        assert len(exported["projects"]) == 2
        archive = root / "runtime" / "diagnostics" / diagnostics["filename"]
        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {"health.json", "settings.json", "providers.json", "metadata.json"}
        restored = app.state.maintenance.restore_backup(backup["filename"])
        assert restored["integrity"] == "ok"
        assert [project["name"] for project in app.state.project_repository.list()] == ["first"]


@pytest.mark.parametrize("quality", ["fast", "balanced", "strict"])
def test_legacy_quality_profiles_use_one_deterministic_execute(quality: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        original_verify = VerificationEngine.verify
        with TestClient(app) as client:
            task = client.post(
                "/api/tasks", headers={"Idempotency-Key": f"quality-create-{quality}"},
                json=_payload(project, quality=quality),
            ).json()
            assert task["quality_profile"] == "deterministic"
            assert client.get(f"/api/tasks/{task['id']}").json()["quality_profile"] == "deterministic"
            with patch.object(
                VerificationEngine, "verify", autospec=True, side_effect=original_verify
            ) as verify:
                completed = client.post(
                    f"/api/tasks/{task['id']}/drive",
                    headers={"Idempotency-Key": f"quality-drive-{quality}"},
                    json={"expected_revision": 0},
                )
                assert completed.json()["status"] == "completed"
                assert completed.json()["quality_profile"] == "deterministic"
                assert verify.call_count == 1

                legacy = app.state.task_repository.create(
                    title=f"stored {quality}", objective="legacy compatibility row",
                    project_path=str(project),
                    command=_payload(project)["command"],
                    verification=_payload(project)["verification"],
                    max_attempts=1, token_budget=0,
                    idempotency_key=f"stored-quality-create-{quality}",
                    quality_profile=quality,
                )
                assert legacy.quality_profile == quality
                legacy_completed = app.state.task_service.drive(
                    legacy.id, expected_revision=legacy.revision,
                    idempotency_key=f"stored-quality-drive-{quality}",
                )
                assert legacy_completed.status.value == "completed"
                assert verify.call_count == 2
        connection = app.state.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT task_id, run_type, result_json FROM task_runs
                WHERE task_id IN (?, ?) ORDER BY task_id, finished_at
                """,
                (task["id"], legacy.id),
            ).fetchall()
        finally:
            connection.close()
        assert [row["run_type"] for row in rows if row["task_id"] == task["id"]] == ["execute"]
        assert [row["run_type"] for row in rows if row["task_id"] == legacy.id] == ["execute"]
        assert all('"model_tokens"' not in row["result_json"] for row in rows)


def test_openapi_and_provider_capabilities_are_complete() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        with TestClient(app) as client:
            schema = client.get("/openapi.json").json()
            providers = client.get("/api/providers").json()
        assert "/api/tasks/{task_id}/control" in schema["paths"]
        assert "/api/maintenance/backup" in schema["paths"]
        generic = next(item for item in providers if item["name"] == "generic-command")
        codex = next(item for item in providers if item["name"] == "codex")
        assert generic["status"] == "available" and generic["model_invoked"] is False
        assert codex["status"] == "unknown" and codex["reason"]
        assert codex["transport"] == "host-bridge"
