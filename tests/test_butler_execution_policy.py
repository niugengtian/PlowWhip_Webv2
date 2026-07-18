from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.butler import route_goal


def _sizing(size: str) -> dict[str, object]:
    values: dict[str, object] = {
        "layers_touched": 0,
        "components_touched": 0,
        "estimated_files_changed": 1,
        "has_migration": False,
        "has_deploy": False,
        "verification_commands_count": 1,
        "estimated_verification_seconds": 0,
        "external_dependencies_count": 0,
        "risk_level": "low",
        "independent_review_required": True,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }
    if size == "M":
        values.update(
            layers_touched=2, components_touched=4,
            estimated_files_changed=5, risk_level="medium",
        )
    elif size == "L":
        values.update(
            layers_touched=4, components_touched=8,
            estimated_files_changed=10, verification_commands_count=3,
            estimated_verification_seconds=300, risk_level="medium",
        )
    return values


@pytest.fixture
def butler_app():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "butler", "path": str(project_path)}
            ).json()
            yield app, client, project


def _create_goal(client: TestClient, project_id: str, size: str):
    return client.post(
        "/api/goals",
        headers={"Idempotency-Key": f"butler-{size.lower()}-fixture"},
        json={
            "title": f"{size} fixture",
            "objective": "route once and require evidence",
            "project_id": project_id,
            "provider": "generic-command",
            "sizing_inputs": _sizing(size),
            "command": {
                "argv": [
                    sys.executable, "-c",
                    "from pathlib import Path; Path('done.txt').write_text('ok')",
                ]
            },
            "verification": [
                {"kind": "exit_code", "expected": 0},
                {"kind": "file_contains", "path": "done.txt", "contains": "ok"},
            ],
        },
    )


def test_fresh_project_has_one_butler_and_policy(butler_app) -> None:
    _, _, project = butler_app
    assert [role["kind"] for role in project["roles"]] == ["butler"]
    assert project["execution_policy"] == {
        "version": "butler-v1",
        "routing": {
            "XS": "simple-worker",
            "S": "ephemeral-fullstack",
            "M": "ephemeral-fullstack",
            "L": "capability-milestones",
            "XL": "capability-milestones",
        },
        "max_milestones": 6,
        "verification_gate_required": True,
        "release_worker_on_terminal": True,
    }


def test_canonical_butler_facade_returns_project_route() -> None:
    decision = route_goal("XS")
    assert decision.route == "simple-worker"
    assert decision.policy["version"] == "butler-v1"


def test_diagnostic_role_is_ephemeral_not_a_new_permanent_pool(butler_app) -> None:
    app, client, project = butler_app
    binding = app.state.project_repository.resolve_role(project["id"], "fullstack")
    state = client.get(f"/api/projects/{project['id']}").json()
    role = next(item for item in state["roles"] if item["id"] == binding["role_id"])
    assert role["kind"].startswith("fullstack:manual:")
    assert role["status"] == "ephemeral"
    assert [item["kind"] for item in state["roles"] if item["status"] == "available"] == [
        "butler"
    ]


@pytest.mark.parametrize(
    ("size", "route", "count", "role_prefix"),
    [
        ("XS", "simple-worker", 1, "simple-worker:"),
        ("M", "ephemeral-fullstack", 1, "fullstack:"),
        ("L", "capability-milestones", 4, "capability:"),
    ],
)
def test_butler_routes_fixtures_without_role_provider_decisions(
    butler_app, size: str, route: str, count: int, role_prefix: str
) -> None:
    _, client, project = butler_app
    response = _create_goal(client, project["id"], size)
    assert response.status_code == 201, response.text
    goal = response.json()
    assert goal["parent_task_id"] is None
    assert goal["plan"]["route"] == route
    assert len(goal["work_items"]) == count
    assert all(item["verification"] for item in goal["work_items"])
    assert all(item["parent_task_id"] is None for item in goal["work_items"])
    state = client.get(f"/api/projects/{project['id']}").json()
    ephemeral = [role for role in state["roles"] if role["status"] == "ephemeral"]
    assert len(ephemeral) == count
    assert all(role["kind"].startswith(role_prefix) for role in ephemeral)


def test_simple_worker_is_released_only_after_verified_terminal(butler_app) -> None:
    _, client, project = butler_app
    goal = _create_goal(client, project["id"], "XS").json()
    task_id = goal["work_items"][0]["id"]

    for _ in range(5):
        client.post("/api/scheduler/tick")
        goal = client.get(f"/api/goals/{goal['id']}").json()
        if goal["status"] == "completed":
            break

    assert goal["status"] == "completed"
    task = next(item for item in goal["work_items"] if item["id"] == task_id)
    assert task["last_evidence_hash"]
    state = client.get(f"/api/projects/{project['id']}").json()
    worker = next(worker for worker in state["workers"] if worker["role"].startswith("simple-worker:"))
    assert worker["status"] == "released"
    assert worker["released_at"] is not None
    assert worker["rotation_reason"] == "task_terminal"
