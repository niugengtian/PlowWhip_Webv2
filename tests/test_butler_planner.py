from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.providers.generic_command import ExecutionResult


class PlannerBridge:
    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal
        self.execute_calls = 0
        self.session_ids: list[str | None] = []
        self.token = "test-token-is-long-enough-123"

    def probe(self, _provider: dict[str, object]) -> tuple[bool, str]:
        return True, "ready"

    def execute(self, **kwargs: object) -> ExecutionResult:
        self.execute_calls += 1
        self.session_ids.append(kwargs.get("session_id"))  # type: ignore[arg-type]
        return ExecutionResult(
            returncode=0,
            stdout=json.dumps(self.proposal, ensure_ascii=False),
            stderr="",
            duration_ms=5,
            input_tokens=120,
            cached_input_tokens=20,
            output_tokens=80,
            external_session_id="planner-session-1",
        )


class InvalidPlannerBridge(PlannerBridge):
    def execute(self, **_kwargs: object) -> ExecutionResult:
        self.execute_calls += 1
        return ExecutionResult(
            returncode=0,
            stdout="not-json",
            stderr="",
            duration_ms=5,
            input_tokens=30,
            output_tokens=5,
        )


def test_natural_language_planner_creates_reviewable_three_task_codex_dag() -> None:
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
        sizing = {
            "layers_touched": 2,
            "components_touched": 7,
            "estimated_files_changed": 11,
            "has_migration": True,
            "has_deploy": False,
            "verification_commands_count": 5,
            "estimated_verification_seconds": 420,
            "external_dependencies_count": 0,
            "risk_level": "medium",
            "independent_review_required": True,
            "gate_artifact": True,
            "gate_boundary": True,
            "gate_verification": True,
            "gate_dependency": True,
        }
        proposal = {
            "goal_spec": {
                "title": "Smart Butler UI",
                "objective": "实现可配置项目管家规划并交付前后端可验证结果",
                "boundaries": ["只改当前项目后端与前端", "禁止部署和修改其他项目"],
                "acceptance": ["pytest 与前端测试必须通过", "最终 SHA 和截图证据可核验"],
                "sizing_inputs": sizing,
                "provider": "codex",
                "role_providers": {
                    "backend": "codex",
                    "frontend": "codex",
                    "verification": "codex",
                },
                "role_templates": {
                    role: templates[role]
                    for role in ("backend", "frontend", "verification")
                },
                "role_rules": {
                    role: rules
                    for role in ("backend", "frontend", "verification")
                },
                "plan_items": [
                    {
                        "ordinal": 1,
                        "role": "backend",
                        "kind": "implementation",
                        "title": "Backend",
                        "objective": "实现规划 API 与 Ledger 合同",
                        "depends_on_ordinals": [],
                        "provider": "codex",
                    },
                    {
                        "ordinal": 2,
                        "role": "frontend",
                        "kind": "implementation",
                        "title": "Frontend",
                        "objective": "实现项目过滤和管家交互",
                        "depends_on_ordinals": [1],
                        "provider": "codex",
                    },
                    {
                        "ordinal": 3,
                        "role": "verification",
                        "kind": "verification",
                        "title": "Verification",
                        "objective": "运行命令并生成确定性证据",
                        "depends_on_ordinals": [2],
                        "provider": "codex",
                    },
                ],
            }
        }
        bridge = PlannerBridge(proposal)
        app.state.provider_pool.bridge = bridge
        app.state.butler_planner.provider_pool.bridge = bridge
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "planner", "path": str(project_path)},
            ).json()
            response = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "natural-planner-intake"},
                json={
                    "instruction": (
                        "实现跨后端和前端的项目管家规划，最后由验证角色给出证据"
                    )
                },
            )
            assert response.status_code == 201, response.text
            conversation = response.json()
            assert conversation["status"] == "awaiting_confirmation", (
                conversation,
                client.get("/api/usage").json()["calls"],
            )
            assert conversation["planner"]["status"] == "planned"
            assert conversation["external_session_id"] == "planner-session-1"
            assert bridge.session_ids == [None]
            assert conversation["spec"]["sizing_inputs"] == sizing
            assert [item["role"] for item in conversation["spec"]["plan_items"]] == [
                "backend", "frontend", "verification",
            ]

            replay = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "natural-planner-intake"},
                json={
                    "instruction": (
                        "实现跨后端和前端的项目管家规划，最后由验证角色给出证据"
                    )
                },
            )
            assert replay.status_code == 201, replay.text
            assert replay.json()["revision"] == conversation["revision"]
            assert replay.json()["planner"] == conversation["planner"]
            assert bridge.execute_calls == 1

            calls = client.get(
                "/api/usage", params={"project_id": project["id"]}
            ).json()["calls"]
            planner_call = next(
                call for call in calls if call["call_kind"] == "butler_planner"
            )
            assert planner_call["status"] == "completed"
            assert planner_call["proposal_revision"] == 0
            assert planner_call["raw_status"] == "returncode:0"
            assert planner_call["total_tokens"] == 200

            confirmed = client.post(
                f"/api/projects/{project['id']}/butler/conversations/"
                f"{conversation['id']}/confirm",
                headers={"Idempotency-Key": "natural-planner-confirm"},
                json={
                    "expected_revision": conversation["revision"],
                    "proposal_hash": conversation["proposal_hash"],
                    "actor_type": "human",
                },
            )
            assert confirmed.status_code == 200, confirmed.text
            goal = client.get(
                f"/api/goals/{confirmed.json()['goal_id']}"
            ).json()
            assert [item["role"] for item in goal["work_items"]] == [
                "backend", "frontend", "verification",
            ]
            assert [item["status"] for item in goal["work_items"]] == [
                "ready", "paused", "paused",
            ]
            assert goal["plan"]["model_invoked"] is True
            instances = app.state.role_instance_repository.list_instances(
                goal_id=goal["id"]
            )
            assert len(instances) == 3
            frontend = next(item for item in instances if item["role_kind"] == "frontend")
            assert frontend["template_id"] == "tmpl.frontend"
            assert frontend["template_revision"] == 1
            ruleset = {
                f"{item['id']}@{item['revision']}"
                for item in frontend["snapshot"]["ruleset"]
            }
            assert ruleset == set(rules)
            bindings = app.state.role_instance_repository.list_bindings(
                project_id=project["id"]
            )
            assert len({item["task_id"] for item in bindings}) == 3


def test_global_butler_keeps_one_scrollable_history_and_resumes_same_session() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        bridge = PlannerBridge({"answer": "canonical project overview"})
        app.state.provider_pool.bridge = bridge
        with TestClient(app) as client:
            client.post(
                "/api/projects",
                json={"name": "global-chat", "path": str(project_path)},
            )
            started = client.post(
                "/api/butlers/global/conversations",
                headers={"Idempotency-Key": "global-butler-chat-start"},
                json={"instruction": "列出当前项目状态"},
            )
            assert started.status_code == 201, started.text
            conversation = started.json()
            assert conversation["scope"] == "global"
            assert conversation["project_id"] is None
            assert conversation["external_session_id"] == "planner-session-1"
            assert [item["sender_type"] for item in conversation["messages"]] == [
                "human", "global_butler"
            ]

            continued = client.post(
                f"/api/butlers/global/conversations/{conversation['id']}/messages",
                headers={"Idempotency-Key": "global-butler-chat-next"},
                json={
                    "expected_revision": conversation["revision"],
                    "content": "哪个项目有进行中任务？",
                },
            )
            assert continued.status_code == 200, continued.text
            current = continued.json()
            assert len(current["messages"]) == 4
            assert current["external_session_id"] == "planner-session-1"
            assert bridge.session_ids == [None, "planner-session-1"]
            listed = client.get(
                "/api/butlers/global/conversations"
            ).json()
            assert [item["id"] for item in listed] == [conversation["id"]]


def test_project_usage_filter_does_not_leak_other_project_calls() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory) / "runtime"))
        first_path = Path(directory) / "one"
        second_path = Path(directory) / "two"
        first_path.mkdir()
        second_path.mkdir()
        with TestClient(app) as client:
            first = client.post(
                "/api/projects", json={"name": "one", "path": str(first_path)}
            ).json()
            second = client.post(
                "/api/projects", json={"name": "two", "path": str(second_path)}
            ).json()
            for project, tokens in ((first, 7), (second, 13)):
                call = app.state.model_calls.prepare(
                    idempotency_key=f"usage-{project['id']}",
                    call_kind="router",
                    provider="internal",
                    project_id=project["id"],
                )
                app.state.model_calls.dispatched(call["call_id"])
                app.state.model_calls.settle(
                    call["call_id"], {"input_tokens": tokens}
                )
            filtered = client.get(
                "/api/usage", params={"project_id": first["id"]}
            ).json()
            assert filtered["project_id"] == first["id"]
            assert filtered["total_tokens"] == 7
            assert {call["project_id"] for call in filtered["calls"]} == {first["id"]}


def test_invalid_planner_output_is_ledgered_and_falls_back_without_dispatch() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(
            data_dir=root / "runtime",
            host_bridge_token="test-token-is-long-enough-123",
        ))
        bridge = InvalidPlannerBridge({})
        app.state.provider_pool.bridge = bridge
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "fallback", "path": str(project_path)},
            ).json()
            response = client.post(
                f"/api/projects/{project['id']}/butler/conversations",
                headers={"Idempotency-Key": "invalid-planner-intake"},
                json={
                    "instruction": (
                        "实现后端 API 和前端界面，并由 verification 运行五条测试"
                    )
                },
            )
            assert response.status_code == 201
            conversation = response.json()
            assert conversation["status"] == "clarifying"
            assert conversation["expected_field"] == "boundaries"
            assert conversation["goal_id"] is None
            assert conversation["spec"]["sizing_inputs"]["layers_touched"] == 2
            assert [item["role"] for item in conversation["spec"]["plan_items"]] == [
                "backend", "frontend", "verification",
            ]
            calls = client.get(
                "/api/usage", params={"project_id": project["id"]}
            ).json()["calls"]
            assert len(calls) == 1
            assert calls[0]["call_kind"] == "butler_planner"
            assert calls[0]["status"] == "failed"
            assert calls[0]["error_class"] == "DomainError"
            assert calls[0]["proposal_revision"] == 0
            assert calls[0]["total_tokens"] == 35
