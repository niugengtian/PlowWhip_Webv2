from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError
from plow_whip_web.runtime.sizing import (
    TaskSizingInputs,
    clamp_total_token_hard_cap,
    estimate_task_sizing,
)
from plow_whip_web.store.database import Database
from plow_whip_web.store.task_repository import TaskRepository


def _ready_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "layers_touched": 3,
        "components_touched": 4,
        "estimated_files_changed": 5,
        "has_migration": True,
        "has_deploy": False,
        "verification_commands_count": 3,
        "estimated_verification_seconds": 120,
        "external_dependencies_count": 1,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    payload.update(overrides)
    return payload


def test_missing_dispatch_gates_return_needs_planning_without_budget() -> None:
    result = estimate_task_sizing(TaskSizingInputs(
        layers_touched=5,
        components_touched=6,
        estimated_files_changed=8,
        has_migration=True,
        has_deploy=True,
        verification_commands_count=4,
        estimated_verification_seconds=300,
        external_dependencies_count=2,
        risk_level="high",
        independent_review_required=False,
        gate_artifact=False,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))

    assert result["status"] == "needs_planning"
    assert result["missing_gates"] == ["artifact"]
    assert result["size_class"] is None
    assert result["reserved_tokens"] is None
    assert result["total_token_hard_cap"] is None
    assert result["soft_deadline_seconds"] is None
    assert result["model_invoked"] is False


def test_same_structured_inputs_produce_stable_output() -> None:
    inputs = TaskSizingInputs(**_ready_payload())  # type: ignore[arg-type]
    first = estimate_task_sizing(inputs)
    second = estimate_task_sizing(inputs)

    assert first == second
    assert first["status"] == "estimated"
    assert first["bootstrap_version"] == second["bootstrap_version"]


def test_independent_review_requires_orchestration_without_allocating_budget() -> None:
    inputs = TaskSizingInputs(**_ready_payload(independent_review_required=True))  # type: ignore[arg-type]

    first = estimate_task_sizing(inputs)
    second = estimate_task_sizing(inputs)

    assert first == second
    assert first["status"] == "needs_planning"
    assert first["missing_gates"] == ["independent_review_orchestration"]
    assert first["size_class"] is None
    for field in (
        "estimated_input_tokens",
        "estimated_output_tokens",
        "soft_deadline_seconds",
        "hard_deadline_seconds",
        "max_turns",
        "max_attempts",
        "verification_timeout_seconds",
        "progress_extension_seconds",
        "total_token_hard_cap",
        "reserved_tokens",
    ):
        assert first[field] is None
    assert first["model_invoked"] is False


def test_medium_and_large_size_class_mapping() -> None:
    medium = estimate_task_sizing(TaskSizingInputs(
        layers_touched=3,
        components_touched=4,
        estimated_files_changed=5,
        has_migration=True,
        has_deploy=False,
        verification_commands_count=3,
        estimated_verification_seconds=120,
        external_dependencies_count=0,
        risk_level="low",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    large = estimate_task_sizing(TaskSizingInputs(
        layers_touched=5,
        components_touched=6,
        estimated_files_changed=8,
        has_migration=True,
        has_deploy=True,
        verification_commands_count=4,
        estimated_verification_seconds=180,
        external_dependencies_count=2,
        risk_level="medium",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))

    assert medium["size_class"] == "M"
    assert medium["soft_deadline_seconds"] == 480
    assert medium["hard_deadline_seconds"] == 1200
    assert medium["reserved_tokens"] == 150_000
    assert medium["total_token_hard_cap"] == 225_000

    assert large["size_class"] == "L"
    assert large["soft_deadline_seconds"] == 900
    assert large["hard_deadline_seconds"] == 2400
    assert large["reserved_tokens"] == 400_000
    assert large["total_token_hard_cap"] == 600_000


def test_total_token_hard_cap_clamps_to_minimum_and_maximum() -> None:
    assert clamp_total_token_hard_cap(10_000) == 25_000
    assert clamp_total_token_hard_cap(1_000_000) == 1_500_000

    xs = estimate_task_sizing(TaskSizingInputs(
        layers_touched=1,
        components_touched=1,
        estimated_files_changed=1,
        has_migration=False,
        has_deploy=False,
        verification_commands_count=1,
        estimated_verification_seconds=30,
        external_dependencies_count=0,
        risk_level="low",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    assert xs["size_class"] == "XS"
    assert xs["total_token_hard_cap"] == 37_500

    xl = estimate_task_sizing(TaskSizingInputs(
        layers_touched=8,
        components_touched=12,
        estimated_files_changed=20,
        has_migration=True,
        has_deploy=True,
        verification_commands_count=8,
        estimated_verification_seconds=900,
        external_dependencies_count=4,
        risk_level="high",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    assert xl["status"] == "estimated"
    assert xl["size_class"] == "XL"
    assert xl["total_token_hard_cap"] == 1_200_000


def test_rationale_lists_structured_weight_items() -> None:
    result = estimate_task_sizing(TaskSizingInputs(**_ready_payload()))  # type: ignore[arg-type]

    joined = "\n".join(result["rationale"])
    assert "layers_touched=3" in joined
    assert "components_touched=4" in joined
    assert "estimated_files_changed=5" in joined
    assert "has_migration=true" in joined
    assert "verification_commands_count=3" in joined
    assert "risk_level=medium" in joined
    assert "complexity_score=" in joined
    assert "size_class=" in joined


def test_estimate_api_is_deterministic_and_does_not_record_token_usage() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            connection = app.state.database.connect()
            try:
                usage_before = connection.execute(
                    "SELECT COUNT(*) FROM token_usage"
                ).fetchone()[0]
            finally:
                connection.close()

            payload = _ready_payload()
            first = client.post("/api/tasks/estimate", json=payload)
            second = client.post("/api/tasks/estimate", json=payload)

            assert first.status_code == 200
            assert second.status_code == 200
            assert first.json() == second.json()
            body = first.json()
            assert body["status"] == "estimated"
            assert body["model_invoked"] is False
            assert body["size_class"] == "M"
            assert body["reserved_tokens"] == 150_000

            connection = app.state.database.connect()
            try:
                usage_after = connection.execute(
                    "SELECT COUNT(*) FROM token_usage"
                ).fetchone()[0]
            finally:
                connection.close()
            assert usage_after == usage_before


def test_estimate_api_rejects_unstructured_title_fields() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks/estimate",
                json={**_ready_payload(), "title": "very long title", "objective": "wordy"},
            )
            assert response.status_code == 422


def test_all_four_gates_missing_lists_each_gate() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks/estimate",
                json=_ready_payload(
                    gate_artifact=False,
                    gate_boundary=False,
                    gate_verification=False,
                    gate_dependency=False,
                ),
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "needs_planning"
            assert body["missing_gates"] == [
                "artifact", "boundary", "verification", "dependency",
            ]
            assert body["reserved_tokens"] is None


def _repository(root: Path) -> TaskRepository:
    database = Database(root / "runtime" / "app.db")
    database.migrate()
    return TaskRepository(database)


def _estimated_sizing() -> dict[str, object]:
    preview = estimate_task_sizing(TaskSizingInputs(
        layers_touched=2,
        components_touched=3,
        estimated_files_changed=5,
        has_migration=True,
        has_deploy=False,
        verification_commands_count=3,
        estimated_verification_seconds=120,
        external_dependencies_count=1,
        risk_level="medium",
        independent_review_required=False,
        gate_artifact=True,
        gate_boundary=True,
        gate_verification=True,
        gate_dependency=True,
    ))
    assert preview["status"] == "estimated"
    assert preview["size_class"] == "M"
    return preview


def _split_preview(preview: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    sizing = {
        "status": preview["status"],
        "size_class": preview["size_class"],
        "rationale": preview["rationale"],
        "estimated_input_tokens": preview["estimated_input_tokens"],
        "estimated_output_tokens": preview["estimated_output_tokens"],
        "bootstrap_version": preview["bootstrap_version"],
    }
    execution_budget = {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
        "total_token_hard_cap": preview["total_token_hard_cap"],
        "reserved_tokens": preview["reserved_tokens"],
    }
    return sizing, execution_budget


def _create_kwargs(project: Path, key: str) -> dict[str, object]:
    return {
        "title": f"task-{key}",
        "objective": "perform bounded work",
        "project_path": str(project),
        "command": {"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        "verification": [{"kind": "exit_code", "expected": 0}],
        "max_attempts": 3,
        "token_budget": 225_000,
        "idempotency_key": key,
    }


def test_estimated_sizing_and_budget_round_trip_across_reopen() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)
        sizing, execution_budget = _split_preview(_estimated_sizing())
        assert execution_budget["reserved_tokens"] == 150_000
        assert execution_budget["soft_deadline_seconds"] == 480
        assert execution_budget["hard_deadline_seconds"] == 1200
        assert execution_budget["total_token_hard_cap"] == 225_000

        created = repository.create(
            **_create_kwargs(project, "sizing-round-trip"),
            sizing=sizing,
            execution_budget=execution_budget,
        )

        assert created.sizing == sizing
        assert created.execution_budget == execution_budget
        assert created.manual_override is False
        assert created.override_reason is None
        assert created.budget_overrun_evidence is None

        fetched = repository.get(created.id)
        listed = [task for task in repository.list() if task.id == created.id][0]
        assert fetched.sizing == sizing
        assert fetched.execution_budget == execution_budget
        assert listed.sizing == sizing
        assert listed.execution_budget == execution_budget

        reopened = TaskRepository(Database(root / "runtime" / "app.db")).get(created.id)
        assert reopened.sizing == sizing
        assert reopened.execution_budget == execution_budget
        assert reopened.manual_override is False
        assert reopened.budget_overrun_evidence is None


def test_idempotent_create_does_not_overwrite_original_sizing() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)
        sizing, execution_budget = _split_preview(_estimated_sizing())

        first = repository.create(
            **_create_kwargs(project, "sizing-idempotent"),
            sizing=sizing,
            execution_budget=execution_budget,
        )
        replay = repository.create(
            **_create_kwargs(project, "sizing-idempotent"),
        )

        assert replay.id == first.id
        assert replay.sizing == sizing
        assert replay.execution_budget == execution_budget


def test_legacy_create_call_is_marked_legacy_fallback() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)

        created = repository.create(**_create_kwargs(project, "legacy-call"))

        assert created.sizing == {"status": "legacy_fallback"}
        assert created.execution_budget is None
        assert created.manual_override is False
        assert created.override_reason is None
        assert created.budget_overrun_evidence is None

        event = repository.events(created.id)[0]
        assert event["event_type"] == "task.created"
        assert event["payload"]["sizing_status"] == "legacy_fallback"
        assert event["payload"]["size_class"] is None
        assert event["payload"]["manual_override"] is False


def test_created_event_carries_summary_not_full_json() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)
        sizing, execution_budget = _split_preview(_estimated_sizing())

        created = repository.create(
            **_create_kwargs(project, "sizing-event-summary"),
            sizing=sizing,
            execution_budget=execution_budget,
            manual_override=True,
            override_reason="operator raised hard cap after planning review",
        )

        payload = repository.events(created.id)[0]["payload"]
        assert payload["sizing_status"] == "estimated"
        assert payload["size_class"] == "M"
        assert payload["bootstrap_version"] == sizing["bootstrap_version"]
        assert payload["total_token_hard_cap"] == 225_000
        assert payload["hard_deadline_seconds"] == 1200
        assert payload["manual_override"] is True
        assert "rationale" not in payload
        assert "estimated_input_tokens" not in payload
        assert "reserved_tokens" not in payload


def test_manual_override_requires_non_empty_reason() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)
        sizing, execution_budget = _split_preview(_estimated_sizing())

        with pytest.raises(DomainError, match="override_reason"):
            repository.create(
                **_create_kwargs(project, "override-no-reason"),
                sizing=sizing,
                execution_budget=execution_budget,
                manual_override=True,
            )
        with pytest.raises(DomainError, match="override_reason"):
            repository.create(
                **_create_kwargs(project, "override-blank-reason"),
                sizing=sizing,
                execution_budget=execution_budget,
                manual_override=True,
                override_reason="   ",
            )
        with pytest.raises(DomainError, match="only allowed with manual_override"):
            repository.create(
                **_create_kwargs(project, "reason-without-override"),
                sizing=sizing,
                execution_budget=execution_budget,
                override_reason="not actually an override",
            )

        accepted = repository.create(
            **_create_kwargs(project, "override-with-reason"),
            sizing=sizing,
            execution_budget=execution_budget,
            manual_override=True,
            override_reason="operator raised hard cap after planning review",
        )
        assert accepted.manual_override is True
        assert accepted.override_reason == "operator raised hard cap after planning review"


def test_execution_budget_without_sizing_is_rejected() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        repository = _repository(root)
        _, execution_budget = _split_preview(_estimated_sizing())

        with pytest.raises(DomainError, match="explicit sizing"):
            repository.create(
                **_create_kwargs(project, "budget-without-sizing"),
                execution_budget=execution_budget,
            )


def _s_sizing_inputs() -> dict[str, object]:
    return {
        "layers_touched": 1,
        "components_touched": 2,
        "estimated_files_changed": 3,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 1,
        "estimated_verification_seconds": 60,
        "external_dependencies_count": 0,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }


def _m_sizing_inputs() -> dict[str, object]:
    return {
        "layers_touched": 2,
        "components_touched": 3,
        "estimated_files_changed": 4,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 3,
        "estimated_verification_seconds": 120,
        "external_dependencies_count": 1,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }


def _xl_sizing_inputs() -> dict[str, object]:
    return {
        "layers_touched": 8,
        "components_touched": 12,
        "estimated_files_changed": 20,
        "has_migration": True,
        "has_deploy": True,
        "verification_commands_count": 8,
        "estimated_verification_seconds": 900,
        "external_dependencies_count": 4,
        "risk_level": "high",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }


def _api_task_payload(project: Path, key: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": f"api-{key}",
        "objective": "perform bounded work",
        "project_path": str(project),
        "command": {"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 60},
        "verification": [{"kind": "exit_code", "expected": 0}],
        "max_attempts": 3,
    }
    payload.update(overrides)
    return payload


def test_create_with_sizing_inputs_matches_estimate_preview() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        sizing_inputs = _m_sizing_inputs()
        with TestClient(app) as client:
            preview = client.post("/api/tasks/estimate", json=sizing_inputs).json()
            assert preview["status"] == "estimated"
            assert preview["size_class"] == "M"
            assert preview["reserved_tokens"] == 150_000
            assert preview["soft_deadline_seconds"] == 480
            assert preview["hard_deadline_seconds"] == 1200
            assert preview["total_token_hard_cap"] == 225_000

            created = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-sized-task-001"},
                json=_api_task_payload(project, "sized", sizing_inputs=sizing_inputs),
            )
            assert created.status_code == 201
            body = created.json()
            assert body["quality_profile"] == "deterministic"
            assert body["token_budget"] == 225_000
            assert body["sizing"]["status"] == "estimated"
            assert body["sizing"]["size_class"] == "M"
            assert body["sizing"]["bootstrap_version"] == preview["bootstrap_version"]
            assert body["sizing"]["rationale"] == preview["rationale"]
            assert body["execution_budget"]["reserved_tokens"] == preview["reserved_tokens"]
            assert body["execution_budget"]["soft_deadline_seconds"] == preview["soft_deadline_seconds"]
            assert body["execution_budget"]["hard_deadline_seconds"] == preview["hard_deadline_seconds"]
            assert body["execution_budget"]["total_token_hard_cap"] == 225_000
            assert "estimated_total_token_hard_cap" not in body["execution_budget"]

            fetched = client.get(f"/api/tasks/{body['id']}").json()
            assert fetched["sizing"] == body["sizing"]
            assert fetched["execution_budget"] == body["execution_budget"]


def test_estimated_max_attempts_is_persisted_and_drives_terminal_decision() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            preview = client.post("/api/tasks/estimate", json=_xl_sizing_inputs()).json()
            assert preview["max_attempts"] == 4
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-xl-attempt-truth-001"},
                json=_api_task_payload(
                    project,
                    "xl-attempt-truth",
                    sizing_inputs=_xl_sizing_inputs(),
                    max_attempts=1,
                ),
            )
            assert response.status_code == 201, response.text
            body = response.json()
            assert body["max_attempts"] == 4
            assert body["execution_budget"]["max_attempts"] == 4
        connection = app.state.database.connect()
        try:
            stored = connection.execute(
                "SELECT max_attempts FROM tasks WHERE id = ?", (body["id"],)
            ).fetchone()
        finally:
            connection.close()
        assert stored["max_attempts"] == 4

        repository = _repository(root)
        create_kwargs = _create_kwargs(project, "attempt-terminal")
        create_kwargs["max_attempts"] = 1
        create_kwargs["sizing"] = {"status": "estimated"}
        create_kwargs["execution_budget"] = {
            "total_token_hard_cap": 100,
            "max_attempts": 4,
        }
        task = repository.create(**create_kwargs)
        for attempt in range(1, 5):
            claim = repository.claim(
                task.id,
                expected_revision=task.revision,
                idempotency_key=f"attempt-claim-{attempt}",
            )
            verifying = repository.mark_verifying(
                task.id,
                expected_revision=claim.task.revision,
                idempotency_key=f"attempt-verify-{attempt}",
            )
            task = repository.finish(
                task.id,
                expected_revision=verifying.revision,
                attempt_id=claim.attempt_id,
                run_id=claim.run_id,
                execution={"input_tokens": 0, "output_tokens": 0},
                verification=_verification(
                    passed=False, evidence_hash=f"attempt-proof-{attempt}"
                ),
                idempotency_key=f"attempt-finish-{attempt}",
                max_same_failure=10,
            )
            assert task.status.value == (
                "ready" if attempt < 4 else "terminal_failed"
            )
        assert task.attempts_used == task.max_attempts == 4


def test_create_rejects_missing_gates_with_machine_readable_detail() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-missing-gates-001"},
                json=_api_task_payload(
                    project,
                    "missing-gates",
                    sizing_inputs={**_m_sizing_inputs(), "gate_artifact": False},
                ),
            )
            assert response.status_code == 400
            detail = response.json()["detail"]
            assert detail["code"] == "needs_planning"
            assert detail["missing_gates"] == ["artifact"]


def test_create_rejects_forged_token_budget_without_override_reason() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-forged-budget-001"},
                json=_api_task_payload(
                    project,
                    "forged-budget",
                    sizing_inputs=_m_sizing_inputs(),
                    token_budget=999_999,
                ),
            )
            assert response.status_code == 400
            detail = response.json()["detail"]
            assert detail["code"] == "manual_override_required"
            assert detail["total_token_hard_cap"] == 225_000


def test_create_records_manual_override_without_changing_deadlines() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            preview = client.post("/api/tasks/estimate", json=_m_sizing_inputs()).json()
            created = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-manual-override-001"},
                json=_api_task_payload(
                    project,
                    "manual-override",
                    sizing_inputs=_m_sizing_inputs(),
                    token_budget=300_000,
                    manual_override_reason="operator approved higher hard cap",
                ),
            )
            assert created.status_code == 201
            body = created.json()
            assert body["token_budget"] == 300_000
            assert body["execution_budget"]["total_token_hard_cap"] == 300_000
            assert body["execution_budget"]["estimated_total_token_hard_cap"] == 225_000
            assert body["manual_override"] is True
            assert body["override_reason"] == "operator approved higher hard cap"
            assert body["execution_budget"]["soft_deadline_seconds"] == preview["soft_deadline_seconds"]
            assert body["execution_budget"]["hard_deadline_seconds"] == preview["hard_deadline_seconds"]
            assert body["execution_budget"]["reserved_tokens"] == preview["reserved_tokens"]

            event = client.get(f"/api/tasks/{body['id']}/events").json()[0]
            assert event["payload"]["total_token_hard_cap"] == 300_000
            assert event["payload"]["manual_override"] is True


def test_create_rejects_override_below_reserved_tokens() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            preview = client.post("/api/tasks/estimate", json=_s_sizing_inputs()).json()
            assert preview["size_class"] == "S"
            assert preview["reserved_tokens"] == 60_000
            assert preview["total_token_hard_cap"] == 90_000

            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-below-reserved-001"},
                json=_api_task_payload(
                    project,
                    "below-reserved",
                    sizing_inputs=_s_sizing_inputs(),
                    token_budget=50_000,
                    manual_override_reason="attempt to undercut reservation",
                ),
            )
            assert response.status_code == 400
            detail = response.json()["detail"]
            assert detail["code"] == "token_budget_below_reserved"
            assert detail["token_budget"] == 50_000
            assert detail["reserved_tokens"] == 60_000


def test_legacy_ui_fast_request_stores_deterministic_and_legacy_fallback() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-legacy-fast-001"},
                json=_api_task_payload(project, "legacy-fast", quality_profile="fast"),
            )
            assert response.status_code == 201
            body = response.json()
            assert body["quality_profile"] == "deterministic"
            assert body["sizing"] == {"status": "legacy_fallback"}
            assert body["execution_budget"] is None
            assert body["manual_override"] is False
            assert body["budget_overrun_evidence"] is None


def test_create_and_estimate_do_not_record_token_usage() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            connection = app.state.database.connect()
            try:
                usage_before = connection.execute(
                    "SELECT COUNT(*) FROM token_usage"
                ).fetchone()[0]
            finally:
                connection.close()

            client.post("/api/tasks/estimate", json=_m_sizing_inputs())
            client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-no-usage-001"},
                json=_api_task_payload(project, "no-usage", sizing_inputs=_m_sizing_inputs()),
            )

            connection = app.state.database.connect()
            try:
                usage_after = connection.execute(
                    "SELECT COUNT(*) FROM token_usage"
                ).fetchone()[0]
            finally:
                connection.close()
            assert usage_after == usage_before


def test_api_idempotent_create_does_not_overwrite_sizing_facts() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        payload = _api_task_payload(project, "api-idempotent", sizing_inputs=_m_sizing_inputs())
        with TestClient(app) as client:
            first = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-api-idempotent-001"},
                json=payload,
            )
            replay = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": "create-api-idempotent-001"},
                json={**payload, "title": "changed title should not matter"},
            )
            assert first.status_code == 201
            assert replay.status_code == 201
            first_body = first.json()
            replay_body = replay.json()
            assert replay_body["id"] == first_body["id"]
            assert replay_body["sizing"] == first_body["sizing"]
            assert replay_body["execution_budget"] == first_body["execution_budget"]
            assert replay_body["title"] == first_body["title"]


def _verifying_task(
    root: Path, key: str, *, cap: int, legacy: bool = False,
) -> tuple[TaskRepository, object, str, str]:
    project = root / f"project-{key}"
    project.mkdir()
    repository = _repository(root)
    create_kwargs = _create_kwargs(project, f"create-{key}")
    create_kwargs["token_budget"] = cap if legacy else cap * 10
    task = repository.create(
        **create_kwargs,
        **({} if legacy else {
            "sizing": {"status": "estimated"},
            "execution_budget": {"total_token_hard_cap": cap},
        }),
    )
    claim = repository.claim(
        task.id, expected_revision=task.revision, idempotency_key=f"claim-{key}"
    )
    assert claim.attempt_id is not None
    assert claim.run_id is not None
    verifying = repository.mark_verifying(
        task.id,
        expected_revision=claim.task.revision,
        idempotency_key=f"verify-{key}",
    )
    return repository, verifying, claim.attempt_id, claim.run_id


def _verification(*, passed: bool = True, evidence_hash: str = "proof-hash") -> dict[str, object]:
    return {
        "passed": passed,
        "checks": [{"kind": "exit_code", "passed": passed}],
        "evidence_hash": evidence_hash,
        "failure_fingerprint": evidence_hash,
        "summary": "verification passed" if passed else "verification failed",
    }


def _finish(
    repository: TaskRepository,
    task: object,
    attempt_id: str,
    run_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    verification: dict[str, object] | None = None,
    evidence: dict[str, object] | None = None,
    key: str = "finish",
):
    return repository.finish(
        task.id,  # type: ignore[attr-defined]
        expected_revision=task.revision,  # type: ignore[attr-defined]
        attempt_id=attempt_id,
        run_id=run_id,
        execution={"input_tokens": input_tokens, "output_tokens": output_tokens},
        verification=verification or _verification(),
        budget_overrun_evidence=evidence,
        idempotency_key=key,
    )


def test_finish_exactly_at_estimated_hard_cap_completes() -> None:
    with TemporaryDirectory() as directory:
        repository, task, attempt_id, run_id = _verifying_task(
            Path(directory), "under-cap", cap=100
        )

        finished = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=60, output_tokens=40,
        )

        assert finished.status.value == "completed"
        assert finished.tokens_used == 100
        assert finished.last_error is None


@pytest.mark.parametrize(
    ("key", "evidence"),
    [
        ("no-evidence", None),
        ("valid-evidence", {
            "actual_tokens": 101,
            "total_token_hard_cap": 100,
            "verification_evidence_hash": "proof-hash",
            "reason": "verified result retained for calibration",
            "prohibit_new_model_run": True,
        }),
        ("malformed-evidence", {"actual_tokens": "forged", "reason": 42}),
    ],
)
def test_finish_over_cap_is_terminal_and_evidence_is_audit_only(
    key: str, evidence: dict[str, object] | None,
) -> None:
    with TemporaryDirectory() as directory:
        repository, task, attempt_id, run_id = _verifying_task(
            Path(directory), f"over-cap-{key}", cap=100
        )

        finished = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=80, output_tokens=21, evidence=evidence,
        )

        assert finished.status.value == "terminal_failed"
        assert finished.tokens_used == 101
        assert finished.last_error == "budget_exceeded"
        assert finished.budget_overrun_evidence == evidence
        event = repository.events(finished.id)[-1]
        assert event["event_type"] == "task.terminal_failed"
        assert event["payload"]["reason"] == "budget_exceeded"
        assert event["payload"]["budget_overrun_evidence_recorded"] is (
            evidence is not None
        )
        assert not any(
            item["event_type"] in {"task.retry_scheduled", "task.needs_human"}
            for item in repository.events(finished.id)
        )
        connection = repository.database.connect()
        try:
            outbox = connection.execute(
                "SELECT payload_json FROM outbox_events WHERE aggregate_id = ?",
                (finished.id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status, finished_at FROM task_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            run = connection.execute(
                "SELECT status, input_tokens, output_tokens, result_json, finished_at "
                "FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            usage = connection.execute(
                "SELECT input_tokens, output_tokens FROM token_usage WHERE call_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        assert outbox is None
        assert attempt["status"] == "failed" and attempt["finished_at"] is not None
        assert run["status"] == "failed" and run["finished_at"] is not None
        assert (run["input_tokens"], run["output_tokens"]) == (80, 21)
        assert (usage["input_tokens"], usage["output_tokens"]) == (80, 21)
        result = json.loads(run["result_json"])
        assert result["verification"]["checks"][0]["kind"] == "exit_code"
        assert result["budget"] == {
            "reason": "budget_exceeded",
            "actual_tokens": 101,
            "total_token_hard_cap": 100,
        }


def test_finish_legacy_fallback_uses_task_token_budget_as_cap() -> None:
    with TemporaryDirectory() as directory:
        repository, task, attempt_id, run_id = _verifying_task(
            Path(directory), "legacy-cap", cap=10, legacy=True
        )

        finished = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=6, output_tokens=5,
        )

        assert finished.status.value == "terminal_failed"
        assert finished.last_error == "budget_exceeded"


def test_finish_replay_does_not_double_count_tokens_usage_or_events() -> None:
    with TemporaryDirectory() as directory:
        repository, task, attempt_id, run_id = _verifying_task(
            Path(directory), "finish-replay", cap=100
        )
        first = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=80, output_tokens=21, key="finish-replay",
        )
        event_count = len(repository.events(first.id))
        revision = first.revision
        replay = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=80, output_tokens=21, key="finish-replay",
        )

        connection = repository.database.connect()
        try:
            usage_count = connection.execute(
                "SELECT COUNT(*) FROM token_usage WHERE call_id = ?", (run_id,)
            ).fetchone()[0]
            run_count = connection.execute(
                "SELECT COUNT(*) FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        assert replay.id == first.id
        assert replay.status.value == first.status.value == "terminal_failed"
        assert replay.revision == revision
        assert replay.tokens_used == first.tokens_used == 101
        assert usage_count == 1
        assert run_count == 1
        assert len(repository.events(replay.id)) == event_count


def test_finish_rejects_evidence_when_there_is_no_overrun() -> None:
    with TemporaryDirectory() as directory:
        repository, task, attempt_id, run_id = _verifying_task(
            Path(directory), "unneeded-evidence", cap=100
        )
        evidence = {
            "actual_tokens": 10,
            "total_token_hard_cap": 100,
            "verification_evidence_hash": "proof-hash",
            "reason": "not actually over budget",
            "prohibit_new_model_run": True,
        }

        finished = _finish(
            repository, task, attempt_id, run_id,
            input_tokens=4, output_tokens=6, evidence=evidence,
        )

        assert finished.status.value == "needs_human"
        assert finished.last_error == "invalid_budget_overrun_evidence"
        assert finished.budget_overrun_evidence is None
