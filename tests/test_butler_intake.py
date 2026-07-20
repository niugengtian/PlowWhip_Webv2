from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.roles import ROLE_PROMPTS


def _large_sizing() -> dict[str, object]:
    return {
        "layers_touched": 4,
        "components_touched": 8,
        "estimated_files_changed": 12,
        "has_migration": True,
        "has_deploy": True,
        "verification_commands_count": 4,
        "estimated_verification_seconds": 300,
        "external_dependencies_count": 0,
        "risk_level": "medium",
        "independent_review_required": False,
        "gate_artifact": True,
        "gate_boundary": True,
        "gate_verification": True,
        "gate_dependency": True,
    }


def test_project_butler_asks_one_question_then_requires_human_confirmation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "isolated", "path": str(project_path)}
            ).json()
            started = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "butler-natural-language-1"},
                json={
                    "instruction": "实现全局管家和项目管家的分层协作",
                    "provider": "cursor",
                    "sizing_inputs": _large_sizing(),
                    "role_providers": {
                        "backend": "codex",
                        "frontend": "cursor",
                        "ui": "cursor",
                        "devops_sre": "codex",
                    },
                },
            )
            assert started.status_code == 201, started.text
            conversation = started.json()
            assert conversation["status"] == "clarifying"
            assert conversation["confidence"] == 35
            assert conversation["expected_field"] == "boundaries"
            assert [item["kind"] for item in conversation["messages"]] == [
                "instruction", "question"
            ]

            out_of_order = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/answers",
                json={
                    "expected_revision": conversation["revision"],
                    "field": "acceptance",
                    "values": ["能通过验收"],
                },
            )
            assert out_of_order.status_code == 409

            boundary_answer = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/messages",
                json={
                    "expected_revision": conversation["revision"],
                    "content": (
                        "只改控制平面和 Web UI\n"
                        "不读取其他项目聊天，不共享项目会话"
                    ),
                },
            ).json()
            assert boundary_answer["confidence"] == 65
            assert boundary_answer["expected_field"] == "acceptance"
            assert boundary_answer["messages"][-2]["content"].startswith(
                "只改控制平面"
            )
            assert boundary_answer["messages"][-1]["kind"] == "question"

            proposal_response = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/messages",
                json={
                    "expected_revision": boundary_answer["revision"],
                    "content": (
                        "未满足三要素时一次只问一个问题\n"
                        "确认后独立角色可以同时进入 ready"
                    ),
                },
            )
            assert proposal_response.status_code == 200, proposal_response.text
            proposal = proposal_response.json()
            assert proposal["status"] == "awaiting_confirmation"
            assert proposal["confidence"] == 95
            assert proposal["expected_field"] is None
            assert len(proposal["proposal_hash"]) == 64
            assert proposal["messages"][-1]["kind"] == "proposal"

            revision_without_field = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/messages",
                json={
                    "expected_revision": proposal["revision"],
                    "content": "最后一条验收标准还需要更准确",
                },
            )
            assert revision_without_field.status_code == 409
            old_proposal_hash = proposal["proposal_hash"]
            revised = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/messages",
                json={
                    "expected_revision": proposal["revision"],
                    "field": "acceptance",
                    "content": (
                        "未满足三要素时一次只问一个问题\n"
                        "确认后四个独立角色同时进入 ready"
                    ),
                },
            )
            assert revised.status_code == 200, revised.text
            proposal = revised.json()
            assert proposal["status"] == "awaiting_confirmation"
            assert proposal["messages"][-2]["payload"]["proposal_revision"] is True
            assert proposal["messages"][-1]["kind"] == "proposal"
            assert proposal["spec"]["acceptance"][-1].startswith("确认后四个")
            assert proposal["proposal_hash"] != old_proposal_hash

            non_human = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-agent-1"},
                json={
                    "expected_revision": proposal["revision"],
                    "proposal_hash": proposal["proposal_hash"],
                    "actor_type": "agent",
                },
            )
            assert non_human.status_code == 422

            confirmed = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-human-1"},
                json={
                    "expected_revision": proposal["revision"],
                    "proposal_hash": proposal["proposal_hash"],
                    "actor_type": "human",
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            dispatched = confirmed.json()
            assert dispatched["status"] == "dispatched"
            goal = client.get(f"/api/goals/{dispatched['goal_id']}").json()
            assert [item["role"] for item in goal["work_items"]] == [
                "backend", "frontend", "ui", "devops_sre", "verification"
            ]
            assert [item["provider"] for item in goal["work_items"]] == [
                "codex", "cursor", "cursor", "codex", "cursor"
            ]
            by_role = {item["role"]: item for item in goal["work_items"]}
            assert by_role["backend"]["status"] == "ready"
            assert by_role["frontend"]["status"] == "paused"
            assert by_role["ui"]["status"] == "ready"
            assert by_role["devops_sre"]["status"] == "paused"
            assert goal["spec"]["acceptance"] == proposal["spec"]["acceptance"]
            assert goal["spec"]["scope"] == proposal["spec"]["boundaries"]
            for item in goal["work_items"]:
                context = app.state.context_compiler.compile(item["id"])
                assert context["role"] == item["role"]
                assert ROLE_PROMPTS[item["role"]] in context["content"]
                if item["role"] in {
                    "backend", "frontend", "ui", "devops_sre", "verification", "fullstack",
                }:
                    assert "Think Before Coding" in context["content"]
                    assert context["behavior_baseline"]["mandatory"] is True
                else:
                    assert context["behavior_baseline"]["not_applicable"] is True

            retried = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-human-retry"},
                json={
                    "expected_revision": proposal["revision"],
                    "proposal_hash": proposal["proposal_hash"],
                    "actor_type": "human",
                },
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["goal_id"] == dispatched["goal_id"]
            assert len(client.get("/api/goals").json()) == 1


def test_global_butler_indexes_registered_resources_and_routes_without_leaking() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        outside = root / "outside"
        first_path = workspace / "one"
        second_path = outside / "two"
        first_path.mkdir(parents=True)
        second_path.mkdir(parents=True)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            first = client.post(
                "/api/projects", json={"name": "one", "path": str(first_path)}
            ).json()
            second = client.post(
                "/api/projects", json={"name": "two", "path": str(second_path)}
            ).json()
            overview = client.get(
                "/api/butlers/global/overview",
                params={"workspace_root": str(workspace)},
            ).json()
            assert overview["totals"]["projects"] == 1
            assert overview["projects"][0]["id"] == first["id"]
            assert overview["canonical_sources"] == [
                "projects", "goals", "tasks", "workers"
            ]
            assert overview["model_invoked"] is False

            routed = client.post(
                "/api/butlers/global/route",
                headers={"Idempotency-Key": "global-route-project-one"},
                json={
                    "project_id": first["id"],
                    "instruction": "检查项目发布状态",
                    "source_type": "human",
                    "source_id": "owner",
                },
            )
            assert routed.status_code == 201, routed.text
            conversation = routed.json()
            assert conversation["source_type"] == "global_butler"
            assert conversation["project_id"] == first["id"]
            assert conversation["direct_project_butler_url"].startswith(
                f"/api/projects/{first['id']}/butler/"
            )

            leaked = client.get(
                f"/api/projects/{second['id']}/butler/conversations/"
                f"{conversation['id']}"
            )
            assert leaked.status_code == 404
