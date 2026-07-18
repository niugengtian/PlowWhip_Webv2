from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.api.schemas import TaskView
from plow_whip_web.config import Settings


def test_one_immutable_spec_binds_create_context_claim_retry_and_view() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        repository = app.state.task_repository
        task = repository.create(
            title="immutable spec",
            objective="ship the declared artifact",
            project_path=str(project),
            command={"argv": ["python3", "-c", "pass"], "timeout_seconds": 90},
            verification=[{"kind": "file_exists", "path": "result.txt"}],
            max_attempts=2,
            idempotency_key="task-spec-create",
            scope=["backend", "migration"],
            acceptance=["upgrade_keeps_terminal_state"],
            artifacts=["result.txt"],
            constraints=["no_dirty_diff_copy"],
            deadline={"hard_seconds": 90},
        )

        assert task.spec_revision == 1
        assert set(task.spec) == {
            "objective", "scope", "acceptance", "verification", "artifacts",
            "constraints", "deadline",
        }
        assert TaskView.from_record(task).spec == task.spec

        # Compatibility columns are projections only; every consumer reads task_specs.
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET objective = 'stale projection', verification_json = '[]' WHERE id = ?",
                (task.id,),
            )
        reloaded = repository.get(task.id)
        assert reloaded.objective == "ship the declared artifact"
        assert reloaded.verification == [{"kind": "file_exists", "path": "result.txt"}]

        first_context = app.state.context_compiler.compile(task.id)
        assert first_context["spec_revision"] == 1
        assert json.dumps(task.spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")) in first_context["content"]

        retry = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="task-spec-run-1",
        )

        retry_context_a = app.state.context_compiler.compile(task.id)
        retry_context_b = app.state.context_compiler.compile(task.id)
        assert retry_context_a["content_hash"] == retry_context_b["content_hash"]
        assert retry_context_a["content"] == retry_context_b["content"]

        second = repository.claim(
            task.id, expected_revision=retry.revision, idempotency_key="task-spec-claim-2"
        )
        connection = app.state.database.connect()
        try:
            bindings = connection.execute(
                """
                SELECT spec_revision FROM task_attempts WHERE task_id = ?
                UNION ALL
                SELECT spec_revision FROM task_runs WHERE task_id = ?
                """,
                (task.id, task.id),
            ).fetchall()
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    "UPDATE task_specs SET spec_json = '{}' WHERE task_id = ?",
                    (task.id,),
                )
        finally:
            connection.close()
        assert second.task.spec_revision == 1
        assert {row["spec_revision"] for row in bindings} == {1}
