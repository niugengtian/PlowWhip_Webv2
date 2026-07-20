from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.orchestration import GoalPlan, PlannedWorkItem


def _goal(app, project_path: Path, *, include_verifier: bool = False):
    project = app.state.project_repository.create(
        name="goal-reducer", path=str(project_path)
    )
    plan = GoalPlan(
        status="planned",
        missing_gates=(),
        rationale=("test",),
        route="capability-milestones",
        items=(
            PlannedWorkItem(1, "capability", "implementation", "one", "one", ()),
            PlannedWorkItem(2, "capability", "implementation", "two", "two", (1,)),
            *(
                (
                    PlannedWorkItem(
                        3, "verification", "verification",
                        "verify", "verify all completed implementation", (2,),
                    ),
                )
                if include_verifier else ()
            ),
        ),
    )
    return app.state.goal_repository.create_with_plan(
        title="pure reducer",
        objective="derive Goal only from immutable facts",
        project_id=project["id"],
        project_path=str(project_path),
        provider="generic-command",
        plan=plan,
        sizing_inputs={
            "layers_touched": 1,
            "components_touched": 2,
            "estimated_files_changed": 2,
            "has_migration": False,
            "has_deploy": False,
            "verification_commands_count": 1,
            "estimated_verification_seconds": 30,
            "external_dependencies_count": 0,
            "risk_level": "low",
            "independent_review_required": False,
            "gate_artifact": True,
            "gate_boundary": True,
            "gate_verification": True,
            "gate_dependency": True,
        },
        verification=[{"kind": "exit_code", "expected": 0}],
        scope=["backend"],
        acceptance=["facts_recompute"],
        artifacts=[],
        constraints=[],
        deadline={"hard_seconds": 60},
        idempotency_key="goal-reducer-create",
        command={
            "argv": [sys.executable, "-c", "print('{\"verdict\":\"PASS\"}')"],
            "timeout_seconds": 60,
        },
    )


def _event_count(app, goal_id: str) -> int:
    connection = app.state.database.connect()
    try:
        return connection.execute(
            """
            SELECT COUNT(*) FROM task_events e
            JOIN tasks t ON t.id = e.task_id WHERE t.goal_id = ?
            """,
            (goal_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def test_terminal_goal_cancels_amended_pending_children_and_tick_is_idempotent() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path)
        first = app.state.task_repository.get(goal["work_items"][0]["id"])
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET status = 'terminal_failed' WHERE id = ?", (first.id,)
            )
            connection.execute(
                "UPDATE goals SET status = 'terminal_failed' WHERE id = ?", (goal["id"],)
            )

        amended_spec = {**first.spec, "objective": "amended implementation"}
        amended = app.state.task_repository.amend_spec(
            first.id,
            spec=amended_spec,
            reason="repair failed acceptance",
            expected_revision=first.revision,
            idempotency_key="amend-terminal-child",
        )
        assert amended.status.value == "ready"
        assert amended.spec_revision == 2
        assert amended.last_evidence_hash is None

        result = app.state.goal_repository.advance()
        event_count = _event_count(app, goal["id"])
        again = app.state.goal_repository.advance()

        assert app.state.goal_repository.get(goal["id"])["status"] == "terminal_failed"
        assert app.state.task_repository.get(first.id).status.value == "cancelled"
        assert app.state.task_repository.get(first.id).last_error == "parent_goal_terminal"
        assert result["completed_goals"] == []
        assert again["unblocked"] == []
        assert _event_count(app, goal["id"]) == event_count


def test_resume_and_restart_cannot_bypass_dependencies() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path)
        first = app.state.task_repository.get(goal["work_items"][0]["id"])
        second = app.state.task_repository.get(goal["work_items"][1]["id"])

        resumed = app.state.task_repository.control(
            second.id,
            action="resume",
            reason="must remain dependency gated",
            expected_revision=second.revision,
            idempotency_key="resume-dependent",
        )
        assert resumed.status.value == "paused"
        assert resumed.blocked_reason == f"waiting_on:{first.id}"

        cancelled = app.state.task_repository.control(
            first.id,
            action="cancel",
            reason="replace child",
            expected_revision=first.revision,
            idempotency_key="cancel-first-child",
        )
        app.state.goal_repository.advance()
        assert app.state.goal_repository.get(goal["id"])["status"] == "cancelled"

        restarted = app.state.task_repository.control(
            first.id,
            action="restart",
            reason="replacement ready",
            expected_revision=cancelled.revision,
            idempotency_key="restart-first-child",
        )
        app.state.goal_repository.advance()
        assert restarted.status.value == "ready"
        assert restarted.spec_revision == 2
        assert app.state.task_repository.get(first.id).status.value == "cancelled"
        assert app.state.task_repository.get(second.id).status.value == "cancelled"
        assert app.state.goal_repository.get(goal["id"])["status"] == "cancelled"


def test_all_goals_recompute_and_legacy_parent_projection_is_cleared() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path)
        first_id = goal["work_items"][0]["id"]
        second_id = goal["work_items"][1]["id"]
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE goals SET status = 'completed', parent_task_id = ? WHERE id = ?",
                (first_id, goal["id"]),
            )
            connection.execute(
                "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (first_id, second_id)
            )

        app.state.goal_repository.advance()
        current = app.state.goal_repository.get(goal["id"])

        assert current["status"] == "running"
        assert current["parent_task_id"] is None
        assert all(item["parent_task_id"] is None for item in current["work_items"])


def test_missing_spec_or_completion_evidence_requires_human() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path)
        first_id = goal["work_items"][0]["id"]
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE tasks SET current_spec_revision = 99 WHERE id = ?", (first_id,)
            )

        result = app.state.goal_repository.advance()

        connection = app.state.database.connect()
        try:
            status = connection.execute(
                "SELECT status FROM goals WHERE id = ?", (goal["id"],)
            ).fetchone()[0]
        finally:
            connection.close()
        assert status == "needs_human"
        assert result["blocked_goals"] == [{
            "goal_id": goal["id"], "reason": "task_spec_missing_or_corrupt",
        }]

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE tasks SET current_spec_revision = 1, status = 'completed',
                    last_evidence_hash = ? WHERE id = ?
                """,
                ("f" * 64, first_id),
            )
        evidence_result = app.state.goal_repository.advance()
        assert evidence_result["blocked_goals"] == [{
            "goal_id": goal["id"], "reason": "evidence_manifest_missing",
        }]


def test_aggregate_verifier_can_attest_legacy_completed_dependencies() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path, include_verifier=True)
        first_id, second_id, verifier_id = [
            item["id"] for item in goal["work_items"]
        ]
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE tasks SET status = 'completed', last_evidence_hash = NULL
                WHERE id IN (?, ?)
                """,
                (first_id, second_id),
            )

        pending = app.state.goal_repository.advance()
        verifier = app.state.task_repository.get(verifier_id)

        assert pending["blocked_goals"] == []
        assert verifier.status.value == "ready"
        assert verifier.handoff is None
        assert app.state.goal_repository.get(goal["id"])["status"] == "running"

        completed_verifier = app.state.task_service.drive(
            verifier.id,
            expected_revision=verifier.revision,
            idempotency_key="aggregate-verifier-drive",
        )
        settled = app.state.goal_repository.advance()

        assert completed_verifier.status.value == "completed"
        assert completed_verifier.evidence_manifest is not None
        assert settled["completed_goals"] == [goal["id"]]
        assert app.state.goal_repository.get(goal["id"])["status"] == "completed"


def test_episode_exhaustion_replans_once_then_converges_without_job_loop() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _goal(app, project_path)
        first_id = goal["work_items"][0]["id"]
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE tasks SET status = 'needs_human',
                    last_error = 'execution_episode_circuit_open:host_processes'
                WHERE id = ?
                """,
                (first_id,),
            )
            connection.execute(
                "UPDATE goals SET status = 'terminal_failed' WHERE id = ?", (goal["id"],)
            )

        first = app.state.goal_repository.advance()
        recovered = app.state.task_repository.get(first_id)
        assert first["replanned"] == [first_id]
        assert recovered.status.value == "cancelled"
        assert recovered.last_error == "parent_goal_terminal"
        assert recovered.spec_revision == 2
        assert app.state.goal_repository.get(goal["id"])["status"] == "terminal_failed"

        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE tasks SET status = 'needs_human',
                    last_error = 'execution_episode_circuit_open:host_processes'
                WHERE id = ?
                """,
                (first_id,),
            )
        second = app.state.goal_repository.advance()
        event_count = _event_count(app, goal["id"])
        app.state.goal_repository.advance()

        assert second["replanned"] == []
        assert app.state.task_repository.get(first_id).status.value == "terminal_failed"
        assert app.state.goal_repository.get(goal["id"])["status"] == "terminal_failed"
        assert _event_count(app, goal["id"]) == event_count
