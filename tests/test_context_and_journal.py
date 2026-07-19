from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.journal import SessionJournal


def test_context_compiler_layers_global_project_and_task_rules() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="context", path=str(project_path)
        )
        task = app.state.task_repository.create(
            title="context", objective="bounded context",
            project_path=str(project_path), project_id=project["id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=0, idempotency_key="context-create",
        )
        app.state.conventions.put(
            scope="global", scope_id="global", content="GLOBAL", expected_revision=0
        )
        app.state.conventions.put(
            scope="project", scope_id=project["id"], content="PROJECT",
            expected_revision=0,
        )
        app.state.conventions.put(
            scope="task", scope_id=task.id, content="TASK", expected_revision=0
        )
        compiled = app.state.context_compiler.compile(task.id)
        assert all(value in compiled["content"] for value in ("GLOBAL", "PROJECT", "TASK"))


def test_session_journal_rotates_without_changing_provider_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        journal = SessionJournal(root / "runtime", app.state.runtime_settings)
        for index in range(20):
            journal.append("worker-journal", {"index": index, "payload": "x" * 256})
        archive = journal.rotate_current("worker-journal")
        assert archive is not None
        assert (root / "runtime" / "sessions" / "worker-journal" / archive["archive"]).exists()
        assert journal.current_bytes("worker-journal") == 0


def test_context_limits_follow_task_project_global_precedence_and_report_sources() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="continuity", path=str(project_path)
        )
        task = app.state.task_repository.create(
            title="continuity", objective="bounded replacement",
            project_path=str(project_path), project_id=project["id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=0, idempotency_key="continuity-create",
        )
        app.state.conventions.put(
            scope="project", scope_id=project["id"], expected_revision=0,
            content='Continuity-Limits: {"handoff_max_bytes":8192}',
        )
        app.state.conventions.put(
            scope="task", scope_id=task.id, expected_revision=0,
            content=(
                'Continuity-Limits: {"handoff_max_bytes":6144,'
                '"observation_tail_lines":12}'
            ),
        )

        compiled = app.state.context_compiler.compile(task.id)

        assert compiled["effective_limits"]["handoff_max_bytes"] == {
            "value": 6144,
            "source": f"task_convention:{task.id}@1",
        }
        assert compiled["effective_limits"]["observation_tail_lines"]["value"] == 12
        assert compiled["effective_limits"]["checkpoint_max_bytes"] == {
            "value": 4096,
            "source": "global_setting",
        }
        assert "Effective continuity limits" in compiled["content"]
