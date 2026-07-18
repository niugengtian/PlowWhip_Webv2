from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.runtime.evidence import build_evidence_manifest, snapshot_environment
from plow_whip_web.runtime.verification import VerificationResult


def _sizing(**changes: object) -> TaskSizingInputs:
    values: dict[str, object] = {
        "layers_touched": 2,
        "components_touched": 3,
        "estimated_files_changed": 5,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 2,
        "estimated_verification_seconds": 120,
        "external_dependencies_count": 0,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    values.update(changes)
    return TaskSizingInputs(**values)  # type: ignore[arg-type]


def test_sizing_keeps_execution_policy_without_token_budget_fields() -> None:
    estimate = estimate_task_sizing(_sizing())
    assert estimate["status"] == "estimated"
    assert estimate["size_class"] in {"S", "M", "L"}
    assert estimate["hard_deadline_seconds"] > 0
    assert estimate["max_attempts"] > 0
    assert not {
        "estimated_input_tokens",
        "estimated_output_tokens",
        "total_token_hard_cap",
        "reserved_tokens",
    } & estimate.keys()


def test_missing_gate_still_blocks_without_allocating_tokens() -> None:
    estimate = estimate_task_sizing(_sizing(gate_artifact=False))
    assert estimate["status"] == "needs_planning"
    assert estimate["missing_gates"] == ["artifact"]
    assert estimate["model_invoked"] is False


def test_task_create_rejects_removed_token_budget_input() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "removed-token-budget-input"},
                json={
                    "title": "removed budget",
                    "objective": "run regardless of token consumption",
                    "project_path": str(project),
                    "provider": "generic-command",
                    "command": {"argv": ["python3", "-c", "print('ok')"]},
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "token_budget": 1,
                },
            )
        assert response.status_code == 422


def test_large_usage_completes_and_is_recorded_once() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        repository = app.state.task_repository
        task = repository.create(
            title="large usage",
            objective="complete by evidence",
            project_path=str(project),
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="large-usage-create",
        )
        claim = repository.claim(
            task.id,
            expected_revision=task.revision,
            idempotency_key="large-usage-claim",
        )
        verifying = repository.mark_verifying(
            task.id,
            expected_revision=claim.task.revision,
            idempotency_key="large-usage-verify",
        )
        execution = {
            "returncode": 0,
            "input_tokens": 3_589_597,
            "cached_input_tokens": 3_423_488,
            "output_tokens": 17_304,
        }
        baseline = snapshot_environment(project, [])
        repository.record_evidence_baseline(
            task_id=task.id,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            spec_revision=task.spec_revision,
            baseline=baseline,
        )
        execution_result = ExecutionResult(
            returncode=0,
            stdout="",
            stderr="",
            duration_ms=1,
            input_tokens=3_589_597,
            cached_input_tokens=3_423_488,
            output_tokens=17_304,
        )
        manifest = build_evidence_manifest(
            task=claim.task,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            call_id=claim.run_id,
            task_revision=verifying.revision,
            baseline=baseline,
            after=snapshot_environment(project, []),
            execution=execution_result,
            verification=VerificationResult(
                passed=True,
                checks=[{"kind": "exit_code", "passed": True}],
                evidence_hash="large-usage-proof",
                summary="passed",
            ),
        )
        finished = repository.finish(
            task.id,
            expected_revision=verifying.revision,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            execution=execution,
            evidence_manifest=manifest,
            idempotency_key="large-usage-finish",
        )
        replayed = repository.finish(
            task.id,
            expected_revision=verifying.revision,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            execution=execution,
            evidence_manifest=manifest,
            idempotency_key="large-usage-finish",
        )
        summary = app.state.token_ledger.summary()
        assert finished.status.value == "completed"
        assert finished.last_error is None
        assert finished.tokens_used == 3_606_901
        assert replayed.tokens_used == 3_606_901
        assert summary["total_tokens"] == 3_606_901
        assert len(summary["calls"]) == 1
