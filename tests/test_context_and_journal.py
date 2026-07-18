from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.journal import SessionJournal


def test_context_compiler_layers_global_project_and_task_rules() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project_record = app.state.project_repository.create(
            name="context", path=str(project)
        )
        task = app.state.task_repository.create(
            title="context",
            objective="compile bounded context",
            project_path=str(project),
            project_id=project_record["id"],
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="context-create",
        )
        app.state.conventions.put(
            scope="global", scope_id="global", content="GLOBAL", expected_revision=0
        )
        app.state.conventions.put(
            scope="project", scope_id=project_record["id"], content="PROJECT",
            expected_revision=0,
        )
        app.state.conventions.put(
            scope="task", scope_id=task.id, content="TASK", expected_revision=0
        )
        compiled = app.state.context_compiler.compile(task.id)
        assert "GLOBAL" in compiled["content"]
        assert "PROJECT" in compiled["content"]
        assert "TASK" in compiled["content"]
        assert compiled["content_hash"]


def test_session_journal_rotates_without_changing_provider_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        journal = SessionJournal(root / "runtime", app.state.runtime_settings)
        worker_id = "worker-journal"
        for index in range(20):
            journal.append(worker_id, {"index": index, "payload": "x" * 256})
        before = journal.current_bytes(worker_id)
        archive = journal.rotate_current(worker_id)
        assert before > 0
        assert archive is not None
        assert (root / "runtime" / "sessions" / worker_id / archive["archive"]).exists()
        assert journal.current_bytes(worker_id) == 0
