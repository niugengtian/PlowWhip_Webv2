from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import DomainError
from plow_whip_web.runtime.rule_library import (
    AGENCY_AGENTS_ZH_SOURCE,
    bundled_role_templates,
    bundled_rules,
    is_local_deterministic_worker,
    seed_templates,
)
from plow_whip_web.store.database import Database


def _sizing(**overrides: object) -> dict[str, object]:
    payload = {
        "layers_touched": 1,
        "components_touched": 2,
        "estimated_files_changed": 3,
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
    }
    payload.update(overrides)
    return payload


def test_rule_library_and_templates_are_versioned_with_attribution() -> None:
    rules = bundled_rules()
    assert {rule["id"] for rule in rules} >= {
        "dev.think_before_coding",
        "dev.simplicity_first",
        "dev.surgical_changes",
        "dev.goal_driven_execution",
        "project_butler.one_question_95",
        "global_butler.readonly_route",
    }
    for rule in rules:
        assert rule["revision"] >= 1
        assert rule["content_hash"]
        assert rule["enforcement"] in {"code", "context", "verification"}
        assert rule["source"]
        assert rule["license"]

    templates = {item["id"]: item for item in bundled_role_templates()}
    for key in (
        "tmpl.frontend", "tmpl.backend", "tmpl.verification",
        "tmpl.devops_sre", "tmpl.review_security",
        "tmpl.project_butler", "tmpl.global_butler",
    ):
        assert key in templates
    for tmpl_id in ("tmpl.frontend", "tmpl.backend", "tmpl.verification", "tmpl.devops_sre"):
        assert set(templates[tmpl_id]["rule_ids"]) == {
            "dev.think_before_coding",
            "dev.simplicity_first",
            "dev.surgical_changes",
            "dev.goal_driven_execution",
        }
        assert templates[tmpl_id]["source_refs"]
        assert templates[tmpl_id]["source_refs"][0]["repository"] == AGENCY_AGENTS_ZH_SOURCE
        assert templates[tmpl_id]["source_refs"][0]["license"] == "MIT"
        assert templates[tmpl_id]["source_refs"][0]["upstream_commit"]
        assert templates[tmpl_id]["source_refs"][0]["source_content_sha256"]
    assert templates["tmpl.project_butler"]["rule_ids"] == ["project_butler.one_question_95"]
    assert templates["tmpl.global_butler"]["rule_ids"] == ["global_butler.readonly_route"]
    assert "project_butler.one_question_95" not in templates["tmpl.global_butler"]["rule_ids"]
    assert "global_butler.readonly_route" not in templates["tmpl.project_butler"]["rule_ids"]
    assert "dev.think_before_coding" not in templates["tmpl.project_butler"]["rule_ids"]


def test_runtime_rules_and_templates_come_from_database_after_seed() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            rules = client.get("/api/rules").json()["items"]
            templates = client.get("/api/role-templates").json()["items"]
            assert len(rules) >= 7
            assert len(templates) >= 7
            assert all("content_hash" in item for item in rules)
            assert all("template_hash" in item for item in templates)
            # Mutate DB directly; runtime must reflect DB, not Python seed.
            with app.state.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO rule_versions(
                        rule_id, revision, scope, source, license, content,
                        content_hash, applies_to_json, mandatory, enforcement, status
                    ) VALUES (
                        'test.db_only_rule', 1, 'development', 'db', 'MIT',
                        'db-only rule', 'abc', '["backend"]', 1, 'context', 'active'
                    )
                    """
                )
            again = client.get("/api/rules").json()["items"]
            assert any(item["id"] == "test.db_only_rule" for item in again)
            # Restart app against same DB: still DB truth.
        app2 = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app2) as client2:
            persisted = client2.get("/api/rules").json()["items"]
            assert any(item["id"] == "test.db_only_rule" for item in persisted)
            # Seed is idempotent and does not wipe the DB-only rule.
            seeded = app2.state.role_instance_repository.seed_catalog_if_empty()
            assert seeded == {"rules": 0, "templates": 0}


def test_project_create_has_exactly_one_butler_and_no_prebuilt_dev_roles() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "one-butler", "path": str(project_path)},
            ).json()
            kinds = [role["kind"] for role in project["roles"]]
            assert kinds == ["butler"]
            assert all(not role.get("legacy") for role in project["roles"])
            global_id = client.get("/api/rules").json()
            assert global_id is not None
            with app.state.database.connect() as connection:
                row = connection.execute(
                    "SELECT role_kind FROM global_butler_identity WHERE id='global'"
                ).fetchone()
                assert row["role_kind"] == "global_butler"


def test_goal_creates_role_instances_and_session_bindings_preserving_identity() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "roles", "path": str(project_path)},
            ).json()
            created = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "role-instance-goal-1"},
                json={
                    "title": "named roles",
                    "objective": "preserve backend frontend devops_sre identity",
                    "project_id": project["id"],
                    "provider": "cursor",
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "scope": ["backend", "frontend", "devops_sre"],
                    "acceptance": ["role instances exist"],
                    "artifacts": [],
                    "constraints": [],
                    "sizing_inputs": _sizing(),
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "backend",
                            "kind": "implementation",
                            "title": "api",
                            "objective": "api",
                            "depends_on_ordinals": [],
                        },
                        {
                            "ordinal": 2,
                            "role": "frontend",
                            "kind": "implementation",
                            "title": "web",
                            "objective": "web",
                            "depends_on_ordinals": [],
                        },
                        {
                            "ordinal": 3,
                            "role": "devops_sre",
                            "kind": "implementation",
                            "title": "ops",
                            "objective": "ops",
                            "depends_on_ordinals": [],
                        },
                    ],
                    "command": {
                        "argv": ["python3", "-c", "print('ok')"],
                        "timeout_seconds": 30,
                    },
                },
            )
            assert created.status_code == 201, created.text
            goal = created.json()
            roles = [item["role"] for item in goal["work_items"]]
            assert roles == ["backend", "frontend", "devops_sre", "verification"]
            assert goal["work_items"][0]["status"] == "ready"
            assert goal["work_items"][1]["status"] == "paused"
            assert goal["work_items"][2]["status"] == "paused"

            instances = client.get(
                "/api/role-instances",
                params={"goal_id": goal["id"]},
            ).json()["items"]
            assert len(instances) == 4
            by_role = {item["role_kind"]: item for item in instances}
            assert set(by_role) == {
                "backend", "frontend", "devops_sre", "verification"
            }
            for role, item in by_role.items():
                assert item["template_id"].startswith("tmpl.")
                assert item["instance_hash"]
                assert item["ruleset_hash"]
                assert item["source_chain"]["template_id"]
                assert item["status"] == "active"
                assert item["match_reason"]

            bindings = client.get(
                "/api/session-bindings",
                params={"project_id": project["id"]},
            ).json()["items"]
            assert len(bindings) == 4
            assert {item["task_id"] for item in bindings} == {
                item["id"] for item in goal["work_items"]
            }


def test_dispatch_rejects_missing_role_instance_unless_local_deterministic_worker() -> None:
    assert is_local_deterministic_worker(
        provider="generic-command",
        command={"argv": ["python3", "-c", "print(1)"]},
        model_invoked=False,
    )
    assert not is_local_deterministic_worker(
        provider="cursor",
        command={"argv": ["python3", "-c", "print(1)"]},
        model_invoked=False,
    )
    assert not is_local_deterministic_worker(
        provider="simple-worker",
        command={"argv": ["python3", "-c", "print(1)"]},
        model_invoked=True,
    )
    assert not is_local_deterministic_worker(
        provider="simple-worker",
        command={"argv": ["cursor", "agent"]},
        model_invoked=False,
    )
    assert not is_local_deterministic_worker(
        provider="generic-command",
        command={"argv": []},
        model_invoked=False,
    )

    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="gate", path=str(project_path)
        )
        role = app.state.project_repository.resolve_role(project["id"], "backend")
        # Manual task without RoleInstance (legacy path) must be rejected for cursor.
        task = app.state.task_repository.create(
            title="no instance",
            objective="must reject",
            project_path=str(project_path),
            project_id=project["id"],
            role_id=role["role_id"],
            provider="cursor",
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="no-instance-cursor",
        )
        with pytest.raises(DomainError, match="missing RoleInstance"):
            app.state.role_instance_repository.require_dispatchable(
                task_id=task.id,
                provider="cursor",
                command=task.command,
                model_invoked=True,
                expected_task_spec_revision=task.spec_revision,
            )

        local = app.state.task_repository.create(
            title="local ok",
            objective="simple worker exception",
            project_path=str(project_path),
            project_id=project["id"],
            role_id=role["role_id"],
            provider="generic-command",
            command={"argv": ["python3", "-c", "print('ok')"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key="simple-worker-ok",
        )
        allowed = app.state.role_instance_repository.require_dispatchable(
            task_id=local.id,
            provider="generic-command",
            command=local.command,
            model_invoked=False,
            expected_task_spec_revision=local.spec_revision,
        )
        assert allowed["exception"] == "local_deterministic_worker"


def test_amend_replaces_role_instance_and_session_generation() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "amend", "path": str(project_path)},
            ).json()
            created = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "amend-role-1"},
                json={
                    "title": "amend",
                    "objective": "replace instance on amend",
                    "project_id": project["id"],
                    "provider": "cursor",
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "scope": ["backend"],
                    "acceptance": ["new generation"],
                    "artifacts": [],
                    "constraints": [],
                    "sizing_inputs": _sizing(),
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "backend",
                            "kind": "implementation",
                            "title": "api",
                            "objective": "api",
                            "depends_on_ordinals": [],
                        }
                    ],
                    "command": {
                        "argv": ["python3", "-c", "print('ok')"],
                        "timeout_seconds": 30,
                    },
                },
            ).json()
            task_id = created["work_items"][0]["id"]
            before = client.get(
                "/api/role-instances", params={"task_id": task_id}
            ).json()["items"][0]
            replaced = app.state.role_instance_repository.replace_instance_for_amend(
                task_id=task_id,
                task_spec_revision=2,
                provider="cursor",
            )
            assert replaced["id"] != before["id"]
            assert replaced["task_spec_revision"] == 2
            assert replaced["session_binding"]["session_generation"] >= 2
            old = app.state.role_instance_repository.get_instance(before["id"])
            assert old["status"] == "replaced"
            assert old["replaced_by"] == replaced["id"]


def test_migration_0030_fresh_and_idempotent() -> None:
    with TemporaryDirectory() as directory:
        db_path = Path(directory) / "control.db"
        first = Database(db_path).migrate()
        assert "0030_role_instances_session_bindings.sql" in first
        second = Database(db_path).migrate()
        assert second == []
        with Database(db_path).connect() as connection:
            tables = {
                item[0]
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "role_instances" in tables
            assert "session_bindings" in tables
            assert "rule_versions" in tables
            assert "role_template_versions" in tables
            assert "project_role_rules" in tables


def test_project_role_rule_overlay_does_not_mutate_template_or_other_projects() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        a_path = root / "a"
        b_path = root / "b"
        a_path.mkdir()
        b_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project_a = client.post(
                "/api/projects", json={"name": "a", "path": str(a_path)}
            ).json()
            project_b = client.post(
                "/api/projects", json={"name": "b", "path": str(b_path)}
            ).json()
            with app.state.database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO rule_versions(
                        rule_id, revision, scope, source, license, content,
                        content_hash, applies_to_json, mandatory, enforcement, status
                    ) VALUES (
                        'project.local.boundary', 1, 'project', 'owner', 'MIT',
                        '### Project Overlay\nUse api/ only', 'hash-local',
                        '["backend"]', 1, 'context', 'active'
                    )
                    """
                )
            overlay = client.post(
                f"/api/projects/{project_a['id']}/role-rules",
                json={
                    "rule_id": "project.local.boundary",
                    "reason": "stack conflict with generic backend template",
                    "source": "owner",
                    "capability": "backend",
                    "template_id": "tmpl.backend",
                },
            )
            assert overlay.status_code == 201, overlay.text
            listed_a = client.get(
                f"/api/projects/{project_a['id']}/role-rules"
            ).json()["items"]
            listed_b = client.get(
                f"/api/projects/{project_b['id']}/role-rules"
            ).json()["items"]
            assert len(listed_a) == 1
            assert listed_b == []
            templates_before = {
                item["template_id"]: item["template_hash"]
                for item in client.get("/api/role-templates").json()["items"]
            }
            goal = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "overlay-goal-1"},
                json={
                    "title": "overlay",
                    "objective": "project overlay in snapshot",
                    "project_id": project_a["id"],
                    "provider": "cursor",
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "scope": ["backend"],
                    "acceptance": ["overlay visible"],
                    "artifacts": [],
                    "constraints": [],
                    "sizing_inputs": _sizing(),
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "backend",
                            "kind": "implementation",
                            "title": "api",
                            "objective": "api",
                            "depends_on_ordinals": [],
                        }
                    ],
                    "command": {
                        "argv": ["python3", "-c", "print('ok')"],
                        "timeout_seconds": 30,
                    },
                },
            )
            assert goal.status_code == 201, goal.text
            task_id = goal.json()["work_items"][0]["id"]
            instance = client.get(
                "/api/role-instances", params={"task_id": task_id}
            ).json()["items"][0]
            detail = app.state.role_instance_repository.get_instance(instance["id"])
            ruleset = {item["id"]: item for item in detail["snapshot"]["ruleset"]}
            assert "project.local.boundary" in ruleset
            assert ruleset["project.local.boundary"]["precedence"] == "project_role_rule"
            templates_after = {
                item["template_id"]: item["template_hash"]
                for item in client.get("/api/role-templates").json()["items"]
            }
            assert templates_after["tmpl.backend"] == templates_before["tmpl.backend"]


def test_template_reuse_and_structural_dedup_on_generate() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        project = app.state.project_repository.create(
            name="dedup", path=str(project_path)
        )
        repo = app.state.role_instance_repository
        with app.state.database.transaction(immediate=True) as connection:
            first = repo.select_or_create_template(
                connection,
                role_kind="backend",
                provider="cursor",
                work_item={"boundaries": [], "deliverables": [], "verification": []},
                project_id=project["id"],
                task_id=None,
            )
            assert first["match"]["reused"] is True
            assert first["template_id"] == "tmpl.backend"
            generated = repo.select_or_create_template(
                connection,
                role_kind="custom_capability_x",
                provider="cursor",
                work_item={
                    "boundaries": ["svc"],
                    "deliverables": ["proof"],
                    "verification": ["exit_code"],
                },
                project_id=project["id"],
                task_id=None,
            )
            assert generated["match"]["reused"] is False
            assert generated["generated_by_project_butler"] in {1, True}
            again = repo.select_or_create_template(
                connection,
                role_kind="custom_capability_x",
                provider="cursor",
                work_item={
                    "boundaries": ["svc"],
                    "deliverables": ["proof"],
                    "verification": ["exit_code"],
                },
                project_id=project["id"],
                task_id=None,
            )
            assert again["match"]["reused"] is True
            assert again["template_id"] == generated["template_id"]
            assert again["template_hash"] == generated["template_hash"]
            count = connection.execute(
                """
                SELECT COUNT(*) FROM role_template_versions
                WHERE capability_key = 'custom_capability_x' AND status = 'active'
                """
            ).fetchone()[0]
            assert count == 1


def test_context_compiler_reads_role_instance_snapshot_rules() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_path = root / "project"
        project_path.mkdir()
        app = create_app(Settings(data_dir=root / "runtime"))
        app.state.provider_pool.require_ready = lambda _name: {}
        with TestClient(app) as client:
            project = client.post(
                "/api/projects",
                json={"name": "ctx", "path": str(project_path)},
            ).json()
            goal = client.post(
                "/api/goals",
                headers={"Idempotency-Key": "ctx-snapshot-1"},
                json={
                    "title": "ctx",
                    "objective": "compile from snapshot",
                    "project_id": project["id"],
                    "provider": "cursor",
                    "verification": [{"kind": "exit_code", "expected": 0}],
                    "scope": ["backend"],
                    "acceptance": ["snapshot rules"],
                    "artifacts": [],
                    "constraints": [],
                    "sizing_inputs": _sizing(),
                    "plan_items": [
                        {
                            "ordinal": 1,
                            "role": "backend",
                            "kind": "implementation",
                            "title": "api",
                            "objective": "api",
                            "depends_on_ordinals": [],
                        }
                    ],
                    "command": {
                        "argv": ["python3", "-c", "print('ok')"],
                        "timeout_seconds": 30,
                    },
                },
            ).json()
            task_id = goal["work_items"][0]["id"]
            compiled = app.state.context_compiler.compile(task_id)
            assert compiled["role_instance"] is not None
            assert any(
                item.get("kind") == "role_instance_rule"
                for item in compiled["injected_sources"]
            )
            assert "### Think Before Coding" in compiled["content"]
            assert compiled["role_instance"]["ruleset"]
