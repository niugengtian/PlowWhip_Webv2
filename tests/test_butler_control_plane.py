from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError


def _runtime():
    directory = TemporaryDirectory()
    root = Path(directory.name)
    project_path = root / "project"
    project_path.mkdir()
    app = create_app(Settings(data_dir=root / "runtime"))
    project = app.state.project_repository.create(
        name="butler", path=str(project_path)
    )
    return directory, app, project, project_path


def test_structured_and_natural_inputs_share_one_intake_and_auto_dispatch() -> None:
    directory, app, project, _ = _runtime()
    try:
        with TestClient(app) as client:
            natural = client.post(
                "/api/butler/intakes",
                headers={"Idempotency-Key": "natural-intake-001"},
                json={
                    "project_id": project["id"],
                    "source": "natural_language",
                    "instruction": "create a small deterministic report",
                    "confidence": 80,
                },
            )
            structured = client.post(
                "/api/butler/intakes",
                headers={"Idempotency-Key": "structured-intake-001"},
                json={
                    "project_id": project["id"],
                    "source": "structured",
                    "instruction": "implement two bounded work items",
                    "structured_input": {
                        "plan_items": [
                            {
                                "ordinal": 1,
                                "role": "fullstack",
                                "kind": "implementation",
                                "title": "implement",
                                "objective": "implement",
                                "depends_on_ordinals": [],
                            },
                            {
                                "ordinal": 2,
                                "role": "verification",
                                "kind": "verification",
                                "title": "verify",
                                "objective": "verify",
                                "depends_on_ordinals": [1],
                            },
                        ]
                    },
                    "confidence": 80,
                },
            )
        assert natural.status_code == structured.status_code == 201
        assert natural.json()["status"] == structured.json()["status"] == "dispatched"
        assert natural.json()["goal_id"] and structured.json()["goal_id"]
        connection = app.state.database.connect()
        try:
            sources = {
                row["source"]
                for row in connection.execute("SELECT source FROM butler_intakes")
            }
            wakes = connection.execute(
                "SELECT COUNT(*) FROM outbox_events "
                "WHERE event_type = 'worker.wake_requested'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert sources == {"structured", "natural_language"}
        assert wakes >= 2
    finally:
        directory.cleanup()


def test_large_intake_has_one_question_and_needs_95_percent_owner_confirm() -> None:
    directory, app, project, _ = _runtime()
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/butler/intakes",
                headers={"Idempotency-Key": "large-intake-001"},
                json={
                    "project_id": project["id"],
                    "source": "structured",
                    "instruction": "deploy a database migration to production",
                    "structured_input": {
                        "has_deploy": True,
                        "has_migration": True,
                    },
                    "model_size": "small",
                    "confidence": 70,
                },
            ).json()
            assert created["deterministic_size"] == created["assessed_size"] == "large"
            assert created["goal_id"] is None
            assert len([q for q in created["questions"] if q["answered_at"] is None]) == 1

            still_unclear = client.post(
                f"/api/butler/intakes/{created['id']}/answers",
                headers={"Idempotency-Key": "large-answer-001"},
                json={
                    "expected_revision": created["revision"],
                    "answer": "first constraint",
                    "confidence": 90,
                },
            ).json()
            assert still_unclear["status"] == "clarifying"
            assert len(
                [q for q in still_unclear["questions"] if q["answered_at"] is None]
            ) == 1

            proposal = {
                "objective": "safe migration",
                "plan_items": [],
                "rollback": "required",
            }
            ready = client.post(
                f"/api/butler/intakes/{created['id']}/answers",
                headers={"Idempotency-Key": "large-answer-002"},
                json={
                    "expected_revision": still_unclear["revision"],
                    "answer": "rollback confirmed",
                    "confidence": 95,
                    "proposal": proposal,
                },
            ).json()
            assert ready["status"] == "awaiting_confirmation"
            assert ready["goal_id"] is None

            stale = client.post(
                f"/api/butler/intakes/{created['id']}/confirm",
                headers={"Idempotency-Key": "large-confirm-stale"},
                json={
                    "expected_revision": ready["revision"],
                    "proposal_hash": "0" * 64,
                    "approved": True,
                    "reason": "approve",
                },
            )
            assert stale.status_code == 409
            confirmed = client.post(
                f"/api/butler/intakes/{created['id']}/confirm",
                headers={"Idempotency-Key": "large-confirm-001"},
                json={
                    "expected_revision": ready["revision"],
                    "proposal_hash": ready["proposal_hash"],
                    "approved": True,
                    "reason": "owner approved exact proposal",
                },
            )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "dispatched"
        assert confirmed.json()["goal_id"]
    finally:
        directory.cleanup()


def test_help_reply_owner_escalation_and_bounded_same_task_context_are_durable() -> None:
    directory, app, project, project_path = _runtime()
    try:
        role = app.state.project_repository.resolve_role(
            project["id"], "fullstack"
        )["role_id"]
        task = app.state.task_repository.create(
            title="help", objective="need help", project_path=str(project_path),
            project_id=project["id"], role_id=role,
            command={"argv": ["true"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=0, idempotency_key="help-task-create",
        )
        with TestClient(app) as client:
            opened = client.post(
                "/api/butler/help",
                headers={"Idempotency-Key": "help-open-001"},
                json={
                    "project_id": project["id"],
                    "task_id": task.id,
                    "category": "decision",
                    "severity": "blocking",
                    "question": "which safe option?",
                    "checkpoint": {"task_id": task.id, "next_action": "wait"},
                },
            ).json()
            escalated = client.post(
                f"/api/butler/help/{opened['id']}/replies",
                headers={"Idempotency-Key": "help-escalate-001"},
                json={
                    "expected_revision": 0,
                    "sender": "butler",
                    "content": "owner decision required",
                    "bounded_context": {"task_id": task.id, "evidence_hash": "abc"},
                    "escalate": True,
                },
            ).json()
            answered = client.post(
                f"/api/butler/help/{opened['id']}/replies",
                headers={"Idempotency-Key": "help-owner-reply-001"},
                json={
                    "expected_revision": escalated["revision"],
                    "sender": "owner",
                    "content": "choose option A",
                    "bounded_context": {
                        "task_id": task.id,
                        "answer": "option A",
                    },
                    "escalate": False,
                },
            ).json()
            direct = client.post(
                "/api/butler/help",
                headers={"Idempotency-Key": "help-direct-open-001"},
                json={
                    "project_id": project["id"],
                    "task_id": task.id,
                    "category": "implementation",
                    "severity": "normal",
                    "question": "can Butler answer directly?",
                },
            ).json()
            rejected = client.post(
                f"/api/butler/help/{direct['id']}/replies",
                headers={"Idempotency-Key": "help-cross-task-001"},
                json={
                    "expected_revision": 0,
                    "sender": "butler",
                    "content": "unsafe context",
                    "bounded_context": {"task_id": "another-task"},
                },
            )
            owner_bypass = client.post(
                f"/api/butler/help/{direct['id']}/replies",
                headers={"Idempotency-Key": "help-owner-bypass-001"},
                json={
                    "expected_revision": 0,
                    "sender": "owner",
                    "content": "cannot bypass Butler escalation",
                },
            )
            direct_answer = client.post(
                f"/api/butler/help/{direct['id']}/replies",
                headers={"Idempotency-Key": "help-direct-answer-001"},
                json={
                    "expected_revision": 0,
                    "sender": "butler",
                    "content": "bounded answer",
                },
            ).json()
            listed = client.get(
                "/api/butler/help", params={"project_id": project["id"]}
            )
            control_plane = client.get(
                f"/api/aggregates/task/{task.id}/control-plane"
            )
        assert escalated["status"] == "owner_escalated"
        assert answered["status"] == "answered"
        assert answered["replies"][-1]["bounded_context"]["task_id"] == task.id
        assert rejected.status_code == 409
        assert owner_bypass.status_code == 409
        assert direct_answer["replies"][-1]["bounded_context"]["task_id"] == task.id
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()} == {opened["id"], direct["id"]}
        assert control_plane.status_code == 200
        canonical = control_plane.json()
        assert canonical["canonical_state"] == {
            "status": "ready",
            "revision": 0,
            "evidence_hash": None,
            "updated_at": task.updated_at,
        }
        assert canonical["session_identity"] == "project_id+role_id+task_id"
        assert canonical["next_action"]["kind"] == "drive"
        assert canonical["deletion"]["status"] == "deletable"
        assert len(canonical["help_requests"]) == 2
        assert canonical["lineage"][-1]["reason"] == "task.created"
        connection = app.state.database.connect()
        try:
            events = {
                row["event_type"]: row["payload_json"]
                for row in connection.execute(
                    "SELECT event_type, payload_json FROM outbox_events "
                    "WHERE topic = 'help'"
                )
            }
        finally:
            connection.close()
        assert {
            "worker.help_requested",
            "owner.escalation_requested",
            "owner.escalation_resolved",
            "butler.help_replied",
        } <= events.keys()
        assert all(task.id in payload for payload in events.values())
    finally:
        directory.cleanup()


def test_help_checkpoint_rejects_payload_above_effective_task_limit() -> None:
    directory, app, project, project_path = _runtime()
    try:
        role = app.state.project_repository.resolve_role(
            project["id"], "fullstack"
        )["role_id"]
        task = app.state.task_repository.create(
            title="bounded-help", objective="bounded help",
            project_path=str(project_path), project_id=project["id"], role_id=role,
            command={"argv": ["true"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1, token_budget=0,
            idempotency_key="bounded-help-task-create",
        )
        app.state.conventions.put(
            scope="task", scope_id=task.id, expected_revision=0,
            content='Continuity-Limits: {"checkpoint_max_bytes":512}',
        )
        with pytest.raises(DomainError, match="effective maximum is 512 bytes"):
            app.state.butler_repository.create_help(
                project_id=project["id"], task_id=task.id, worker_id=None,
                category="implementation", severity="normal", question="help",
                checkpoint={"payload": "x" * 600},
                idempotency_key="bounded-help-open",
            )
    finally:
        directory.cleanup()


def test_interrupt_persists_state_and_stops_dispatched_goal_tasks() -> None:
    directory, app, project, _ = _runtime()
    try:
        with TestClient(app) as client:
            intake = client.post(
                "/api/butler/intakes",
                headers={"Idempotency-Key": "interrupt-intake-001"},
                json={
                    "project_id": project["id"],
                    "source": "natural_language",
                    "instruction": "small interruptible goal",
                    "confidence": 80,
                },
            ).json()
            stopped = client.post(
                f"/api/butler/intakes/{intake['id']}/interrupt",
                headers={"Idempotency-Key": "interrupt-intake-action-001"},
                json={
                    "expected_revision": intake["revision"],
                    "reason": "owner stop",
                },
            )
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "interrupted"
        goal = app.state.goal_repository.get(intake["goal_id"])
        assert all(
            item["status"] in {"cancelled", "completed", "terminal_failed"}
            for item in goal["work_items"]
        )
    finally:
        directory.cleanup()


def test_butler_api_dispatch_to_real_generic_provider_and_verification_e2e() -> None:
    directory, app, project, project_path = _runtime()
    try:
        artifact = project_path / "butler-e2e.txt"
        with TestClient(app) as client:
            intake = client.post(
                "/api/butler/intakes",
                headers={"Idempotency-Key": "butler-provider-e2e-intake"},
                json={
                    "project_id": project["id"],
                    "source": "structured",
                    "instruction": "write and verify a bounded fixture artifact",
                    "structured_input": {
                        "provider": "generic-command",
                        "title": "Butler provider E2E",
                        "plan_items": [
                            {
                                "ordinal": 1,
                                "role": "fullstack",
                                "kind": "implementation",
                                "title": "write fixture",
                                "objective": "write fixture",
                                "depends_on_ordinals": [],
                            },
                            {
                                "ordinal": 2,
                                "role": "verification",
                                "kind": "verification",
                                "title": "verify fixture",
                                "objective": "verify fixture",
                                "depends_on_ordinals": [1],
                            },
                        ],
                        "command": {
                            "argv": [
                                sys.executable, "-c",
                                "from pathlib import Path; "
                                "Path('butler-e2e.txt').write_text('verified')",
                            ]
                        },
                        "verification": [
                            {"kind": "file_exists", "path": "butler-e2e.txt"}
                        ],
                    },
                    "confidence": 80,
                },
            )
            assert intake.status_code == 201, intake.text
            goal = client.get(
                f"/api/goals/{intake.json()['goal_id']}"
            ).json()
            task = next(
                item for item in goal["work_items"]
                if item["work_item_kind"] == "implementation"
            )
            implemented = client.post(
                f"/api/tasks/{task['id']}/drive",
                headers={"Idempotency-Key": "butler-provider-e2e-drive"},
                json={"expected_revision": task["revision"]},
            )
            assert implemented.status_code == 200
            control_plane = client.get(
                f"/api/aggregates/task/{task['id']}/control-plane"
            ).json()
            physical = control_plane["provider_sessions"][0]
            assert (
                physical["project_id"], physical["role_id"], physical["task_id"]
            ) == (project["id"], task["role_id"], task["id"])
            assert control_plane["session_identity"] == "project_id+role_id+task_id"
            for _ in range(4):
                tick = client.post("/api/scheduler/tick")
                assert tick.status_code == 200
                advanced = client.get(
                    f"/api/goals/{intake.json()['goal_id']}"
                ).json()
                if advanced["status"] == "completed":
                    break
            else:
                raise AssertionError(f"goal did not complete: {advanced}")
            verification = next(
                item for item in advanced["work_items"]
                if item["work_item_kind"] == "verification"
            )
        assert implemented.json()["status"] == verification["status"] == "completed"
        assert artifact.read_text(encoding="utf-8") == "verified"
        assert verification["last_evidence_hash"]
        connection = app.state.database.connect()
        try:
            session = connection.execute(
                "SELECT project_id, role_id, task_id FROM provider_sessions "
                "WHERE task_id = ?",
                (task["id"],),
            ).fetchone()
            model_calls = connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE task_id = ?",
                (task["id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        assert tuple(session) == (project["id"], task["role_id"], task["id"])
        assert model_calls == 0
    finally:
        directory.cleanup()
