from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.providers.generic_command import ExecutionResult
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


class SmartButlerBridge:
    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal
        self.providers: list[str] = []
        self.token = "test-token-is-long-enough-123"

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "ready"

    def execute(self, **kwargs: object) -> ExecutionResult:
        provider = kwargs["provider"]
        assert isinstance(provider, dict)
        self.providers.append(str(provider["name"]))
        return ExecutionResult(
            returncode=0,
            stdout=json.dumps(self.proposal, ensure_ascii=False),
            stderr="",
            duration_ms=5,
            input_tokens=100,
            output_tokens=60,
            external_session_id="smart-butler-session",
        )


def test_project_butler_uses_selected_model_then_requires_human_confirmation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        templates = {
            item["capability"]: f"{item['template_id']}@{item['revision']}"
            for item in app.state.role_instance_repository.list_templates()
        }
        rules = [
            "dev.think_before_coding@1",
            "dev.simplicity_first@1",
            "dev.surgical_changes@1",
            "dev.goal_driven_execution@1",
        ]
        roles = ["backend", "frontend", "devops_sre", "verification"]
        role_providers = {role: "cursor" for role in roles}
        proposal = {
            "goal_spec": {
                "title": "分层管家协作",
                "objective": "实现全局管家和项目管家的分层协作并给出可核验证据",
                "boundaries": ["只改控制平面和 Web UI", "禁止读取其他项目聊天"],
                "acceptance": ["后端和前端测试通过", "最终证据包含提交哈希"],
                "sizing_inputs": _large_sizing(),
                "provider": "cursor",
                "role_providers": role_providers,
                "role_templates": {role: templates[role] for role in roles},
                "role_rules": {role: rules for role in roles},
                "plan_items": [
                    {
                        "ordinal": ordinal,
                        "role": role,
                        "kind": "verification" if role == "verification" else "implementation",
                        "title": role,
                        "objective": f"完成 {role} 工作并留下证据",
                        "depends_on_ordinals": [] if ordinal == 1 else [ordinal - 1],
                        "provider": role_providers[role],
                    }
                    for ordinal, role in enumerate(roles, 1)
                ],
            }
        }
        bridge = SmartButlerBridge(proposal)
        app.state.provider_pool.bridge = bridge
        app.state.butler_planner.provider_pool.bridge = bridge
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects", json={"name": "isolated", "path": str(project_path)}
            ).json()
            started = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "butler-natural-language-1"},
                json={
                    "instruction": "使用 Cursor 实现全局管家和项目管家的分层协作",
                    "provider": "codex",
                    "sizing_inputs": _large_sizing(),
                },
            )
            assert started.status_code == 201, started.text
            proposal_view = started.json()
            assert proposal_view["status"] == "awaiting_confirmation"
            assert proposal_view["planner"]["status"] == "planned"
            assert proposal_view["provider"] == "codex"
            assert proposal_view["spec"]["provider"] == "cursor"
            assert bridge.providers == ["codex"]
            assert [item["kind"] for item in proposal_view["messages"]] == [
                "instruction", "proposal"
            ]

            non_human = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{proposal_view['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-agent-1"},
                json={
                    "expected_revision": proposal_view["revision"],
                    "proposal_hash": proposal_view["proposal_hash"],
                    "actor_type": "agent",
                },
            )
            assert non_human.status_code == 422

            confirmed = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{proposal_view['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-human-1"},
                json={
                    "expected_revision": proposal_view["revision"],
                    "proposal_hash": proposal_view["proposal_hash"],
                    "actor_type": "human",
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            dispatched = confirmed.json()
            assert dispatched["status"] == "dispatched"
            goal = client.get(f"/api/goals/{dispatched['goal_id']}").json()
            assert [item["role"] for item in goal["work_items"]] == [
                "backend", "frontend", "devops_sre", "verification"
            ]
            assert [item["provider"] for item in goal["work_items"]] == [
                "cursor", "cursor", "cursor", "cursor"
            ]
            by_role = {item["role"]: item for item in goal["work_items"]}
            assert by_role["backend"]["status"] == "ready"
            assert by_role["frontend"]["status"] == "paused"
            assert by_role["devops_sre"]["status"] == "paused"
            assert goal["spec"]["acceptance"] == proposal_view["spec"]["acceptance"]
            assert goal["spec"]["scope"] == proposal_view["spec"]["boundaries"]
            for item in goal["work_items"]:
                context = app.state.context_compiler.compile(item["id"])
                assert context["role"] == item["role"]
                assert ROLE_PROMPTS[item["role"]] in context["content"]
                assert "## Worker orchestration boundary" in context["content"]
                assert (
                    "Do not create, spawn, delegate to, or wait for other agents"
                    in context["content"]
                )
                assert (
                    "Do not load personal memory, old chats, external skills"
                    in context["content"]
                )
                if item["role"] == "verification":
                    assert "不得自行要求或创建 OS 沙箱" in context["content"]
                    assert "具备写权限本身不使真实只读证据失效" in context["content"]
                if item["role"] in {
                    "backend", "frontend", "ui", "devops_sre", "verification", "fullstack",
                }:
                    assert "Think Before Coding" in context["content"]
                    assert context["behavior_baseline"]["mandatory"] is True
                else:
                    assert context["behavior_baseline"]["not_applicable"] is True

            retried = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{proposal_view['id']}/confirm",
                headers={"Idempotency-Key": "butler-confirm-human-retry"},
                json={
                    "expected_revision": proposal_view["revision"],
                    "proposal_hash": proposal_view["proposal_hash"],
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
