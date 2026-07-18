from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def _create_task(app: object, project: Path, *, command: str, key: str):
    return app.state.task_repository.create(
        title="evidence manifest",
        objective="produce the declared release evidence",
        project_path=str(project),
        command={"argv": [sys.executable, "-c", command], "timeout_seconds": 30},
        verification=[
            {"kind": "exit_code", "expected": 0},
            {
                "kind": "file_contains",
                "path": "release-evidence.json",
                "contains": '"status":"ok"',
            },
        ],
        artifacts=["release-evidence.json"],
        acceptance=["manifest_bound_completion"],
        max_attempts=1,
        idempotency_key=key,
    )


def test_completion_persists_one_immutable_bound_evidence_manifest() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = _create_task(
            app,
            project,
            command=(
                "from pathlib import Path; "
                "Path('release-evidence.json').write_text('{\"status\":\"ok\"}')"
            ),
            key="manifest-create",
        )

        completed = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="manifest-drive",
        )

        assert completed.status.value == "completed"
        manifest = completed.evidence_manifest
        assert manifest is not None
        assert manifest["passed"] is True
        assert manifest["task_id"] == task.id
        assert manifest["call_id"] == manifest["run_id"]
        assert manifest["spec_revision"] == task.spec_revision
        assert manifest["task_revision"] == completed.revision - 1
        assert len(manifest["environment_hash"]) == 64
        assert manifest["verification_commands"][0]["exit_code"] == 0
        assert manifest["test_report"]["checks_total"] == 2
        artifact = manifest["artifacts"][0]
        assert artifact["before"]["sha256"] is None
        assert len(artifact["after"]["sha256"]) == 64
        assert artifact["produced_by_run"] is True
        assert "before" in manifest["git_diff_summary"]
        assert "after" in manifest["git_diff_summary"]
        assert completed.last_evidence_hash == manifest["manifest_hash"]

        connection = app.state.database.connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM evidence_manifests WHERE task_id = ?",
                (task.id,),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    """
                    UPDATE evidence_manifests SET passed = 0
                    WHERE task_id = ?
                    """,
                    (task.id,),
                )
        finally:
            connection.close()
        assert count == 1


def test_preexisting_unchanged_file_cannot_impersonate_current_run_artifact() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        artifact = project / "release-evidence.json"
        artifact.write_text('{"status":"ok"}', encoding="utf-8")
        app = create_app(Settings(data_dir=root / "runtime"))
        task = _create_task(
            app,
            project,
            command="pass",
            key="stale-artifact-create",
        )

        failed = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="stale-artifact-drive",
        )

        assert failed.status.value == "terminal_failed"
        assert failed.evidence_manifest is not None
        assert failed.evidence_manifest["test_report"]["passed"] is True
        assert failed.evidence_manifest["artifact_contract_passed"] is False
        assert failed.evidence_manifest["artifacts"][0]["produced_by_run"] is False
        assert failed.last_error == (
            "artifact contract failed: not produced by this run: "
            "release-evidence.json"
        )
