from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.runtime.evidence import build_evidence_manifest, snapshot_environment
from plow_whip_web.runtime.orchestration import GoalPlan, PlannedWorkItem
from plow_whip_web.runtime.verification import VerificationEngine
from plow_whip_web.store.task_repository import _validate_evidence_manifest


def _verification_goal(app: object, project_path: Path, verdict: str) -> dict:
    project = app.state.project_repository.create(
        name=f"verdict-{verdict.lower()}", path=str(project_path)
    )
    plan = GoalPlan(
        status="planned",
        missing_gates=(),
        rationale=("independent verification",),
        items=(
            PlannedWorkItem(
                1,
                "verification",
                "verification",
                "independent verification",
                "emit the canonical verification verdict",
                (),
            ),
        ),
    )
    return app.state.goal_repository.create_with_plan(
        title="verification contract",
        objective="complete only from a canonical PASS",
        project_id=project["id"],
        project_path=str(project_path),
        provider="generic-command",
        plan=plan,
        sizing_inputs={
            "layers_touched": 1,
            "components_touched": 1,
            "estimated_files_changed": 0,
            "has_migration": False,
            "has_deploy": False,
            "verification_commands_count": 1,
            "estimated_verification_seconds": 10,
            "external_dependencies_count": 0,
            "risk_level": "low",
            "independent_review_required": True,
            "gate_artifact": True,
            "gate_boundary": True,
            "gate_verification": True,
            "gate_dependency": True,
        },
        verification=[{"kind": "exit_code", "expected": 0}],
        scope=["verification"],
        acceptance=["canonical_verdict_contract"],
        artifacts=[],
        constraints=["independent_task_session"],
        deadline={"hard_seconds": 60},
        idempotency_key=f"verification-goal-{verdict.lower()}",
        command={
            "argv": [
                sys.executable,
                "-c",
                f"print('{{\"verdict\":\"{verdict}\"}}')",
            ],
            "timeout_seconds": 60,
        },
    )


def test_changes_required_with_returncode_zero_never_completes_task_or_goal() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _verification_goal(app, project, "CHANGES_REQUIRED")
        task = app.state.task_repository.get(goal["work_items"][0]["id"])

        result = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="changes-required-returncode-zero",
        )
        app.state.goal_repository.advance()
        current_goal = app.state.goal_repository.get(goal["id"])

        assert result.status.value != "completed"
        assert result.completed_at is None
        assert result.evidence_manifest is not None
        assert result.evidence_manifest["passed"] is False
        assert result.evidence_manifest["verdict"] == "CHANGES_REQUIRED"
        assert "MODEL_TEXT_CHANGES_REQUIRED" in result.evidence_manifest["reason_codes"]
        assert result.evidence_manifest["failed_acceptance_ids"] == (
            result.evidence_manifest["required_acceptance_ids"]
        )
        assert current_goal["status"] != "completed"
        assert current_goal["completed_at"] is None


def test_pass_with_all_gates_completes_task_and_goal_consistently() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _verification_goal(app, project, "PASS")
        task = app.state.task_repository.get(goal["work_items"][0]["id"])

        completed = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key="pass-all-verification-gates",
        )
        app.state.goal_repository.advance()
        current_goal = app.state.goal_repository.get(goal["id"])

        assert completed.status.value == "completed"
        assert completed.work_item_kind == "verification"
        assert completed.completed_at is not None
        assert completed.evidence_manifest is not None
        assert completed.evidence_manifest["passed"] is True
        assert completed.evidence_manifest["verdict"] == "PASS"
        assert completed.evidence_manifest["reason_codes"] == []
        gate = completed.evidence_manifest["verification_commands"][0]
        assert gate["acceptance_id"]
        assert gate["argv"] == task.command["argv"]
        assert gate["cwd"] == str(project)
        assert gate["started_at"] and gate["finished_at"]
        assert gate["exit_code"] == 0
        assert gate["run_id"] == completed.evidence_manifest["run_id"]
        assert current_goal["status"] == "completed"
        assert current_goal["completed_at"] is not None

        instances = app.state.role_instance_repository.list_instances(
            task_id=task.id
        )
        bindings = app.state.role_instance_repository.list_bindings(
            project_id=goal["project_id"], status="archived"
        )
        assert instances[0]["role_kind"] == "verification"
        assert bindings[0]["task_id"] == task.id
        assert bindings[0]["session_generation"] == 1


def test_command_gate_records_its_real_argv_cwd_exit_and_output_hashes() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        command = [sys.executable, "-c", "print('gate-ok')"]
        result = VerificationEngine().verify(
            project,
            ExecutionResult(0, "", "", 1),
            [{
                "kind": "command",
                "argv": command,
                "cwd": "",
                "expected": 0,
                "timeout_seconds": 30,
            }],
            acceptance=["real command gate"],
        )

        assert result.verdict == "PASS"
        check = result.checks[0]
        assert check["argv"] == command
        assert Path(check["cwd"]).resolve() == project.resolve()
        assert check["actual"] == 0
        assert check["started_at"] and check["finished_at"]
        assert check["stdout_bytes"] > 0
        assert len(check["stdout_sha256"]) == 64


@pytest.mark.parametrize("missing", ["argv", "acceptance_id"])
def test_manifest_missing_required_command_evidence_is_rejected(missing: str) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        goal = _verification_goal(app, project, "PASS")
        task = app.state.task_repository.get(goal["work_items"][0]["id"])
        completed = app.state.task_service.drive(
            task.id,
            expected_revision=task.revision,
            idempotency_key=f"manifest-required-{missing}",
        )
        manifest = dict(completed.evidence_manifest or {})
        manifest["verification_commands"] = [
            dict(item) for item in manifest["verification_commands"]
        ]
        manifest["verification_commands"][0].pop(missing)

        with pytest.raises(DomainError, match="command evidence"):
            _validate_evidence_manifest(
                completed,
                manifest,
                {"returncode": 0},
            )


def test_artifact_inheritance_is_scoped_to_same_task_and_session_generation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        artifact = project / "proof.json"
        artifact.write_text('{"verified":true}', encoding="utf-8")
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="artifact continuity",
            objective="reuse a verified unchanged artifact",
            project_path=str(project),
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 30},
            verification=[
                {"kind": "exit_code", "expected": 0},
                {"kind": "file_exists", "path": "proof.json"},
            ],
            acceptance=["artifact_hash_continuity"],
            artifacts=["proof.json"],
            max_attempts=1,
            idempotency_key="artifact-continuity-task",
        )
        baseline = snapshot_environment(project, ["proof.json"])
        after = snapshot_environment(project, ["proof.json"])
        execution = ExecutionResult(0, "", "", 1)
        verification = VerificationEngine().verify(
            project,
            execution,
            task.verification,
            acceptance=task.spec["acceptance"],
        )
        sha256 = after["artifacts"][0]["sha256"]
        inherited = [{
            "relative_path": "proof.json",
            "sha256": sha256,
            "task_id": task.id,
            "session_generation": 3,
            "manifest_hash": "a" * 64,
            "spec_revision": 1,
            "run_id": "prior-run",
        }]
        context = {
            "argv": task.command["argv"],
            "cwd": str(project),
            "started_at": baseline["captured_at"],
            "finished_at": after["captured_at"],
            "session_generation": 3,
        }

        same = build_evidence_manifest(
            task=task,
            attempt_id="attempt",
            run_id="run",
            call_id="run",
            task_revision=1,
            baseline=baseline,
            after=after,
            execution=execution,
            verification=verification,
            execution_context=context,
            inherited_artifacts=inherited,
        )
        new_generation = build_evidence_manifest(
            task=task,
            attempt_id="attempt",
            run_id="run-2",
            call_id="run-2",
            task_revision=1,
            baseline=baseline,
            after=after,
            execution=execution,
            verification=verification,
            execution_context={**context, "session_generation": 4},
            inherited_artifacts=inherited,
        )
        other_task = build_evidence_manifest(
            task=replace(task, id="another-task"),
            attempt_id="attempt",
            run_id="run-3",
            call_id="run-3",
            task_revision=1,
            baseline=baseline,
            after=after,
            execution=execution,
            verification=verification,
            execution_context=context,
            inherited_artifacts=inherited,
        )

        assert same["passed"] is True
        assert same["artifacts"][0]["provenance"] == "same_task_session_generation"
        assert new_generation["passed"] is False
        assert other_task["passed"] is False


def test_watchdog_run_inherits_hash_from_same_generation_execution_baseline() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        task = app.state.task_repository.create(
            title="watchdog artifact continuity",
            objective="preserve verified artifact across episode runs",
            project_path=str(project),
            command={"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 30},
            verification=[{"kind": "file_exists", "path": "proof.json"}],
            acceptance=["artifact_hash_continuity"],
            artifacts=["proof.json"],
            max_attempts=2,
            idempotency_key="watchdog-artifact-task",
        )
        claim = app.state.task_repository.claim(
            task.id,
            expected_revision=task.revision,
            idempotency_key="watchdog-artifact-claim",
        )
        assert claim.attempt_id and claim.run_id
        baseline = snapshot_environment(project, ["proof.json"])
        baseline["environment"] = {"session_generation": 1}
        app.state.task_repository.record_evidence_baseline(
            task_id=task.id,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            spec_revision=task.spec_revision,
            baseline=baseline,
        )
        (project / "proof.json").write_text('{"verified":true}', encoding="utf-8")
        current = snapshot_environment(project, ["proof.json"])

        inherited = app.state.task_repository.inheritable_artifacts(
            task.id,
            session_generation=1,
            spec_revision=task.spec_revision,
            current_artifacts=current["artifacts"],
        )
        rejected = app.state.task_repository.inheritable_artifacts(
            task.id,
            session_generation=2,
            spec_revision=task.spec_revision,
            current_artifacts=current["artifacts"],
        )

        assert inherited[0]["sha256"] == current["artifacts"][0]["sha256"]
        assert inherited[0]["baseline_hash"]
        assert inherited[0]["run_id"] == claim.run_id
        assert rejected == []
