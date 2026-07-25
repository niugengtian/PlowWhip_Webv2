import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from plowwhip.app import make_server
from plowwhip.artifact_contract import register_artifact, task_data_scope
from plowwhip.butler import conversation, route_global_message, semantic_search
from plowwhip.continuity import checkpoint_project, compile_hot_context
from plowwhip.cronner import (
    acquire_scheduler_lock,
    run as run_cronner,
    run_until_idle,
    tick,
)
from plowwhip.execution import (
    ProviderStep,
    _fallback_provider_generation,
    _root_provider_execution,
    perform_provider_step,
)
from plowwhip.intake import (
    archive_project,
    canonical_json,
    create_project,
    normalize_instruction,
    set_project_rule,
    set_project_setting,
    submit_action,
    submit_message,
)
from plowwhip.lifecycle import (
    LeaseLost,
    _ensure_project_question,
    _materialize_plan,
    _validate_promotable_python,
    advance_project,
)
from plowwhip.lifecycle_state import lifecycle_write_scope
from plowwhip.monitor import (
    monitor_snapshot,
    projects_snapshot,
    settings_library_snapshot,
    snapshot,
    task_file,
    token_snapshot,
)
from plowwhip.planner import (
    PLANNER_RESULT_PREFIX,
    instruction_facts,
    normalize_plan,
    parse_planner_result,
)
from plowwhip.provider import (
    CHECKER_RESULT_PREFIX,
    HostBridgeError,
    parse_context_events,
    provider_adapter,
    provider_facts,
    record_model_call,
)
from plowwhip.store import Store, candidate_preflight, rollback_preflight
from plowwhip.verification import (
    _parse_checker_verdict,
    perform_checker_step as real_perform_checker_step,
)


def checker_output(
    verdict: str = "PASS", failed: tuple[str, ...] = ()
) -> str:
    acceptances = []
    for acceptance_id in ("owner_instruction", "relevant_checks"):
        passed = acceptance_id not in failed
        acceptances.append(
            {
                "acceptance_id": acceptance_id,
                "passed": passed,
                "actual_evidence": (
                    f"{acceptance_id} verified" if passed else f"{acceptance_id} missing"
                ),
                "recheck_command": "python3 -m unittest -q",
            }
        )
    return (
        "bounded review\n"
        + CHECKER_RESULT_PREFIX
        + json.dumps(
            {
                "verdict": verdict,
                "acceptances": acceptances,
                "decision_reason": None,
            },
            sort_keys=True,
        )
    )


def complete_plan_contract(
    plan: dict,
    *,
    size: str,
    requires_owner_choice: bool = False,
) -> dict:
    plan = json.loads(json.dumps(plan))
    coverage = [f"{item['key']}-result" for item in plan["tasks"]]
    plan["size"] = size
    plan["required_coverage"] = coverage
    plan["selection"] = {
        "mode": "owner_required" if requires_owner_choice else "objective",
        "basis": [
            (
                "owner must choose the real business tradeoff"
                if requires_owner_choice
                else "selected alternative dominates on equal coverage"
            )
        ],
    }
    selected = int(plan["selected"])
    for index, alternative in enumerate(plan["alternatives"]):
        distance = 0 if index == selected else index + 1
        alternative["objective_metrics"] = {
            "coverage": coverage,
            "estimated_effort": 1 + distance,
            "risk_level": min(4, distance),
        }
    for item, coverage_id in zip(plan["tasks"], coverage, strict=True):
        instruction = item.get("instruction")
        if instruction is None and isinstance(item.get("spec"), dict):
            instruction = item["spec"].get("instruction")
        spec, fallback_acceptance = normalize_instruction(instruction)
        acceptance = item.get("acceptance") or [
            {
                "id": value["id"],
                "expected": value.get("expected")
                or value.get("expected_result")
                or f"{value['id']} passes",
            }
            for value in fallback_acceptance
        ]
        item["acceptance"] = acceptance
        acceptance_ids = [value["id"] for value in acceptance]
        dependencies = item.get("depends_on", [])
        role = item.get("role_key")
        if not role:
            role = {
                "write_text": "deterministic",
                "provider_probe": "provider_probe",
                "provider_task": "fullstack",
                "git_publish": "git_publisher",
            }[spec["kind"]]
            item["role_key"] = role
        checker_role = (
            "independent_checker"
            if role == "fullstack"
            or (
                spec["kind"] == "provider_probe"
                and spec["mode"] == "minimal"
            )
            else "deterministic_checker"
        )
        item["responsibility"] = {
            "component": item["key"],
            "deliverable": instruction,
        }
        item["inputs"] = (
            [
                {"kind": "task_result", "task_key": dependency}
                for dependency in dependencies
            ]
            if dependencies
            else [{"kind": "owner_instruction", "source": "goal"}]
        )
        if spec["kind"] == "write_text":
            result_type = "artifact"
            artifact = {"path": spec["target"], "format": "text"}
            authorization = {
                "level": "recoverable",
                "allowed_actions": ["write_workspace"],
                "target_scope": "project_workspace",
            }
        elif spec["kind"] in {"provider_probe", "git_publish"}:
            result_type = "evidence"
            artifact = None
            authorization = (
                {
                    "level": "external",
                    "allowed_actions": ["git_publish"],
                    "target_scope": (
                        f"{spec['remote_ssh']}#refs/heads/{spec['branch']}"
                    ),
                }
                if spec["kind"] == "git_publish"
                else {
                    "level": "none",
                    "allowed_actions": ["probe_provider"],
                    "target_scope": f"provider:{spec['provider_key']}",
                }
            )
        elif spec["workspace_change_required"]:
            result_type = "workspace_change"
            artifact = None
            authorization = {
                "level": "recoverable",
                "allowed_actions": ["write_workspace", "run_checks"],
                "target_scope": "project_workspace",
            }
        else:
            result_type = "evidence"
            artifact = None
            authorization = {
                "level": "none",
                "allowed_actions": ["read_workspace"],
                "target_scope": "project_workspace",
            }
        item["result"] = {
            "type": result_type,
            "coverage": [coverage_id],
            "acceptance_ids": acceptance_ids,
        }
        if artifact is not None:
            item["result"]["artifact"] = artifact
        item["checker"] = {
            "role_key": checker_role,
            "acceptance_ids": acceptance_ids,
        }
        item["authorization_boundary"] = authorization
        if size == "large":
            item["max_runtime_seconds"] = (
                item.get("settings", {})
                .get(role, {})
                .get("max_runtime_seconds", 600)
            )
    return plan


def complete_planner_payload(planned: dict) -> dict:
    planned = json.loads(json.dumps(planned))
    classification = planned["classification"]
    classification.setdefault("workflow", "standard")
    size = classification["size"]
    final_index = ("simple", "medium", "large").index(size)
    classification["upgrade_path"] = [
        "simple",
        "medium",
        "large",
    ][: final_index + 1]
    classification["upgrade_evidence"] = [
        {
            "from": ("simple", "medium")[index],
            "to": ("medium", "large")[index],
            "facts": [classification["reasons"][0]],
        }
        for index in range(final_index)
    ]
    if planned.get("plan") is not None:
        planned["plan"] = complete_plan_contract(
            planned["plan"],
            size=size,
            requires_owner_choice=classification["requires_owner_choice"],
        )
    return planned


class VerticalSliceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "state.db"
        self.data = self.root / "data"
        self.store = Store(self.db, self.data)
        self.store.initialize()
        self.planner_patcher = patch(
            "plowwhip.lifecycle.perform_planner_step",
            side_effect=self._perform_semantic_planner_step,
        )
        self.planner_patcher.start()
        self.planner_checker_patcher = patch(
            "plowwhip.lifecycle.perform_checker_step",
            side_effect=self._perform_checker_step,
        )
        self.planner_checker_patcher.start()

    def tearDown(self):
        self.planner_checker_patcher.stop()
        self.planner_patcher.stop()
        self.temporary.cleanup()

    def _use_real_planner_adapter(self) -> None:
        self.planner_patcher.stop()

    def _perform_semantic_planner_step(self, step):
        connection = self.store.connect()
        try:
            instruction = connection.execute(
                """
                SELECT goal.objective FROM tasks task
                JOIN goals goal ON goal.id = task.goal_id
                WHERE task.id = ?
                """,
                (step.task_id,),
            ).fetchone()["objective"]
        finally:
            connection.close()
        spec, _ = normalize_instruction(instruction)
        if spec["kind"] == "write_text":
            size, role_key = "simple", "deterministic"
        elif spec["kind"] == "provider_probe":
            size, role_key = "simple", "provider_probe"
        elif spec["kind"] == "git_publish":
            size, role_key = "simple", "git_publisher"
        elif "前端和后端" in instruction or "前后端" in instruction:
            size, role_key = "large", "fullstack"
        else:
            size, role_key = "medium", "fullstack"
        tasks = [
            {
                "key": "task",
                "instruction": instruction,
                "depends_on": [],
                "sprint": 1,
                "role_key": role_key,
            }
        ]
        alternatives = [
            {
                "name": "bounded",
                "scope": "the owner instruction only",
                "cost": "bounded",
                "risk": "bounded",
                "reversible": True,
                "acceptance": "the frozen Task acceptance passes",
            }
        ]
        if size == "large":
            alternatives.append(
                {
                    "name": "integrated",
                    "scope": "the same Goal with wider integration",
                    "cost": "higher",
                    "risk": "higher",
                    "reversible": True,
                    "acceptance": "integrated acceptance passes",
                }
            )
            tasks = [
                {
                    "key": "backend",
                    "instruction": "实现后端刷新接口",
                    "depends_on": [],
                    "sprint": 1,
                    "role_key": "fullstack",
                },
                {
                    "key": "frontend",
                    "instruction": "实现前端刷新按钮",
                    "depends_on": ["backend"],
                    "sprint": 1,
                    "role_key": "fullstack",
                },
            ]
        planned = {
            "classification": {
                "size": size,
                "reasons": [
                    (
                        "one bounded deterministic action"
                        if size == "simple"
                        else "one professional Worker owns the complete boundary"
                    )
                ],
                "information_sufficient": True,
                "requires_owner_choice": False,
            },
            "confidence": 0.99,
            "plan": {
                "summary": "one semantically classified bounded Task",
                "alternatives": alternatives,
                "selected": 0,
                "tasks": tasks,
            },
        }
        planned = complete_planner_payload(planned)
        return {
            "ok": True,
            "state": {
                "status": "completed",
                "returncode": 0,
                "session_id": f"planner-{step.task_id}",
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "model": "semantic-planner-test-double",
            },
            "output": {
                "chunks": [
                    {
                        "stream": "stdout",
                        "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                    }
                ]
            },
        }

    def _perform_checker_step(self, step):
        subject = step.execution.get("subject")
        if subject in {"planner", "model_probe"}:
            acceptance_id = (
                "planner_contract"
                if subject == "planner"
                else "provider_minimal_probe"
            )
            output = (
                CHECKER_RESULT_PREFIX
                + json.dumps(
                    {
                        "verdict": "PASS",
                        "acceptances": [
                            {
                                "acceptance_id": acceptance_id,
                                "passed": True,
                                "actual_evidence": (
                                    f"parsed {subject} Artifact independently "
                                    "satisfies the frozen contract"
                                ),
                                "recheck_command": f"validate {subject} artifact",
                            }
                        ],
                        "decision_reason": None,
                    }
                )
            )
            return {
                "ok": True,
                "state": {
                    "status": "completed",
                    "returncode": 0,
                    "session_id": f"{subject}-checker-{step.task_id}",
                    "input_tokens": 5,
                    "cached_input_tokens": 0,
                    "output_tokens": 5,
                    "model": "planner-checker-test-double",
                },
                "output": {
                    "complete": True,
                    "chunks": [{"stream": "stdout", "text": output}],
                },
            }
        return real_perform_checker_step(step)

    def _create_project(
        self, project_id: str, idempotency_key: str, host_path: str | None = None
    ) -> None:
        create_project(self.store, project_id, idempotency_key, host_path)
        self.assertEqual(tick(self.store)[0]["action"], "create_project")

    def test_plaintext_secrets_are_rejected_before_persistence_or_prompt(self):
        self._create_project("secret-safe", "secret-safe-create", str(self.root))
        plaintext = "ghp_abcdefghijklmnopqrstuvwxyz"
        bad_values = (
            f"请使用 token={plaintext}",
            f"Authorization: Bearer {plaintext}",
            f"https://example.invalid/?X-Amz-Signature={plaintext}",
        )
        for index, value in enumerate(bad_values):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, "plaintext Secret"):
                    submit_message(
                        self.store,
                        "secret-safe",
                        value,
                        f"secret-message-{index}",
                    )
        with self.assertRaisesRegex(ValueError, "plaintext Secret"):
            route_global_message(
                self.store,
                f"@secret-safe api_key={plaintext}",
                "secret-global",
            )
        with self.assertRaisesRegex(ValueError, "plaintext Secret"):
            set_project_rule(
                self.store,
                "secret-safe",
                "unsafe-rule",
                f"password={plaintext}",
                "secret-rule",
            )
        with self.assertRaisesRegex(ValueError, "plaintext Secret"):
            submit_action(
                self.store,
                "secret-safe",
                "a" * 32,
                "provide_decision",
                f"secret={plaintext}",
                "secret-action",
            )

        opaque = "credential-ref://providers/codex-primary"
        submit_message(
            self.store,
            "secret-safe",
            f"使用 {opaque} 对当前代码进行只读检查",
            "opaque-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        connection = self.store.connect()
        try:
            persisted = "\n".join(connection.iterdump())
        finally:
            connection.close()
        self.assertIn(opaque, persisted)
        self.assertNotIn(plaintext, persisted)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(plaintext.encode(), path.read_bytes())

    def test_every_formal_instruction_is_semantically_planned_before_execution(self):
        cases = (
            ("formal-simple", "写入 result.txt: 完成", "simple", 1),
            ("formal-medium", "实现一个项目内刷新按钮", "medium", 1),
            ("formal-large", "前端和后端增加刷新能力", "large", 2),
        )
        for index, (
            project_id,
            instruction,
            expected_size,
            expected_tasks,
        ) in enumerate(cases):
            with self.subTest(size=expected_size):
                if index:
                    self.temporary.cleanup()
                    self.temporary = tempfile.TemporaryDirectory()
                    self.root = Path(self.temporary.name)
                    self.db = self.root / "state.db"
                    self.data = self.root / "data"
                    self.store = Store(self.db, self.data)
                    self.store.initialize()
                self._create_project(
                    project_id,
                    f"{project_id}-create",
                    str(self.root),
                )
                submit_message(
                    self.store,
                    project_id,
                    instruction,
                    f"{project_id}-message",
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                connection = self.store.connect()
                try:
                    placeholder = connection.execute(
                        "SELECT * FROM tasks WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                    sessions = connection.execute(
                        """
                        SELECT role_key FROM task_sessions
                        WHERE task_id = ? ORDER BY role_key
                        """,
                        (placeholder["id"],),
                    ).fetchall()
                    jobs = connection.execute(
                        "SELECT COUNT(*) AS value FROM host_jobs WHERE task_id = ?",
                        (placeholder["id"],),
                    ).fetchone()["value"]
                finally:
                    connection.close()
                placeholder_spec = json.loads(placeholder["spec_json"])
                self.assertEqual(placeholder["phase"], "plan")
                self.assertEqual(
                [row["role_key"] for row in sessions],
                ["independent_checker", "planner"],
                )
                self.assertEqual(jobs, 0)
                self.assertEqual(placeholder_spec["kind"], "planner_intake")
                self.assertNotIn("size", placeholder_spec["input_facts"])
                self.assertEqual(
                    placeholder_spec["input_facts"],
                    instruction_facts(
                        instruction,
                        placeholder_spec["normalized_candidate"]["kind"],
                    ),
                )

                self.assertEqual(tick(self.store)[0]["action"], "planner_check")
                self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
                connection = self.store.connect()
                try:
                    tasks = connection.execute(
                        """
                        SELECT id, phase FROM tasks
                        WHERE project_id = ? ORDER BY rowid
                        """,
                        (project_id,),
                    ).fetchall()
                    selected_plan = json.loads(
                        connection.execute(
                            """
                            SELECT summary_json FROM plans
                            WHERE goal_id = ? AND selected = 1
                            ORDER BY revision DESC LIMIT 1
                            """,
                            (placeholder["goal_id"],),
                        ).fetchone()["summary_json"]
                    )
                    planner_job = connection.execute(
                        """
                        SELECT status, output_ref FROM host_jobs
                        WHERE task_id = ? AND purpose = 'command'
                        """,
                        (placeholder["id"],),
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual(len(tasks), expected_tasks)
                self.assertEqual(selected_plan["size"], expected_size)
                self.assertEqual(planner_job["status"], "succeeded")
                self.assertTrue(planner_job["output_ref"])

    def test_message_to_verified_done(self):
        first = submit_message(
            self.store, "project-a", "写入 result.txt: 闭环完成", "request-1"
        )
        duplicate = submit_message(
            self.store, "project-a", "ignored duplicate", "request-1"
        )
        self.assertEqual(first, duplicate)

        with self.assertRaises(LeaseLost):
            advance_project(self.store, "project-a", "not-a-lease", 0)

        actions = run_until_idle(self.store)
        self.assertEqual(
            [item["action"] for item in actions],
            ["intake", "planner_check", "plan_applied", "execute", "verify"],
        )

        before = self._row_counts()
        view = snapshot(self.db, self.data, "project-a")
        after = self._row_counts()
        self.assertEqual(before, after, "Monitor must not mutate canonical state")
        self.assertEqual(view["task"]["public_status"], "done")
        self.assertEqual(view["task"]["outcome"], "done")
        self.assertEqual(view["goals"][0]["objective"], "写入 result.txt: 闭环完成")
        self.assertEqual(view["goals"][0]["public_status"], "done")
        self.assertEqual(view["tasks"][0]["provider_key"], "local")
        self.assertEqual(view["tasks"][0]["normalized_total"], 30)
        self.assertEqual(
            [item["kind"] for item in view["artifacts"]],
            ["artifact", "evidence", "artifact", "evidence"],
        )
        evidence_row = next(
            item
            for item in view["artifacts"]
            if item["kind"] == "evidence"
            and item["acceptance_id"] == "artifact_content_sha256"
        )
        evidence_body, file_metadata = task_file(
            self.db,
            self.data,
            view["task"]["id"],
            evidence_row["file_id"],
        )
        evidence = json.loads(evidence_body)
        self.assertEqual(file_metadata["sha256"], evidence_row["sha256"])
        self.assertEqual(
            evidence_row["open_url"],
            f"/api/tasks/{view['task']['id']}/files/{evidence_row['file_id']}",
        )
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["acceptance_id"], "artifact_content_sha256")
        self.assertEqual(
            view["observation_tail"],
            [json.dumps(evidence, ensure_ascii=False, sort_keys=True)],
        )
        self.assertIn("不是 Artifact", view["observation_notice"])
        promoted = [
            item
            for item in settings_library_snapshot(self.db, self.data)["library"]
            if item["scope"] == "project"
            and item["project_id"] == "project-a"
            and item["kind"] == "worker_template"
        ]
        self.assertEqual(promoted, [])
        self.assertEqual(
            len(
                list(
                    (
                        self.data
                        / "projects"
                        / "project-a"
                        / "conversations"
                    ).glob("*.json")
                )
            ),
            1,
        )
        self.assertEqual(
            len(list((self.data / "global" / "conversations").glob("*.json"))),
            1,
        )

        connection = self.store.connect()
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM host_jobs").fetchone()[0], 4)
            sessions = connection.execute(
                "SELECT role_key, role_snapshot_json, settings_json FROM task_sessions ORDER BY role_key"
            ).fetchall()
            generations = connection.execute(
                "SELECT generation, status, handoff_ref FROM session_generations"
            ).fetchall()
            jobs = connection.execute(
                "SELECT task_session_id, session_generation, purpose FROM host_jobs ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [row["role_key"] for row in sessions],
            [
                "deterministic",
                "deterministic_checker",
                "independent_checker",
                "planner",
            ],
        )
        self.assertTrue(all(json.loads(row["settings_json"])["sources"] for row in sessions))
        role_snapshot = json.loads(sessions[0]["settings_json"])
        self.assertEqual(role_snapshot["sources"]["max_runtime_seconds"], "v1_default")
        self.assertEqual(
            role_snapshot["budget_contract"]["effective"]["context_max_bytes"][
                "source"
            ],
            "v1_default",
        )
        self.assertIsInstance(
            role_snapshot["budget_contract"]["warnings"], list
        )
        self.assertEqual(len(json.loads(sessions[0]["role_snapshot_json"])["library"]), 3)
        self.assertEqual(
            [(row["generation"], row["status"]) for row in generations],
            [
                (1, "archived"),
                (1, "archived"),
                (1, "archived"),
                (1, "archived"),
            ],
        )
        self.assertTrue(all(row["handoff_ref"] for row in generations))
        self.assertEqual(
            [row["purpose"] for row in jobs],
            ["command", "check", "execute", "check"],
        )
        self.assertEqual(len({row["task_session_id"] for row in jobs}), 4)
        self.assertEqual({row["session_generation"] for row in jobs}, {1})
        handoffs = list((self.data / "projects" / "project-a" / "tasks" / view["task"]["id"] / "handoffs").glob("*/current.json"))
        self.assertEqual(len(handoffs), 4)

    def test_default_settings_upgrade_without_overwriting_project_policy(self):
        old_provider_order = {
            "deterministic": ["local"],
            "deterministic_checker": ["local"],
        }
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE settings SET value_json = ?
                WHERE scope = 'global' AND setting_key = 'provider_order'
                """,
                (json.dumps(old_provider_order),),
            )
            connection.execute(
                """
                UPDATE settings SET value_json = '5', source = 'owner'
                WHERE scope = 'global' AND setting_key = 'retry_count'
                """
            )

        self.store.initialize()

        connection = self.store.connect()
        try:
            rows = {
                row["setting_key"]: row
                for row in connection.execute(
                    """
                    SELECT setting_key, value_json, source FROM settings
                    WHERE scope = 'global'
                      AND setting_key IN ('provider_order', 'retry_count')
                    """
                )
            }
        finally:
            connection.close()
        provider_order = json.loads(rows["provider_order"]["value_json"])
        self.assertEqual(
            provider_order["provider_probe"],
            ["cursor_cli", "deepseek"],
        )
        self.assertEqual(
            provider_order["local_script_runner"],
            ["local_script"],
        )
        self.assertEqual(rows["provider_order"]["source"], "v1_default")
        self.assertEqual(json.loads(rows["retry_count"]["value_json"]), 5)
        self.assertEqual(rows["retry_count"]["source"], "owner")

    def test_project_setting_is_queued_then_frozen_into_new_session(self):
        self._create_project("configured", "configured-create")
        with self.assertRaisesRegex(ValueError, "allowed bounded"):
            set_project_setting(
                self.store,
                "configured",
                "max_runtime_seconds",
                0,
                "configured-invalid",
            )
        set_project_setting(
            self.store,
            "configured",
            "max_runtime_seconds",
            45,
            "configured-setting",
        )
        connection = self.store.connect()
        try:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM settings
                    WHERE scope = 'project' AND project_id = 'configured'
                    """
                ).fetchone()
            )
        finally:
            connection.close()
        self.assertEqual(tick(self.store)[0]["action"], "set_project_setting")
        submit_message(
            self.store,
            "configured",
            "写入 configured.txt: configured",
            "configured-task",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            frozen = json.loads(
                connection.execute(
                    """
                    SELECT settings_json FROM task_sessions
                    WHERE role_key = 'deterministic'
                    """
                ).fetchone()["settings_json"]
            )
        finally:
            connection.close()
        self.assertEqual(frozen["values"]["max_runtime_seconds"], 45)
        self.assertTrue(
            frozen["sources"]["max_runtime_seconds"].startswith(
                "project:configured:owner_message:"
            )
        )

    def test_project_provider_order_and_rules_freeze_into_task_session(self):
        self._create_project("policy", "policy-create", "/workspace/policy")
        with self.assertRaisesRegex(ValueError, "deterministic roles"):
            set_project_setting(
                self.store,
                "policy",
                "provider_order",
                {"deterministic": ["codex_cli"]},
                "policy-invalid-order",
            )
        set_project_setting(
            self.store,
            "policy",
            "provider_order",
            {"fullstack": ["codex_cli", "cursor_cli"]},
            "policy-order",
        )
        self.assertEqual(tick(self.store)[0]["action"], "set_project_setting")
        with self.assertRaisesRegex(ValueError, "invalid Provider or model"):
            set_project_setting(
                self.store,
                "policy",
                "provider_models",
                {"deepseek": "bad model"},
                "policy-invalid-model",
            )
        set_project_setting(
            self.store,
            "policy",
            "provider_models",
            {"codex_cli": "gpt-test-model"},
            "policy-model",
        )
        self.assertEqual(tick(self.store)[0]["action"], "set_project_setting")
        set_project_rule(
            self.store,
            "policy",
            "coding_convention",
            "# Project convention\n\nUse the smallest relevant test.\n",
            "policy-rule",
        )
        self.assertEqual(tick(self.store)[0]["action"], "set_project_rule")
        submit_message(
            self.store,
            "policy",
            "实现一个项目内刷新按钮",
            "policy-task",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            session = connection.execute(
                """
                SELECT settings_json, role_snapshot_json FROM task_sessions
                WHERE task_id = (
                    SELECT id FROM tasks WHERE project_id = 'policy'
                ) AND role_key = 'fullstack'
                """
            ).fetchone()
            setting = connection.execute(
                """
                SELECT value_json FROM settings
                WHERE scope = 'project' AND project_id = 'policy'
                  AND setting_key = 'provider_order'
                """
            ).fetchone()
        finally:
            connection.close()
        frozen = json.loads(session["settings_json"])
        self.assertEqual(
            frozen["values"]["provider_order"]["fullstack"],
            ["codex_cli", "cursor_cli"],
        )
        self.assertEqual(
            frozen["values"]["provider_models"]["codex_cli"],
            "gpt-test-model",
        )
        self.assertTrue(
            frozen["sources"]["provider_models"].startswith(
                "project:policy:owner_message:"
            )
        )
        self.assertIn("planner", json.loads(setting["value_json"]))
        role_snapshot = json.loads(session["role_snapshot_json"])
        self.assertEqual(
            role_snapshot["permission"], "recoverable_workspace_change"
        )
        self.assertIn(
            "coding_convention",
            {item["item_key"] for item in role_snapshot["library"]},
        )

    def test_script_promotion_requires_checked_cli_contract_and_records_lineage(self):
        source = (
            "def transform(value):\n"
            "    return value.upper()\n\n"
            "def main():\n"
            "    import sys\n"
            "    if len(sys.argv) != 2:\n"
            "        print('usage: tool.py VALUE', file=sys.stderr)\n"
            "        return 2\n"
            "    print(transform(sys.argv[1]))\n"
            "    return 0\n\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        )
        acceptance = [
            {"id": "script-callable", "expected": "transform is callable"},
            {"id": "script-cli", "expected": "main CLI exits deterministically"},
            {"id": "script-io", "expected": "stdout and stderr follow contract"},
        ]
        script_contract = {
            "language": "python",
            "callable": "transform",
            "cli_entry": "main",
            "exit_codes": [0, 2],
            "stdout": "transformed value on success",
            "stderr": "usage on invalid arguments",
            "acceptance_ids": {
                "callable": "script-callable",
                "cli": "script-cli",
                "io": "script-io",
            },
        }
        plan = complete_plan_contract(
            {
                "summary": "produce one reusable script",
                "alternatives": [
                    {
                        "name": "single-file",
                        "scope": "one Python file",
                        "cost": "bounded",
                        "risk": "bounded",
                        "reversible": True,
                        "acceptance": "script contract passes",
                    }
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "script",
                        "instruction": f"写入 tool.py: {source}",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "deterministic",
                        "acceptance": acceptance,
                    }
                ],
            },
            size="simple",
        )
        plan["tasks"][0]["result"]["script_contract"] = script_contract
        normalized = normalize_plan(plan, size="simple")
        self.assertEqual(
            normalized["tasks"][0]["result"]["script_contract"],
            script_contract,
        )
        incomplete = json.loads(json.dumps(plan))
        incomplete["tasks"][0]["result"]["script_contract"].pop("stderr")
        with self.assertRaisesRegex(ValueError, "incomplete script contract"):
            normalize_plan(incomplete, size="simple")

        submit_message(
            self.store,
            "script-project",
            f"写入 tool.py: {source}",
            "script-task",
        )
        run_until_idle(self.store)
        connection = self.store.connect()
        try:
            task = connection.execute(
                "SELECT * FROM tasks WHERE project_id = 'script-project'"
            ).fetchone()
            artifact = connection.execute(
                """
                SELECT * FROM artifacts
                WHERE task_id = ? AND kind = 'output'
                  AND acceptance_id = 'artifact_content_sha256'
                  AND revision = ?
                ORDER BY rowid LIMIT 1
                """,
                (task["id"], task["spec_revision"]),
            ).fetchone()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "single-file script Artifact"):
            submit_action(
                self.store,
                "script-project",
                task["id"],
                "promote_script",
                "",
                "script-without-contract",
                promotion={"item_key": "tool", "artifact_id": artifact["id"]},
            )

        spec = json.loads(task["spec_json"])
        result = spec["task_contract"]["result"]
        result["acceptance_ids"] = [item["id"] for item in acceptance]
        result["script_contract"] = script_contract
        with self.store.transaction() as connection:
            with lifecycle_write_scope("advance_project"):
                connection.execute(
                    """
                    UPDATE tasks SET spec_json = ?, acceptance_json = ?
                    WHERE id = ?
                    """,
                    (canonical_json(spec), canonical_json(acceptance), task["id"]),
                )

        blocked_message = submit_action(
            self.store,
            "script-project",
            task["id"],
            "promote_script",
            "",
            "script-without-complete-evidence",
            promotion={"item_key": "tool", "artifact_id": artifact["id"]},
        )
        with self.assertRaisesRegex(ValueError, "complete current-revision Evidence"):
            tick(self.store)
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE messages SET processed_at = ? WHERE id = ?",
                (time.time(), blocked_message),
            )

        evidence = {
            "verdict": "PASS",
            "acceptances": [
                {"acceptance_id": item["id"], "passed": True}
                for item in acceptance
            ],
        }
        evidence_path = (
            self.data
            / "projects"
            / "script-project"
            / "tasks"
            / task["id"]
            / "artifacts"
            / f"revision-{task['spec_revision']:06d}"
            / "script-checker-evidence.json"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(canonical_json(evidence), encoding="utf-8")
        with self.store.transaction() as connection:
            register_artifact(
                self.store,
                connection,
                project_id="script-project",
                task_id=task["id"],
                kind="evidence",
                path=evidence_path,
                stored_path=self.store.relative_data_path(evidence_path),
                acceptance_id="script-contract",
                revision=task["spec_revision"],
                scope=task_data_scope(["task-result"]),
                source_task_id=task["id"],
                created_at=time.time(),
            )
        submit_action(
            self.store,
            "script-project",
            task["id"],
            "promote_script",
            "",
            "script-promote",
            promotion={"item_key": "tool", "artifact_id": artifact["id"]},
        )
        self.assertEqual(tick(self.store)[0]["action"], "promote_script")
        connection = self.store.connect()
        try:
            item = connection.execute(
                """
                SELECT * FROM library_items
                WHERE project_id = 'script-project'
                  AND kind = 'script' AND item_key = 'tool'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(item["source_task_id"], task["id"])
        self.assertEqual(item["source_artifact_id"], artifact["id"])
        self.assertEqual(item["source_revision"], task["spec_revision"])
        promoted = self.store.resolve_data_path(item["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(promoted).hexdigest(), item["sha256"])
        self.assertEqual(
            promoted,
            self.store.resolve_data_path(artifact["path"]).read_bytes(),
        )
        with self.assertRaisesRegex(ValueError, "SystemExit"):
            _validate_promotable_python(
                b"def transform(value): return value\ndef main(): return 0\n",
                result["script_contract"],
            )

    def test_butler_exact_search_stays_zero_model_then_queues_checked_summary(self):
        submit_message(
            self.store,
            "semantic-project",
            "写入 known.txt: canonical source",
            "semantic-source",
        )
        run_until_idle(self.store)
        connection = self.store.connect()
        try:
            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id = 'semantic-project'"
            ).fetchone()[0]
        finally:
            connection.close()
        exact = semantic_search(
            self.store,
            "known.txt",
            "semantic-project",
            "semantic-exact",
        )
        self.assertFalse(exact["model_queued"])
        self.assertTrue(exact["routed_only"])
        self.assertTrue(exact["results"])
        self.assertEqual(tick(self.store)[0]["action"], "global_route")
        connection = self.store.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id = 'semantic-project'"
                ).fetchone()[0],
                task_count,
            )
        finally:
            connection.close()

        queued = semantic_search(
            self.store,
            "把已有目标和交付按含义归纳",
            "semantic-project",
            "semantic-fuzzy",
        )
        self.assertTrue(queued["model_queued"])
        self.assertGreater(queued["source_count"], 0)
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        planner_result = tick(self.store)[0]
        self.assertEqual(
            planner_result["action"],
            "planner_check",
            snapshot(self.db, self.data, "semantic-project")["task"],
        )
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            query_task = connection.execute(
                """
                SELECT * FROM tasks
                WHERE project_id = 'semantic-project' AND outcome IS NULL
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        spec = json.loads(query_task["spec_json"])
        contract = spec["task_contract"]
        self.assertFalse(spec["workspace_change_required"])
        self.assertEqual(
            spec["butler_semantic_query"]["query"],
            "把已有目标和交付按含义归纳",
        )
        self.assertEqual(contract["result"]["type"], "evidence")
        self.assertEqual(
            contract["checker"]["acceptance_ids"],
            ["semantic_summary_sources", "semantic_summary_scope"],
        )
        self.assertEqual(
            contract["authorization_boundary"],
            {
                "level": "none",
                "allowed_actions": ["read_workspace"],
                "target_scope": "project_workspace",
            },
        )

    def test_schema_v10_preserves_history_and_adds_library_lineage(self):
        submit_message(self.store, "migration", "写入 migrated.txt: ok", "migration")
        run_until_idle(self.store)
        connection = self.store.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE host_jobs RENAME TO host_jobs_v4")
            connection.execute(
                """
                CREATE TABLE host_jobs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    task_session_id TEXT,
                    session_generation INTEGER,
                    spec_revision INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    purpose TEXT NOT NULL CHECK (
                        purpose IN ('execute', 'check', 'repair', 'command')
                    ),
                    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL,
                    returncode INTEGER NOT NULL,
                    output_ref TEXT,
                    failure_code TEXT,
                    UNIQUE (task_id, sequence)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO host_jobs
                SELECT id, task_id, task_session_id, session_generation,
                       spec_revision, sequence, purpose, status,
                       started_at, ended_at, returncode, output_ref, failure_code
                FROM host_jobs_v4
                """
            )
            connection.execute("DROP TABLE host_jobs_v4")
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        finally:
            connection.close()

        self.store.initialize()
        connection = self.store.connect()
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            artifact_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(artifacts)")
            }
            self.assertIn("scope_json", artifact_columns)
            self.assertIn("source_task_id", artifact_columns)
            model_call_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(model_calls)")
            }
            self.assertIn("physical_session_id", model_call_columns)
            library_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(library_items)")
            }
            self.assertIn("source_task_id", library_columns)
            self.assertIn("source_artifact_id", library_columns)
            self.assertIn("source_revision", library_columns)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM model_calls
                    WHERE physical_session_id IS NULL
                    """
                ).fetchone()[0],
                0,
            )
            project_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(projects)")
            }
            self.assertIn("display_name", project_columns)
            self.assertEqual(
                connection.execute(
                    "SELECT display_name FROM projects WHERE id = 'migration'"
                ).fetchone()["display_name"],
                "migration",
            )
            message_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)")
            }
            self.assertIn("entry_kind", message_columns)
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            self.assertTrue({"deadline_at", "next_action_kind"} <= task_columns)
            self.assertIn(
                "terminal_capabilities_revoked_at",
                task_columns,
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM host_jobs").fetchone()[0], 4)
            previous = connection.execute(
                "SELECT * FROM host_jobs ORDER BY sequence LIMIT 1"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO host_jobs(
                    id, task_id, task_session_id, session_generation,
                    spec_revision, sequence, purpose, status, started_at
                ) VALUES ('00000000-0000-0000-0000-000000000004', ?, ?, ?, ?, 5,
                          'command', 'running', ?)
                """,
                (
                    previous["task_id"],
                    previous["task_session_id"],
                    previous["session_generation"],
                    previous["spec_revision"],
                    time.time(),
                ),
            )
            connection.commit()
            active = connection.execute(
                "SELECT ended_at, returncode FROM host_jobs WHERE status = 'running'"
            ).fetchone()
            self.assertIsNone(active["ended_at"])
            self.assertIsNone(active["returncode"])
        finally:
            connection.close()

    def test_backup_candidate_isolation_and_single_scheduler_gate(self):
        submit_message(
            self.store, "backup-source", "写入 backup.txt: ok", "backup-source"
        )
        run_until_idle(self.store)
        backup_path = self.root / "candidate" / "plowwhip.db"
        result = self.store.backup_to(backup_path)
        self.assertEqual(result["method"], "sqlite_backup_api")
        self.assertEqual(result["quick_check"], ["ok"])
        backup = sqlite3.connect(backup_path)
        try:
            self.assertEqual(
                backup.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1
            )
        finally:
            backup.close()
        production = {
            "code_root": "/srv/plowwhip-blue",
            "data_root": "/srv/plowwhip-data-blue",
            "db_path": "/srv/plowwhip-data-blue/plowwhip.db",
            "compose_project": "plowwhip_blue",
            "port": 8742,
            "host_bridge_namespace": "production",
            "cronner_enabled": True,
        }
        candidate = {
            "code_root": "/srv/plowwhip-green",
            "data_root": "/srv/plowwhip-data-green",
            "db_path": "/srv/plowwhip-data-green/plowwhip.db",
            "compose_project": "plowwhip_green",
            "port": 8750,
            "host_bridge_namespace": "candidate",
            "cronner_enabled": False,
        }
        gate = candidate_preflight(production, candidate)
        self.assertTrue(gate["isolated"])
        self.assertFalse(gate["cutover_approved"])
        with self.assertRaisesRegex(ValueError, "collision"):
            candidate_preflight(
                production, {**candidate, "compose_project": "plowwhip_blue"}
            )
        scheduler = acquire_scheduler_lock(self.data)
        try:
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                acquire_scheduler_lock(self.data)
        finally:
            scheduler.close()
        replacement = acquire_scheduler_lock(self.data)
        replacement.close()
        rollback_manifest = {
            **candidate,
            "code_root": str(self.root / "candidate-code"),
            "data_root": str(backup_path.parent),
            "db_path": str(backup_path),
        }
        rollback_gate = rollback_preflight(rollback_manifest)
        self.assertTrue(rollback_gate["rollback_ready"])
        self.assertTrue(rollback_gate["scheduler_lock_released"])
        candidate_scheduler = acquire_scheduler_lock(backup_path.parent)
        try:
            with self.assertRaisesRegex(ValueError, "scheduler lock"):
                rollback_preflight(rollback_manifest)
        finally:
            candidate_scheduler.close()
        backup = sqlite3.connect(backup_path)
        try:
            backup.execute(
                """
                UPDATE projects SET lease_token = 'still-active', lease_until = ?
                WHERE id = 'backup-source'
                """,
                (time.time() + 60,),
            )
            backup.commit()
        finally:
            backup.close()
        with self.assertRaisesRegex(ValueError, "active project lease"):
            rollback_preflight(rollback_manifest)

    def test_owner_wake_is_queued_but_cronner_remains_the_only_driver(self):
        submit_message(self.store, "wake", "写入 wake.txt: done", "wake-message")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        waiting = snapshot(self.db, self.data, "wake")
        submit_action(
            self.store,
            "wake",
            waiting["task"]["id"],
            "wake",
            "",
            "wake-action",
        )
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["wake", "planner_check", "plan_applied", "execute", "verify"],
        )
        done = snapshot(self.db, self.data, "wake")
        self.assertEqual(done["task"]["public_status"], "done")
        self.assertIn("wake_requested", {item["kind"] for item in done["events"]})

    def test_checker_changes_required_is_a_bounded_repair_package(self):
        expected = [
            {
                "id": "owner_instruction",
                "kind": "checker_evidence",
                "expected": "refresh button is usable",
            },
            {
                "id": "relevant_checks",
                "kind": "checker_evidence",
                "expected": "tests pass",
            },
        ]
        verdict = _parse_checker_verdict(
            checker_output("CHANGES_REQUIRED", ("owner_instruction",)),
            expected,
            "/workspace",
        )
        self.assertTrue(verdict["valid"])
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["verdict"], "CHANGES_REQUIRED")
        self.assertEqual(
            verdict["repair_package"],
            [
                {
                    "acceptance_id": "owner_instruction",
                    "passed": False,
                    "actual_evidence": "owner_instruction missing",
                    "expected_result": "refresh button is usable",
                    "allowed_scope": "/workspace",
                    "recheck_command": "python3 -m unittest -q",
                }
            ],
        )

    def test_failed_evidence_converges_to_needs_decision(self):
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, created_at)
                VALUES ('project-b', 'project-b', ?)
                """,
                (time.time(),),
            )
            connection.execute(
                """
                INSERT INTO settings(
                    id, scope, project_id, setting_key, value_json, source, updated_at
                ) VALUES ('project-b:retry_count', 'project', 'project-b',
                          'retry_count', '0', 'test_policy', ?)
                """,
                (time.time(),),
            )
        submit_message(self.store, "project-b", "write result.txt: expected", "request-2")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            frozen = json.loads(
                connection.execute(
                    """
                    SELECT settings_json FROM task_sessions
                    WHERE role_key = 'deterministic'
                    """
                ).fetchone()["settings_json"]
            )
        finally:
            connection.close()
        self.assertEqual(frozen["values"]["retry_count"], 0)
        self.assertEqual(
            frozen["sources"]["retry_count"], "project:project-b:test_policy"
        )
        self.assertEqual(tick(self.store)[0]["action"], "execute")

        view = snapshot(self.db, self.data, "project-b")
        execution_artifact = next(
            item
            for item in view["artifacts"]
            if item["kind"] == "artifact"
            and item["acceptance_id"] == "artifact_content_sha256"
        )
        Path(execution_artifact["path"]).write_text("tampered")
        result = tick(self.store)[0]
        self.assertEqual(result["status"], "needs_decision")

        view = snapshot(self.db, self.data, "project-b")
        self.assertEqual(view["task"]["public_status"], "needs_decision")
        self.assertEqual(view["task"]["fault_code"], "verification")
        evidence_row = next(
            item
            for item in view["artifacts"]
            if item["kind"] == "evidence"
            and item["acceptance_id"] == "artifact_content_sha256"
        )
        evidence = json.loads(Path(evidence_row["path"]).read_text())
        self.assertFalse(evidence["passed"])
        self.assertEqual(tick(self.store), [])

        submit_action(
            self.store,
            "project-b",
            view["task"]["id"],
            "provide_decision",
            "write result.txt: revised",
            "request-2-decision",
        )
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["provide_decision", "execute", "verify"],
        )
        revised = snapshot(self.db, self.data, "project-b")
        self.assertEqual(revised["task"]["public_status"], "done")
        self.assertEqual(revised["task"]["spec_revision"], 3)
        self.assertEqual(
            {item["revision"] for item in revised["artifacts"]},
            {1, 2, 3},
        )
        self.assertEqual(len({item["path"] for item in revised["artifacts"]}), 6)

    def test_tampered_output_is_repaired_before_owner_is_disturbed(self):
        submit_message(self.store, "repair", "write result.txt: expected", "repair-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        self.assertEqual(tick(self.store)[0]["action"], "execute")
        view = snapshot(self.db, self.data, "repair")
        execution_artifact = next(
            item
            for item in view["artifacts"]
            if item["kind"] == "artifact"
            and item["acceptance_id"] == "artifact_content_sha256"
        )
        Path(execution_artifact["path"]).write_text("tampered")
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["verify", "repair", "verify"],
        )
        done = snapshot(self.db, self.data, "repair")
        self.assertEqual(done["task"]["outcome"], "done")
        self.assertEqual(done["task"]["retry_count"], 1)
        self.assertEqual(
            [event["kind"] for event in reversed(done["events"])],
            [
                "task_created",
                "planner_prepared",
                "planner_checker_prepared",
                "plan_applied",
                "executed",
                "verified",
                "repaired",
                "temporary_capabilities_revoked",
                "verified",
            ],
        )

    def test_unrecognized_instruction_is_planned_before_scope_decision(self):
        submit_message(self.store, "project-c", "please decide for me", "request-3")
        actions = run_until_idle(self.store)
        self.assertEqual(
            [(item["action"], item["status"]) for item in actions],
            [("intake", "pending"), ("needs_decision", "needs_decision")],
        )
        view = snapshot(self.db, self.data, "project-c")
        self.assertEqual(view["task"]["phase"], "plan")
        self.assertEqual(view["task"]["fault_code"], "verification")
        self.assertEqual(view["artifacts"], [])
        connection = self.store.connect()
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_sessions").fetchone()[0], 2
            )
            self.assertEqual(
                {
                    row["role_key"]
                    for row in connection.execute(
                        "SELECT role_key FROM task_sessions"
                    )
                },
                {"planner", "independent_checker"},
            )
        finally:
            connection.close()

    def test_restart_recovers_queue_in_strict_project_order(self):
        submit_message(self.store, "restart", "write first.txt: first", "restart-1")
        submit_message(self.store, "restart", "write second.txt: second", "restart-2")
        self.assertEqual(tick(self.store)[0]["action"], "intake")

        restarted = Store(self.db, self.data)
        self.assertEqual(
            [item["action"] for item in run_until_idle(restarted)],
            [
                "planner_check",
                "plan_applied",
                "execute",
                "verify",
                "intake",
                "planner_check",
                "plan_applied",
                "execute",
                "verify",
            ],
        )
        connection = restarted.connect()
        try:
            rows = connection.execute(
                "SELECT public_status, outcome FROM tasks ORDER BY created_at, rowid"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([tuple(row) for row in rows], [("done", "done"), ("done", "done")])

    def test_cancel_rerun_and_complete_schema(self):
        submit_message(self.store, "cancel", "write result.txt: first", "cancel-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        self.assertEqual(tick(self.store)[0]["action"], "execute")
        task = snapshot(self.db, self.data, "cancel")["task"]

        submit_action(self.store, "cancel", task["id"], "cancel", "", "cancel-2")
        result = tick(self.store)[0]
        self.assertEqual((result["action"], result["status"]), ("cancel", "cancelled"))
        cancelled = snapshot(self.db, self.data, "cancel")
        self.assertEqual(cancelled["task"]["outcome"], "cancelled")
        self.assertEqual(tick(self.store), [])
        connection = self.store.connect()
        try:
            self.assertEqual(
                {tuple(row) for row in connection.execute("SELECT generation, status FROM session_generations")},
                {(1, "archived")},
            )
        finally:
            connection.close()

        submit_action(self.store, "cancel", task["id"], "rerun", "", "cancel-3")
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            ["rerun", "execute", "verify"],
        )
        rerun = snapshot(self.db, self.data, "cancel")
        self.assertEqual(rerun["task"]["id"], task["id"])
        self.assertEqual(rerun["task"]["outcome"], "done")
        self.assertEqual(len({item["path"] for item in rerun["artifacts"]}), 5)

        connection = self.store.connect()
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            generations = connection.execute(
                "SELECT generation, status FROM session_generations ORDER BY task_session_id, generation"
            ).fetchall()
            jobs = connection.execute(
                "SELECT sequence, session_generation, purpose FROM host_jobs ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            tables,
            {
                "projects",
                "messages",
                "goals",
                "plans",
                "tasks",
                "task_dependencies",
                "workers",
                "task_sessions",
                "session_generations",
                "host_jobs",
                "artifacts",
                "task_events",
                "model_calls",
                "library_items",
                "settings",
            },
        )
        self.assertEqual(
            [(row["generation"], row["status"]) for row in generations],
            [
                (1, "archived"),
                (2, "archived"),
                (1, "archived"),
                (2, "archived"),
                (1, "archived"),
                (2, "archived"),
                (1, "archived"),
                (2, "archived"),
            ],
        )
        self.assertEqual(
            [tuple(row) for row in jobs],
            [
                (1, 1, "command"),
                (2, 1, "check"),
                (3, 1, "execute"),
                (4, 2, "execute"),
                (5, 2, "check"),
            ],
        )

    def test_terminal_revocation_is_explicit_and_rerun_drops_old_refs(self):
        submit_message(
            self.store,
            "terminal-revoke",
            "写入 revoked.txt: safe",
            "terminal-revoke-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        task = snapshot(self.db, self.data, "terminal-revoke")["task"]
        with self.store.transaction() as connection:
            spec = json.loads(
                connection.execute(
                    "SELECT spec_json FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()["spec_json"]
            )
            spec["authorization"] = {
                "action_kind": "temporary-test",
                "expires_at": time.time() + 900,
            }
            spec["secret_refs"] = ["vault://test/opaque-reference"]
            connection.execute(
                "UPDATE tasks SET spec_json = ? WHERE id = ?",
                (json.dumps(spec, sort_keys=True), task["id"]),
            )
        submit_action(
            self.store,
            "terminal-revoke",
            task["id"],
            "cancel",
            "",
            "terminal-revoke-cancel",
        )
        self.assertEqual(tick(self.store)[0]["action"], "cancel")
        cancelled = snapshot(self.db, self.data, "terminal-revoke")
        self.assertEqual(cancelled["task"]["outcome"], "cancelled")
        self.assertIsNotNone(
            cancelled["task"]["terminal_capabilities_revoked_at"]
        )
        revocation_event = next(
            item
            for item in cancelled["events"]
            if item["kind"] == "temporary_capabilities_revoked"
        )
        revocation = json.loads(revocation_event["detail_json"])
        self.assertRegex(
            revocation["authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(len(revocation["secret_reference_hashes"]), 1)
        self.assertNotIn("vault://", revocation_event["detail_json"])

        submit_action(
            self.store,
            "terminal-revoke",
            task["id"],
            "rerun",
            "",
            "terminal-revoke-rerun",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")
        rerun = snapshot(self.db, self.data, "terminal-revoke")
        rerun_spec = json.loads(rerun["task"]["spec_json"])
        self.assertNotIn("authorization", rerun_spec)
        self.assertNotIn("secret_refs", rerun_spec)
        self.assertIsNone(
            rerun["task"]["terminal_capabilities_revoked_at"]
        )

    def test_cancelled_planner_rerun_returns_to_plan_without_executor_bypass(self):
        self._use_real_planner_adapter()
        self._create_project(
            "planner-rerun",
            "planner-rerun-create",
            "/workspace/planner-rerun",
        )
        submit_message(
            self.store,
            "planner-rerun",
            "1、使用 SSH 上传到 "
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue；"
            "2、指定 Cursor 审查稳健性、安全与 Token；"
            "3、指定 Codex 根据审查结果修复并验证",
            "planner-rerun-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        task = snapshot(self.db, self.data, "planner-rerun")["task"]
        self.assertEqual((task["role_key"], task["phase"]), ("planner", "plan"))

        submit_action(
            self.store,
            "planner-rerun",
            task["id"],
            "cancel",
            "",
            "planner-rerun-cancel",
        )
        self.assertEqual(tick(self.store)[0]["action"], "cancel")
        submit_action(
            self.store,
            "planner-rerun",
            task["id"],
            "rerun",
            "",
            "planner-rerun-retry",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")

        rerun = snapshot(self.db, self.data, "planner-rerun")
        self.assertEqual(rerun["task"]["phase"], "plan")
        self.assertEqual(rerun["task"]["next_action_kind"], "plan")
        self.assertEqual(rerun["task"]["role_key"], "planner")
        self.assertNotIn(
            "fullstack",
            {item["role_key"] for item in rerun["sessions"]},
        )
        self.assertEqual(
            {
                item["role_key"]
                for item in rerun["sessions"]
                if item["status"] == "active"
            },
            {"planner", "independent_checker"},
        )
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "running",
                    "session_id": "planner-rerun-session",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={"chunks": []},
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "plan_wait")

    def test_versioned_plan_runs_serial_dag(self):
        submit_message(self.store, "plan", "build two files", "plan-1")
        run_until_idle(self.store)
        placeholder = snapshot(self.db, self.data, "plan")["task"]
        plan = {
            "summary": "two deterministic steps",
            "alternatives": [
                {
                    "name": "serial",
                    "scope": "two files",
                    "cost": "low",
                    "risk": "low",
                    "reversible": True,
                    "acceptance": "two hashes",
                },
                {
                    "name": "manual",
                    "scope": "two files",
                    "cost": "high",
                    "risk": "low",
                    "reversible": True,
                    "acceptance": "manual review",
                },
            ],
            "selected": 0,
            "tasks": [
                {"key": "first", "instruction": "write first.txt: first"},
                {
                    "key": "second",
                    "instruction": "write second.txt: second",
                    "depends_on": ["first"],
                    "sprint": 2,
                    "settings": {
                        "deterministic": {"max_runtime_seconds": 30},
                        "deterministic_checker": {"monitor_tail_lines": 10},
                    },
                },
            ],
        }
        plan = complete_plan_contract(plan, size="large")
        submit_action(
            self.store,
            "plan",
            placeholder["id"],
            "provide_plan",
            "",
            "plan-2",
            plan,
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_plan")
        submit_message(
            self.store,
            "plan",
            "写入 urgent.txt: urgent",
            "plan-urgent",
        )
        self.assertEqual(tick(self.store)[0]["action"], "execute")
        self.assertEqual(tick(self.store)[0]["action"], "verify")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(
            [item["action"] for item in run_until_idle(self.store)],
            [
                "planner_check",
                "plan_applied",
                "execute",
                "verify",
                "ready",
                "execute",
                "verify",
            ],
        )
        connection = self.store.connect()
        try:
            tasks = connection.execute(
                "SELECT id, outcome, sprint FROM tasks WHERE project_id = 'plan' ORDER BY rowid"
            ).fetchall()
            selected = connection.execute(
                "SELECT revision FROM plans WHERE selected = 1 AND goal_id = ?",
                (placeholder["goal_id"],),
            ).fetchone()["revision"]
            dependencies = connection.execute(
                "SELECT COUNT(*) AS count FROM task_dependencies"
            ).fetchone()["count"]
            session_count = connection.execute(
                "SELECT COUNT(*) AS count FROM task_sessions"
            ).fetchone()["count"]
            second_settings = [
                json.loads(row["settings_json"])
                for row in connection.execute(
                    """
                    SELECT settings_json FROM task_sessions
                    WHERE task_id = ? ORDER BY role_key
                    """,
                    (tasks[1]["id"],),
                )
            ]
        finally:
            connection.close()
        self.assertEqual(tasks[0]["id"], placeholder["id"])
        self.assertEqual(
            [(row["outcome"], row["sprint"]) for row in tasks],
            [("done", 1), ("done", 2), ("done", 1)],
        )
        self.assertEqual((selected, dependencies), (2, 1))
        self.assertEqual(session_count, 10)
        self.assertEqual(second_settings[0]["values"]["max_runtime_seconds"], 30)
        self.assertEqual(second_settings[0]["sources"]["max_runtime_seconds"], "task_role")
        self.assertEqual(second_settings[1]["values"]["monitor_tail_lines"], 10)

        submit_message(self.store, "blocked", "build two files", "blocked-1")
        blocked_actions = run_until_idle(self.store)
        self.assertEqual(blocked_actions[-1]["status"], "needs_decision")
        blocked_task = snapshot(self.db, self.data, "blocked")["task"]
        submit_action(
            self.store,
            "blocked",
            blocked_task["id"],
            "provide_plan",
            "",
            "blocked-2",
            plan,
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_plan")
        self.assertEqual(snapshot(self.db, self.data, "blocked")["task"]["id"], blocked_task["id"])
        submit_action(
            self.store, "blocked", blocked_task["id"], "cancel", "", "blocked-3"
        )
        self.assertEqual(tick(self.store)[0]["action"], "cancel")
        self.assertEqual(tick(self.store)[0]["action"], "dependency_blocked")
        blocked = snapshot(self.db, self.data, "blocked")["task"]
        self.assertNotEqual(blocked["id"], blocked_task["id"])
        self.assertEqual(blocked["public_status"], "needs_decision")

        cyclic = {**plan, "tasks": [
            {"key": "a", "instruction": "write a.txt: a", "depends_on": ["b"]},
            {"key": "b", "instruction": "write b.txt: b", "depends_on": ["a"]},
        ]}
        cyclic = complete_plan_contract(cyclic, size="large")
        with self.assertRaisesRegex(ValueError, "cycle"):
            normalize_plan(cyclic)
        external = {**plan, "tasks": [
            {"key": "a", "instruction": "write a.txt: a", "settings": {"deterministic": {"provider_order": ["codex_cli"]}}},
            {"key": "b", "instruction": "write b.txt: b", "depends_on": ["a"]},
        ]}
        external = complete_plan_contract(external, size="large")
        with self.assertRaisesRegex(ValueError, "requires local"):
            normalize_plan(external)

    def test_large_instruction_uses_planner_and_auto_selects_confident_plan(self):
        self._use_real_planner_adapter()
        self._create_project("auto-plan", "project-1", "/workspace/auto-plan")
        submit_message(
            self.store,
            "auto-plan",
            "前端和后端增加刷新能力",
            "message-1",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        planned = {
            "classification": {
                "size": "large",
                "reasons": ["two semantic scopes require an ordered Task DAG"],
                "information_sufficient": True,
                "requires_owner_choice": False,
            },
            "confidence": 0.97,
            "plan": {
                "summary": "two bounded code tasks",
                "alternatives": [
                    {
                        "name": "serial",
                        "scope": "backend then frontend",
                        "cost": "low",
                        "risk": "low",
                        "reversible": True,
                        "acceptance": "refresh flow passes checks",
                    },
                    {
                        "name": "single change",
                        "scope": "one broad worker task",
                        "cost": "medium",
                        "risk": "medium",
                        "reversible": True,
                        "acceptance": "manual integrated review",
                    },
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "backend",
                        "instruction": "实现后端刷新接口",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {
                                "id": "backend_refresh",
                                "expected_result": "refresh endpoint passes its bounded check",
                            }
                        ],
                    },
                    {
                        "key": "frontend",
                        "instruction": "实现前端刷新按钮",
                        "depends_on": ["backend"],
                        "sprint": 1,
                        "role_key": "fullstack",
                    },
                ],
            },
        }
        planned = complete_planner_payload(planned)
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "planner-session",
                    "input_tokens": 120,
                    "cached_input_tokens": 80,
                    "output_tokens": 60,
                    "model": "test-planner",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                        }
                    ]
                },
            ),
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            tasks = connection.execute(
                """
                SELECT id, phase, role_key, checker_role_key, acceptance_json,
                       spec_json
                FROM tasks WHERE project_id = 'auto-plan' ORDER BY rowid
                """
            ).fetchall()
            plans = connection.execute(
                "SELECT revision, selected FROM plans ORDER BY revision"
            ).fetchall()
            roles = connection.execute(
                """
                SELECT role_key FROM task_sessions
                WHERE task_id = ? ORDER BY role_key
                """,
                (tasks[0]["id"],),
            ).fetchall()
            planner_generation = connection.execute(
                """
                SELECT generation.status FROM session_generations generation
                JOIN task_sessions session ON session.id = generation.task_session_id
                WHERE session.task_id = ? AND session.role_key = 'planner'
                """,
                (tasks[0]["id"],),
            ).fetchone()
            selected_plan = json.loads(
                connection.execute(
                    "SELECT summary_json FROM plans WHERE selected = 1"
                ).fetchone()["summary_json"]
            )
        finally:
            connection.close()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            [(row["phase"], row["role_key"], row["checker_role_key"]) for row in tasks],
            [
                ("execute", "fullstack", "independent_checker"),
                ("queued", "fullstack", "independent_checker"),
            ],
        )
        self.assertEqual([tuple(row) for row in plans], [(1, 0), (2, 1)])
        self.assertEqual(
            json.loads(tasks[0]["acceptance_json"])[0]["id"],
            "backend_refresh",
        )
        self.assertEqual(
            json.loads(tasks[0]["acceptance_json"])[0]["expected"],
            "refresh endpoint passes its bounded check",
        )
        self.assertEqual(
            [row["role_key"] for row in roles],
            ["fullstack", "independent_checker", "planner"],
        )
        self.assertEqual(planner_generation["status"], "archived")
        self.assertEqual(
            selected_plan["classification"]["upgrade_path"],
            ["simple", "medium", "large"],
        )
        task_contract = json.loads(tasks[0]["spec_json"])["task_contract"]
        self.assertEqual(task_contract["max_runtime_seconds"], 600)
        self.assertEqual(
            task_contract["checker"]["role_key"], "independent_checker"
        )
        facts = instruction_facts("前端和后端增加刷新能力", "provider_task")
        self.assertNotIn("size", facts)
        self.assertIn("前端和后端", facts["possible_multi_scope_terms"])
        negated_risk_facts = instruction_facts(
            "不要部署、不要发布，也不要接触生产环境",
            "provider_task",
        )
        self.assertNotIn("authorization_required", negated_risk_facts)
        self.assertEqual(negated_risk_facts["possible_high_risk_terms"], [])
        self.assertEqual(parse_planner_result(PLANNER_RESULT_PREFIX + json.dumps(planned))["confidence"], 0.97)
        codex_jsonl = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                },
            }
        )
        self.assertEqual(parse_planner_result(codex_jsonl)["confidence"], 0.97)
        descriptive_reversibility = json.loads(json.dumps(planned))
        descriptive_reversibility["plan"]["alternatives"][0][
            "reversible"
        ] = "高：可通过提交回退"
        with self.assertRaisesRegex(
            ValueError, "objective selection requires boolean"
        ):
            parse_planner_result(
                PLANNER_RESULT_PREFIX + json.dumps(descriptive_reversibility)
            )
        numeric_sprint = json.loads(json.dumps(planned["plan"]))
        numeric_sprint["tasks"][0]["sprint"] = "2"
        self.assertEqual(normalize_plan(numeric_sprint)["tasks"][0]["sprint"], 2)

    def test_large_plan_requires_objective_atomic_complete_task_contracts(self):
        planned = complete_planner_payload(
            {
                "classification": {
                    "size": "large",
                    "reasons": [
                        "backend and frontend are independent responsibility boundaries"
                    ],
                    "information_sufficient": True,
                    "requires_owner_choice": False,
                },
                "confidence": 0.99,
                "plan": {
                    "summary": "two atomic responsibilities",
                    "alternatives": [
                        {
                            "name": "serial",
                            "scope": "backend then frontend",
                            "cost": "lower",
                            "risk": "lower",
                            "reversible": True,
                            "acceptance": "both bounded results pass",
                        },
                        {
                            "name": "broad",
                            "scope": "one coordinated delivery",
                            "cost": "higher",
                            "risk": "higher",
                            "reversible": True,
                            "acceptance": "both bounded results pass",
                        },
                    ],
                    "selected": 0,
                    "tasks": [
                        {
                            "key": "backend",
                            "instruction": "实现后端刷新接口",
                            "role_key": "fullstack",
                        },
                        {
                            "key": "frontend",
                            "instruction": "实现前端刷新按钮",
                            "depends_on": ["backend"],
                            "role_key": "fullstack",
                        },
                    ],
                },
            }
        )
        normalized = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(planned)
        )
        first = normalized["plan"]["tasks"][0]
        self.assertEqual(
            set(first["spec"]["task_contract"]),
            {
                "responsibility",
                "inputs",
                "result",
                "acceptance",
                "checker",
                "depends_on",
                "max_runtime_seconds",
                "authorization_boundary",
            },
        )
        self.assertEqual(first["max_runtime_seconds"], 600)
        self.assertEqual(
            first["settings"]["fullstack"]["max_runtime_seconds"], 600
        )
        self.assertEqual(
            {
                coverage
                for task in normalized["plan"]["tasks"]
                for coverage in task["result"]["coverage"]
            },
            set(normalized["plan"]["required_coverage"]),
        )

        cases = []
        missing_result = json.loads(json.dumps(planned))
        missing_result["plan"]["tasks"][0].pop("result")
        cases.append((missing_result, "result contract"))

        missing_runtime = json.loads(json.dumps(planned))
        missing_runtime["plan"]["tasks"][0].pop("max_runtime_seconds")
        cases.append((missing_runtime, "requires max_runtime_seconds"))

        non_atomic = json.loads(json.dumps(planned))
        non_atomic["plan"]["tasks"][1]["responsibility"]["component"] = (
            non_atomic["plan"]["tasks"][0]["responsibility"]["component"]
        )
        cases.append((non_atomic, "unique responsibility components"))

        incomplete_coverage = json.loads(json.dumps(planned))
        incomplete_coverage["plan"]["tasks"][1]["result"]["coverage"] = [
            incomplete_coverage["plan"]["tasks"][0]["result"]["coverage"][0]
        ]
        cases.append((incomplete_coverage, "owned by multiple tasks"))

        self_reported_confidence = json.loads(json.dumps(planned))
        selected_metrics = self_reported_confidence["plan"]["alternatives"][0][
            "objective_metrics"
        ]
        selected_metrics["estimated_effort"] = 99
        selected_metrics["risk_level"] = 4
        cases.append(
            (
                self_reported_confidence,
                "lacks a dominating alternative",
            )
        )

        unjustified_upgrade = json.loads(json.dumps(planned))
        unjustified_upgrade["classification"]["upgrade_evidence"] = []
        cases.append((unjustified_upgrade, "justify every size upgrade"))

        unsupported_external = json.loads(json.dumps(planned))
        unsupported_external["plan"]["tasks"][0]["instruction"] = (
            "部署当前服务到生产并切流"
        )
        cases.append(
            (
                unsupported_external,
                "without a dedicated authorized runtime executor",
            )
        )

        for payload, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(
                ValueError, error
            ):
                parse_planner_result(
                    PLANNER_RESULT_PREFIX + json.dumps(payload)
                )

    def test_audit_then_repair_requires_checked_complete_report_dependency(self):
        planned = complete_planner_payload(
            {
                "classification": {
                    "size": "large",
                    "reasons": ["audit and repair are separate responsibilities"],
                    "information_sufficient": True,
                    "requires_owner_choice": False,
                },
                "confidence": 0.99,
                "plan": {
                    "summary": "audit before repair",
                    "alternatives": [
                        {
                            "name": "bounded",
                            "scope": "audit and repair only",
                            "cost": "lower",
                            "risk": "lower",
                            "reversible": True,
                            "acceptance": "checked report gates repair",
                        },
                        {
                            "name": "expanded",
                            "scope": "same coverage with more work",
                            "cost": "higher",
                            "risk": "higher",
                            "reversible": True,
                            "acceptance": "checked report gates repair",
                        },
                    ],
                    "selected": 0,
                    "tasks": [
                        {
                            "key": "audit",
                            "instruction": "审查当前代码并生成 audit-report.md",
                            "depends_on": [],
                            "sprint": 1,
                            "role_key": "fullstack",
                        },
                        {
                            "key": "repair",
                            "instruction": "依据完整审查报告制定并实施修复",
                            "depends_on": ["audit"],
                            "sprint": 2,
                            "role_key": "fullstack",
                        },
                    ],
                },
            }
        )
        planned["classification"]["workflow"] = "audit_then_repair"
        audit = planned["plan"]["tasks"][0]
        audit["result"] = {
            "type": "artifact",
            "coverage": audit["result"]["coverage"],
            "acceptance_ids": audit["result"]["acceptance_ids"],
            "artifact": {
                "path": "audit-report.md",
                "format": "markdown",
            },
        }
        audit["authorization_boundary"] = {
            "level": "recoverable",
            "allowed_actions": ["write_workspace", "run_checks"],
            "target_scope": "project_workspace",
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(planned)
        )
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["artifact"]["path"],
            "audit-report.md",
        )
        missing_dependency = json.loads(json.dumps(planned))
        missing_dependency["plan"]["tasks"][1]["depends_on"] = []
        missing_dependency["plan"]["tasks"][1]["inputs"] = [
            {"kind": "owner_instruction", "source": "goal"}
        ]
        with self.assertRaisesRegex(ValueError, "audit-report.md"):
            parse_planner_result(
                PLANNER_RESULT_PREFIX + json.dumps(missing_dependency)
            )
        wrong_path = json.loads(json.dumps(planned))
        wrong_path["plan"]["tasks"][0]["result"]["artifact"]["path"] = (
            "audit-tail.md"
        )
        with self.assertRaisesRegex(ValueError, "audit-report.md"):
            parse_planner_result(
                PLANNER_RESULT_PREFIX + json.dumps(wrong_path)
            )

    def test_composite_git_cursor_codex_goal_uses_planner_and_keeps_all_steps(self):
        self._use_real_planner_adapter()
        self._create_project(
            "composite-plan",
            "project-create",
            "/workspace/composite-plan",
        )
        instruction = (
            "1、你将本地代码上传到 GitHub，注意避免上传 .env 等秘密文件；"
            "使用 SSH 上传到 "
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue。"
            "2、指定使用 Cursor 对代码进行多维度审查，重点考虑稳健性、"
            "无人值守、省 Token、代码规范和安全。"
            "3、根据审查结果，指定使用 Codex 进行修复和优化，并完成验证。"
        )
        spec, _ = normalize_instruction(instruction)
        self.assertEqual(spec["kind"], "provider_task")
        codex_spec, _ = normalize_instruction("仅使用 Codex 修复当前代码")
        cursor_spec, _ = normalize_instruction("使用 Cursor 只读审查当前代码")
        self.assertEqual(codex_spec["provider_key"], "codex_cli")
        self.assertEqual(cursor_spec["provider_key"], "cursor_cli")
        self.assertFalse(cursor_spec["workspace_change_required"])
        planned_publish, _ = normalize_instruction(
            "使用 SSH 认证，将本地代码发布到 "
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue"
        )
        self.assertEqual(planned_publish["kind"], "git_publish")
        facts = instruction_facts(instruction, spec["kind"])
        self.assertNotIn("size", facts)
        self.assertEqual(facts["declared_step_count"], 3)
        source_message_id = submit_message(
            self.store,
            "composite-plan",
            instruction,
            "composite-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        publish = (
            "使用 SSH 上传到 "
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue，"
            "注意避免上传 .env 等秘密文件"
        )
        planned = {
            "classification": {
                "size": "large",
                "reasons": ["three named Provider boundaries form a serial DAG"],
                "information_sufficient": True,
                "requires_owner_choice": False,
            },
            "confidence": 0.98,
            "plan": {
                "summary": "publish, review, then repair",
                "alternatives": [
                    {
                        "name": "serial evidence chain",
                        "scope": "publish, Cursor review, Codex repair",
                        "cost": "bounded",
                        "risk": "low",
                        "reversible": True,
                        "acceptance": "three assigned Providers complete in order",
                    },
                    {
                        "name": "combined Provider task",
                        "scope": "one Provider attempts all three steps",
                        "cost": "lower",
                        "risk": "loses named Provider boundaries",
                        "reversible": True,
                        "acceptance": "not selected",
                    },
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "initial-publish",
                        "instruction": publish,
                        "depends_on": [],
                        "role_key": "git_publisher",
                    },
                    {
                        "key": "cursor-review",
                        "instruction": (
                            "使用 Cursor 对当前代码进行多维度只读审查并生成有界 "
                            "Evidence，不修改代码"
                        ),
                        "depends_on": ["initial-publish"],
                        "role_key": "fullstack",
                        "settings": {
                            "fullstack": {
                                "provider_order": ["cursor_cli"],
                            }
                        },
                    },
                    {
                        "key": "codex-repair",
                        "instruction": "根据审查 Evidence 修复和优化并运行相关验证",
                        "depends_on": ["cursor-review"],
                        "role_key": "fullstack",
                        "settings": {
                            "fullstack": {
                                "provider_order": ["codex_cli"],
                            }
                        },
                    },
                ],
            },
        }
        planned = complete_planner_payload(planned)
        nested_spec_plan = json.loads(json.dumps(planned["plan"]))
        for item in nested_spec_plan["tasks"]:
            item["spec"] = {
                "instruction": item.pop("instruction"),
                "kind": "write_text",
                "provider_key": "untrusted",
                "remote_ssh": "git@example.invalid:wrong/repository.git",
            }
        normalized_nested = normalize_plan(nested_spec_plan)
        self.assertEqual(
            [item["spec"]["kind"] for item in normalized_nested["tasks"]],
            ["git_publish", "provider_task", "provider_task"],
        )
        self.assertEqual(
            normalized_nested["tasks"][0]["spec"]["remote_ssh"],
            "git@github.com:niugengtian/PlowWhip_Webv2.git",
        )
        self.assertNotIn("provider_key", normalized_nested["tasks"][0]["spec"])
        overplanned = json.loads(json.dumps(planned["plan"]))
        overplanned["tasks"].append(
            {
                "key": "unrequested-final-publish",
                "instruction": publish,
                "depends_on": ["codex-repair"],
                "role_key": "git_publisher",
            }
        )
        overplanned = complete_plan_contract(overplanned, size="large")
        connection = self.store.connect()
        try:
            goal_id = connection.execute(
                "SELECT goal_id FROM tasks WHERE project_id = 'composite-plan'"
            ).fetchone()["goal_id"]
            with self.assertRaisesRegex(ValueError, "exactly three serial tasks"):
                _materialize_plan(
                    connection,
                    "composite-plan",
                    goal_id,
                    normalize_plan(overplanned),
                )
        finally:
            connection.close()
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "composite-planner-session",
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 50,
                    "model": "test-planner",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                        }
                    ]
                },
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "planner_check")
            self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        task_view = snapshot(self.db, self.data, "composite-plan")
        self.assertEqual(task_view["goals"][0]["objective"], instruction)
        self.assertTrue(
            all(item["objective"] != instruction for item in task_view["tasks"])
        )
        task_objectives = [item["objective"] for item in task_view["tasks"]]
        self.assertTrue(any("SSH" in item for item in task_objectives))
        self.assertTrue(any("Cursor" in item for item in task_objectives))
        self.assertTrue(any("修复" in item for item in task_objectives))
        connection = self.store.connect()
        try:
            tasks = connection.execute(
                """
                SELECT id, role_key, checker_role_key, spec_revision, spec_json,
                       acceptance_json
                FROM tasks WHERE project_id = 'composite-plan' ORDER BY rowid
                """
            ).fetchall()
            providers = {
                row["id"]: {
                    session["role_key"]: session["provider_key"]
                    for session in connection.execute(
                        """
                        SELECT session.role_key, generation.provider_key
                        FROM task_sessions session
                        JOIN session_generations generation
                          ON generation.task_session_id = session.id
                        WHERE session.task_id = ?
                        ORDER BY generation.generation
                        """,
                        (row["id"],),
                    )
                }
                for row in tasks
            }
            cursor_task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (tasks[1]["id"],)
            ).fetchone()
            cursor_session = connection.execute(
                """
                SELECT id FROM task_sessions
                WHERE task_id = ? AND role_key = 'fullstack'
                """,
                (tasks[1]["id"],),
            ).fetchone()
            with lifecycle_write_scope("advance_project"):
                self.assertEqual(
                    _fallback_provider_generation(
                        self.store,
                        connection,
                        cursor_task,
                        {
                            "id": "cursor-retry-fixture",
                            "task_session_id": cursor_session["id"],
                            "session_generation": 1,
                        },
                        "cursor_cli",
                        time.time(),
                        retry_same_provider=True,
                    ),
                    "cursor_cli",
                )
            retry = connection.execute(
                """
                SELECT task.retry_count, generation.generation,
                       generation.provider_key
                FROM tasks task
                JOIN task_sessions session ON session.task_id = task.id
                JOIN session_generations generation
                  ON generation.task_session_id = session.id
                WHERE task.id = ? AND session.role_key = 'fullstack'
                  AND generation.status = 'active'
                """,
                (tasks[1]["id"],),
            ).fetchone()
            self.assertEqual(tuple(retry), (1, 2, "cursor_cli"))
        finally:
            connection.close()
        self.assertEqual(
            [row["role_key"] for row in tasks],
            ["git_publisher", "fullstack", "fullstack"],
        )
        self.assertEqual(providers[tasks[1]["id"]]["fullstack"], "cursor_cli")
        self.assertEqual(providers[tasks[2]["id"]]["fullstack"], "codex_cli")
        connection = self.store.connect_readonly()
        try:
            active_first_roles = {
                row["role_key"]
                for row in connection.execute(
                    """
                    SELECT session.role_key
                    FROM task_sessions session
                    JOIN session_generations generation
                      ON generation.task_session_id = session.id
                    WHERE session.task_id = ? AND generation.status = 'active'
                    """,
                    (tasks[0]["id"],),
                )
            }
        finally:
            connection.close()
        self.assertEqual(
            active_first_roles,
            {"git_publisher", "deterministic_checker"},
        )
        authorization = json.loads(tasks[0]["spec_json"])["authorization"]
        self.assertEqual(authorization["source_message_id"], source_message_id)
        self.assertEqual(authorization["task_id"], tasks[0]["id"])
        self.assertEqual(
            authorization["spec_revision"],
            tasks[0]["spec_revision"],
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', outcome = 'cancelled',
                    phase = 'done', next_action_at = NULL, next_action_kind = NULL
                WHERE id = ?
                """,
                (tasks[0]["id"],),
            )
        self.assertEqual(tick(self.store)[0]["action"], "dependency_blocked")
        submit_action(
            self.store,
            "composite-plan",
            tasks[0]["id"],
            "rerun",
            "",
            "rerun-cancelled-planned-git",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")
        connection = self.store.connect_readonly()
        try:
            git_rerun = connection.execute(
                """
                SELECT public_status, phase, outcome, wait_reason
                FROM tasks WHERE id = ?
                """,
                (tasks[0]["id"],),
            ).fetchone()
            cursor_after_rerun = connection.execute(
                "SELECT public_status, phase, outcome FROM tasks WHERE id = ?",
                (tasks[1]["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            tuple(git_rerun)[:3],
            ("needs_decision", "intake", None),
        )
        self.assertIn("authorization", git_rerun["wait_reason"])
        self.assertEqual(
            tuple(cursor_after_rerun),
            ("pending", "queued", None),
        )
        submit_action(
            self.store,
            "composite-plan",
            tasks[0]["id"],
            "authorize",
            tasks[0]["id"],
            "reauthorize-cancelled-planned-git",
        )
        self.assertEqual(tick(self.store)[0]["action"], "authorize")
        connection = self.store.connect_readonly()
        try:
            reauthorized_git = connection.execute(
                """
                SELECT public_status, phase, outcome, spec_revision, spec_json
                FROM tasks WHERE id = ?
                """,
                (tasks[0]["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            tuple(reauthorized_git)[:4],
            ("pending", "execute", None, 4),
        )
        refreshed_authorization = json.loads(
            reauthorized_git["spec_json"]
        )["authorization"]
        self.assertEqual(
            refreshed_authorization["task_id"],
            tasks[0]["id"],
        )
        self.assertEqual(refreshed_authorization["spec_revision"], 4)
        repair_acceptances = json.loads(tasks[2]["acceptance_json"])
        self.assertIn(
            "review-findings-disposition",
            {item["id"] for item in repair_acceptances},
        )
        dependency_verdict = {
            "checker_verdict": "PASS",
            "acceptances": [
                {
                    "acceptance_id": "review-findings",
                    "actual_evidence": "Cursor produced ten numbered findings.",
                    "recheck_command": "read bounded Cursor transcript",
                }
            ],
        }
        dependency_path = self.data / "dependency-review-verdict.json"
        dependency_body = json.dumps(dependency_verdict).encode()
        dependency_path.write_bytes(dependency_body)
        dependency_report_path = self.data / "dependency-review-report.md"
        dependency_report_body = (
            "F-001 High: bounded review finding\n" + "evidence " * 1_350
        ).encode()
        dependency_report_path.write_bytes(dependency_report_body)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', phase = 'done',
                    outcome = 'done'
                WHERE id = ?
                """,
                (tasks[1]["id"],),
            )
            register_artifact(
                self.store,
                connection,
                project_id="composite-plan",
                task_id=tasks[1]["id"],
                kind="evidence",
                path=dependency_path,
                stored_path=self.store.relative_data_path(dependency_path),
                acceptance_id=None,
                revision=1,
                scope=task_data_scope(["review-findings"]),
                source_task_id=tasks[1]["id"],
                created_at=time.time(),
            )
            register_artifact(
                self.store,
                connection,
                project_id="composite-plan",
                task_id=tasks[1]["id"],
                kind="output",
                path=dependency_report_path,
                stored_path=self.store.relative_data_path(
                    dependency_report_path
                ),
                acceptance_id="provider_report",
                revision=1,
                scope=task_data_scope(["review-findings"]),
                source_task_id=tasks[1]["id"],
                created_at=time.time(),
            )
            repair_task = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (tasks[2]["id"],)
            ).fetchone()
            capsule = json.loads(
                compile_hot_context(
                    self.store, connection, repair_task, "fullstack"
                )
            )
        self.assertEqual(
            capsule["dependency_results"][0]["task_id"],
            tasks[1]["id"],
        )
        self.assertEqual(
            capsule["dependency_results"][0]["evidence"][0]["content"][
                "acceptances"
            ][0]["acceptance_id"],
            "review-findings",
        )
        self.assertEqual(
            next(
                item
                for item in capsule["dependency_results"][0]["artifacts"]
                if item["kind"] == "output"
            )["sha256"],
            hashlib.sha256(dependency_report_body).hexdigest(),
        )
        self.assertNotIn("objective", capsule["goal"])
        self.assertEqual(
            capsule["goal"]["objective_sha256"],
            hashlib.sha256(instruction.encode()).hexdigest(),
        )
        self.assertLessEqual(len(canonical_json(capsule).encode()), 16_384)
        with self.store.transaction() as connection:
            legacy_spec = json.loads(tasks[1]["spec_json"])
            self.assertFalse(legacy_spec["workspace_change_required"])
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', outcome = 'done',
                    phase = 'done', next_action_at = NULL, next_action_kind = NULL
                WHERE id = ?
                """,
                (tasks[0]["id"],),
            )
            connection.execute(
                """
                UPDATE tasks SET public_status = 'needs_decision',
                    phase = 'provider_recovery', wait_reason = 'provider failed',
                    fault_code = 'provider', next_action_at = NULL,
                    next_action_kind = NULL
                WHERE id = ?
                """,
                (tasks[1]["id"],),
            )
        submit_action(
            self.store,
            "composite-plan",
            tasks[1]["id"],
            "provide_decision",
            "继续啊",
            "continue-without-replacing-planned-spec",
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            continued = connection.execute(
                "SELECT spec_revision, spec_json FROM tasks WHERE id = ?",
                (tasks[1]["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(continued["spec_revision"], 1)
        self.assertIn("只读审查", json.loads(continued["spec_json"])["instruction"])
        legacy_spec["instruction"] = "继续啊"
        legacy_spec["workspace_change_required"] = True
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', outcome = 'cancelled',
                    phase = 'done', spec_json = ?,
                    next_action_at = NULL, next_action_kind = NULL
                WHERE id = ?
                """,
                (canonical_json(legacy_spec), tasks[1]["id"]),
            )
        self.assertEqual(tick(self.store)[0]["action"], "dependency_blocked")
        submit_action(
            self.store,
            "composite-plan",
            tasks[1]["id"],
            "rerun",
            "",
            "rerun-cancelled-plan-child",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")
        connection = self.store.connect_readonly()
        try:
            rerun_rows = connection.execute(
                """
                SELECT id, public_status, phase, outcome, spec_revision, spec_json
                FROM tasks WHERE id IN (?, ?)
                ORDER BY CASE id WHEN ? THEN 0 ELSE 1 END
                """,
                (tasks[1]["id"], tasks[2]["id"], tasks[1]["id"]),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            tuple(rerun_rows[0])[:5],
            (tasks[1]["id"], "pending", "execute", None, 2),
        )
        self.assertFalse(
            json.loads(rerun_rows[0]["spec_json"])["workspace_change_required"]
        )
        self.assertIn(
            "只读审查",
            json.loads(rerun_rows[0]["spec_json"])["instruction"],
        )
        self.assertEqual(
            tuple(rerun_rows[1])[:5],
            (tasks[2]["id"], "pending", "queued", None, 1),
        )

    def test_running_planner_host_job_reconciles_after_store_restart(self):
        self._use_real_planner_adapter()
        self._create_project(
            "planner-restart", "planner-restart-create", "/workspace/planner"
        )
        submit_message(
            self.store,
            "planner-restart",
            "前端和后端分两步写入验收文件",
            "planner-restart-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "running",
                    "session_id": "planner-restart-session",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={"chunks": []},
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "plan_wait")
        running = snapshot(self.db, self.data, "planner-restart")
        self.assertEqual(running["task"]["phase"], "plan_wait")
        self.assertEqual(running["host_jobs"][0]["status"], "running")
        planned = {
            "classification": {
                "size": "large",
                "reasons": ["two dependent deliverables require a serial DAG"],
                "information_sufficient": True,
                "requires_owner_choice": False,
            },
            "confidence": 0.98,
            "plan": {
                "summary": "two deterministic steps",
                "alternatives": [
                    {
                        "name": "serial",
                        "scope": "two files",
                        "cost": "low",
                        "risk": "low",
                        "reversible": True,
                        "acceptance": "both hashes pass",
                    },
                    {
                        "name": "combined",
                        "scope": "one broader write",
                        "cost": "medium",
                        "risk": "medium",
                        "reversible": True,
                        "acceptance": "manual combined review",
                    },
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "one",
                        "instruction": "写入 one.txt: one",
                    },
                    {
                        "key": "two",
                        "instruction": "写入 two.txt: two",
                        "depends_on": ["one"],
                    },
                ],
            },
        }
        planned = complete_planner_payload(planned)
        restarted = Store(self.db, self.data)
        restarted.initialize()
        with (
            patch(
                "plowwhip.planner.provider_job_status",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "planner-restart-session",
                    "input_tokens": 9,
                    "cached_input_tokens": 4,
                    "output_tokens": 3,
                    "model": "planner-test",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                        }
                    ]
                },
            ),
        ):
            with restarted.transaction() as connection:
                connection.execute(
                    """
                    UPDATE tasks SET next_action_at = ?
                    WHERE project_id = 'planner-restart'
                    """,
                    (time.time(),),
                )
            self.assertEqual(tick(restarted)[0]["action"], "planner_check")
            self.assertEqual(tick(restarted)[0]["action"], "plan_applied")
        finished = snapshot(self.db, self.data, "planner-restart")
        planner_jobs = [
            job for job in finished["host_jobs"] if job["purpose"] == "command"
        ]
        self.assertEqual(len(planner_jobs), 1)
        self.assertEqual(planner_jobs[0]["status"], "succeeded")

    def test_planner_terminal_failure_falls_back_without_resetting_task(self):
        self._use_real_planner_adapter()
        self._create_project(
            "planner-fallback", "planner-fallback-create", "/workspace/planner"
        )
        submit_message(
            self.store,
            "planner-fallback",
            "前端和后端比较方案后分别实现",
            "planner-fallback-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 1,
                    "failure_class": "provider_unavailable",
                    "input_tokens": 2,
                    "cached_input_tokens": 1,
                    "output_tokens": 1,
                    "model": "codex-test",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {"stream": "stderr", "text": "provider unavailable"}
                    ]
                },
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "provider_fallback")
        state = snapshot(self.db, self.data, "planner-fallback")
        self.assertEqual(state["task"]["phase"], "plan")
        self.assertEqual(state["task"]["spec_revision"], 1)
        planner = [
            (item["generation"], item["provider_key"], item["status"])
            for item in state["sessions"]
            if item["role_key"] == "planner"
        ]
        self.assertEqual(
            planner,
            [
                (1, "cursor_cli", "archived"),
                (2, "deepseek", "active"),
            ],
        )

    def test_planner_parse_failure_falls_back_instead_of_immediate_decision(self):
        """MECH-04 / LIVE-DS-05: verification parse failure is fallback-eligible."""
        self._use_real_planner_adapter()
        self._create_project(
            "planner-parse-fallback",
            "planner-parse-fallback-create",
            "/workspace/planner",
        )
        submit_message(
            self.store,
            "planner-parse-fallback",
            "前端和后端比较方案后分别实现",
            "planner-parse-fallback-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "planner-parse-session",
                    "input_tokens": 4,
                    "cached_input_tokens": 0,
                    "output_tokens": 4,
                    "model": "cursor-test",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": "completed without structured planner marker",
                        }
                    ]
                },
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "provider_fallback")
        state = snapshot(self.db, self.data, "planner-parse-fallback")
        self.assertEqual(state["task"]["public_status"], "in_progress")
        self.assertEqual(state["task"]["phase"], "plan")
        self.assertEqual(state["task"]["fault_code"], "verification")
        self.assertIn(
            "planner_rejected",
            {item["kind"] for item in state["events"]},
        )
        self.assertIn(
            "provider_fallback",
            {item["kind"] for item in state["events"]},
        )
        planner = [
            (item["generation"], item["provider_key"], item["status"])
            for item in state["sessions"]
            if item["role_key"] == "planner"
        ]
        self.assertEqual(
            planner,
            [
                (1, "cursor_cli", "archived"),
                (2, "deepseek", "active"),
            ],
        )

    def test_planner_start_rejection_falls_back_instead_of_unknown_outcome(self):
        self._use_real_planner_adapter()
        self._create_project(
            "planner-rejected",
            "planner-rejected-create",
            "/workspace/planner",
        )
        submit_message(
            self.store,
            "planner-rejected",
            "前端和后端比较方案后分别实现",
            "planner-rejected-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with patch(
            "plowwhip.planner.start_provider_job",
            side_effect=HostBridgeError(
                "Host Bridge rejected request",
                status=400,
                detail="executable not found",
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "provider_fallback")
        state = snapshot(self.db, self.data, "planner-rejected")
        self.assertEqual(state["task"]["public_status"], "in_progress")
        self.assertEqual(state["task"]["phase"], "plan")
        self.assertEqual(state["task"]["fault_code"], "provider")
        job = next(
            item for item in state["host_jobs"] if item["purpose"] == "command"
        )
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["returncode"], 125)
        self.assertEqual(job["failure_code"], "rejected")
        self.assertIn(
            "host_job_rejected",
            {item["kind"] for item in state["events"]},
        )
        planner = [
            (item["generation"], item["provider_key"], item["status"])
            for item in state["sessions"]
            if item["role_key"] == "planner"
        ]
        self.assertEqual(
            planner,
            [
                (1, "cursor_cli", "archived"),
                (2, "deepseek", "active"),
            ],
        )

    def test_planner_checker_rejection_blocks_plan_installation(self):
        submit_message(
            self.store,
            "planner-check-rejected",
            "写入 rejected.txt: must-not-run",
            "planner-check-rejected-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        rejected_output = (
            CHECKER_RESULT_PREFIX
            + json.dumps(
                {
                    "verdict": "CHANGES_REQUIRED",
                    "acceptances": [
                        {
                            "acceptance_id": "planner_contract",
                            "passed": False,
                            "actual_evidence": "Planner Artifact is not acceptable",
                            "recheck_command": "validate planner artifact",
                        }
                    ],
                    "decision_reason": "independent Planner check failed",
                }
            )
        )
        with patch(
            "plowwhip.lifecycle.perform_checker_step",
            return_value={
                "ok": True,
                "state": {
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "rejected-planner-checker",
                    "input_tokens": 4,
                    "cached_input_tokens": 0,
                    "output_tokens": 4,
                    "model": "checker-test",
                },
                "output": {
                    "complete": True,
                    "chunks": [{"stream": "stdout", "text": rejected_output}],
                },
            },
        ):
            result = tick(self.store)[0]
        self.assertEqual(
            (result["action"], result["status"]),
            ("needs_decision", "needs_decision"),
        )
        state = snapshot(self.db, self.data, "planner-check-rejected")
        self.assertEqual(state["task"]["phase"], "plan")
        self.assertEqual(state["task"]["fault_code"], "verification")
        self.assertEqual(len(state["tasks"]), 1)
        connection = self.store.connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plans WHERE selected = 1"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        self.assertEqual(
            [
                item["acceptance_id"]
                for item in state["artifacts"]
                if item["kind"] == "evidence"
            ],
            ["planner_contract"],
        )

    def test_planner_checker_provider_fallback_reuses_checked_artifact(self):
        submit_message(
            self.store,
            "planner-check-fallback",
            "写入 fallback.txt: checked",
            "planner-check-fallback-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        calls = 0

        def rejected_then_pass(step):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "ok": False,
                    "failure_kind": "rejected",
                    "failure_stage": "start",
                    "error_status": 400,
                    "error_detail": "first Checker unavailable",
                }
            return self._perform_checker_step(step)

        with patch(
            "plowwhip.lifecycle.perform_checker_step",
            side_effect=rejected_then_pass,
        ):
            self.assertEqual(tick(self.store)[0]["action"], "checker_fallback")
            waiting = snapshot(
                self.db, self.data, "planner-check-fallback"
            )
            self.assertEqual(waiting["task"]["phase"], "check_call")
            self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        finished = snapshot(self.db, self.data, "planner-check-fallback")
        self.assertEqual(
            len(
                [
                    item
                    for item in finished["host_jobs"]
                    if item["purpose"] == "command"
                ]
            ),
            1,
        )
        checker_jobs = [
            item
            for item in reversed(finished["host_jobs"])
            if item["purpose"] == "check"
        ]
        self.assertEqual(
            [item["status"] for item in checker_jobs],
            ["failed", "succeeded"],
        )
        self.assertEqual(
            [
                (item["generation"], item["provider_key"], item["status"])
                for item in finished["sessions"]
                if item["role_key"] == "independent_checker"
            ],
            [
                (1, "codex_cli", "archived"),
                (2, "cursor_cli", "archived"),
            ],
        )
        connection = self.store.connect()
        try:
            generation = connection.execute(
                """
                SELECT generation.handoff_ref
                FROM session_generations generation
                JOIN task_sessions session
                  ON session.id = generation.task_session_id
                WHERE session.task_id = ? AND session.role_key = ?
                  AND generation.generation = 2
                """,
                (
                    finished["task"]["id"],
                    "independent_checker",
                ),
            ).fetchone()
            latest_handoff = connection.execute(
                """
                SELECT path, sha256, revision FROM artifacts
                WHERE task_id = ? AND kind = 'handoff'
                  AND acceptance_id = 'handoff:independent_checker'
                ORDER BY revision DESC LIMIT 1
                """,
                (finished["task"]["id"],),
            ).fetchone()
            fallback_event = connection.execute(
                """
                SELECT detail_json FROM task_events
                WHERE task_id = ? AND kind = 'provider_fallback'
                ORDER BY rowid DESC LIMIT 1
                """,
                (finished["task"]["id"],),
            ).fetchone()
            detail = json.loads(fallback_event["detail_json"])
            bootstrap_handoff = connection.execute(
                """
                SELECT path, sha256, revision FROM artifacts
                WHERE task_id = ? AND kind = 'handoff' AND path = ?
                """,
                (finished["task"]["id"], detail["handoff_ref"]),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(generation["handoff_ref"], latest_handoff["path"])
        self.assertEqual(detail["handoff_ref"], bootstrap_handoff["path"])
        self.assertEqual(
            detail["handoff_sha256"], bootstrap_handoff["sha256"]
        )
        self.assertEqual(
            detail["handoff_revision"], bootstrap_handoff["revision"]
        )

    def test_high_risk_plan_asks_exactly_one_project_butler_question(self):
        self._use_real_planner_adapter()
        self._create_project("risk-plan", "project-1", "/workspace/risk-plan")
        submit_message(
            self.store,
            "risk-plan",
            "部署前端和后端到生产",
            "message-1",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        planned = {
            "classification": {
                "size": "large",
                "reasons": ["deployment affects multiple production scopes"],
                "information_sufficient": True,
                "requires_owner_choice": True,
            },
            "confidence": 0.99,
            "plan": {
                "summary": "prepare only",
                "alternatives": [
                    {
                        "name": "staged",
                        "scope": "prepare deployment changes",
                        "cost": "medium",
                        "risk": "medium",
                        "reversible": True,
                        "acceptance": "owner approves before external effect",
                    },
                    {
                        "name": "manual",
                        "scope": "leave deployment to owner",
                        "cost": "high",
                        "risk": "low",
                        "reversible": True,
                        "acceptance": "owner performs deployment",
                    },
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "backend",
                        "instruction": "准备后端部署清单但不执行部署",
                        "role_key": "fullstack",
                    },
                    {
                        "key": "frontend",
                        "instruction": "准备前端部署清单但不执行部署",
                        "depends_on": ["backend"],
                        "role_key": "fullstack",
                    },
                ],
            },
        }
        planned = complete_planner_payload(planned)
        with (
            patch(
                "plowwhip.planner.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "planner-session",
                    "input_tokens": 20,
                    "cached_input_tokens": 10,
                    "output_tokens": 20,
                    "model": "test-planner",
                },
            ),
            patch(
                "plowwhip.planner.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                        }
                    ]
                },
            ),
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "planner_check")
        result = tick(self.store)[0]
        self.assertEqual(result["status"], "needs_decision")
        messages = conversation(self.db, self.data, "risk-plan")["messages"]
        questions = [item for item in messages if item["role"] == "butler"]
        self.assertEqual(len(questions), 1)
        self.assertIn("只需要你决定一件事", questions[0]["content"])
        self.assertIn("是否批准", questions[0]["content"])
        self.assertEqual(tick(self.store), [])
        task = snapshot(self.db, self.data, "risk-plan")["task"]
        with self.assertRaisesRegex(ValueError, "exact Task ID"):
            submit_action(
                self.store,
                "risk-plan",
                task["id"],
                "authorize",
                "yes",
                "authorize-wrong",
            )
        submit_action(
            self.store,
            "risk-plan",
            task["id"],
            "authorize",
            task["id"],
            "authorize-plan",
        )
        connection = self.store.connect()
        try:
            authorization = json.loads(
                connection.execute(
                    """
                    SELECT action_json FROM messages
                    WHERE project_id = 'risk-plan' AND idempotency_key = 'authorize-plan'
                    """
                ).fetchone()["action_json"]
            )
        finally:
            connection.close()
        self.assertEqual(
            {
                key: authorization[key]
                for key in (
                    "task_id",
                    "spec_revision",
                    "action_kind",
                    "target_scope",
                )
            },
            {
                "task_id": task["id"],
                "spec_revision": 1,
                "action_kind": "select_plan",
                "target_scope": "/workspace/risk-plan",
            },
        )
        self.assertGreater(authorization["expires_at"], time.time())
        self.assertEqual(tick(self.store)[0]["action"], "authorize")
        after = snapshot(self.db, self.data, "risk-plan")
        self.assertEqual(len(after["tasks"]), 2)
        self.assertEqual(after["task"]["phase"], "execute")
        self.assertIn(
            "authorization_granted", {item["kind"] for item in after["events"]}
        )

    def test_only_one_owner_question_waits_across_projects(self):
        for index in (1, 2):
            project_id = f"question-{index}"
            self._create_project(
                project_id,
                f"Question {index}",
                f"/workspace/{project_id}",
            )
        task_ids = []
        for index in (1, 2):
            project_id = f"question-{index}"
            submit_message(
                self.store,
                project_id,
                f"write result-{index}.txt: question {index}",
                f"question-message-{index}",
            )
            self.assertEqual(tick(self.store)[0]["action"], "intake")
            task_id = snapshot(self.db, self.data, project_id)["task"]["id"]
            task_ids.append(task_id)
            with lifecycle_write_scope("advance_project"):
                with self.store.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE tasks SET public_status = 'needs_decision',
                            phase = 'plan', wait_reason = ?, fault_code = 'scope',
                            next_action_at = NULL, next_action_kind = NULL
                        WHERE id = ?
                        """,
                        (f"decision {index}", task_id),
                    )
                    _ensure_project_question(connection, project_id)
        with lifecycle_write_scope("advance_project"):
            with self.store.transaction() as connection:
                _ensure_project_question(connection, "question-1")
                _ensure_project_question(connection, "question-2")
        connection = self.store.connect_readonly()
        try:
            questions = connection.execute(
                """
                SELECT action_json FROM messages
                WHERE role = 'butler'
                  AND json_extract(action_json, '$.kind') = 'question'
                ORDER BY created_at, rowid
                """
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(questions), 1)
        self.assertTrue(json.loads(questions[0]["action_json"])["waiting"])
        with lifecycle_write_scope("advance_project"):
            with self.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE tasks SET public_status = 'done', outcome = 'done',
                        wait_reason = NULL, fault_code = NULL
                    WHERE id = ?
                    """,
                    (task_ids[0],),
                )
                _ensure_project_question(connection, "question-1")
        connection = self.store.connect_readonly()
        try:
            questions = connection.execute(
                """
                SELECT action_json FROM messages
                WHERE role = 'butler'
                  AND json_extract(action_json, '$.kind') = 'question'
                ORDER BY created_at, rowid
                """
            ).fetchall()
        finally:
            connection.close()
        waiting = [
            json.loads(row["action_json"])
            for row in questions
            if json.loads(row["action_json"]).get("waiting", True)
        ]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["task_id"], task_ids[1])

    def test_provider_facts_and_token_normalization_are_fail_closed(self):
        submit_message(self.store, "usage", "write result.txt: usage", "usage-1")
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        connection = self.store.connect()
        try:
            row = connection.execute(
                """
                SELECT task.id AS task_id, session.id AS session_id
                FROM tasks task JOIN task_sessions session ON session.task_id = task.id
                WHERE task.project_id = 'usage' AND session.role_key = 'deterministic'
                """
            ).fetchone()
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "single", 10, 8, 2,
                ),
                12,
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 100, 60, 20,
                ),
                120,
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 130, 90, 25,
                ),
                35,
            )
            with self.assertRaisesRegex(ValueError, "subset"):
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "single", 1, 2, 0,
                )
            # Cursor cumulative counters can jitter; decreases clamp to prior
            # sample and charge zero instead of failing the Cronner tick.
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 140, 89, 30,
                ),
                0,
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 140, 105, 30,
                ),
                0,
            )
            connection.execute(
                """
                UPDATE session_generations SET external_session_id = 'physical-2'
                WHERE task_session_id = ? AND generation = 1
                """,
                (row["session_id"],),
            )
            self.assertEqual(
                record_model_call(
                    connection, row["task_id"], row["session_id"], 1,
                    "local", "cumulative", 5, 2, 1,
                ),
                6,
            )
            connection.commit()
            calls = [
                tuple(item)
                for item in connection.execute(
                    """
                    SELECT normalized_total, physical_session_id
                    FROM model_calls ORDER BY rowid
                    """
                )
            ]
        finally:
            connection.close()
        self.assertEqual(
            [item[0] for item in calls], [20, 10, 12, 120, 35, 0, 0, 6]
        )
        self.assertNotEqual(calls[-2][1], calls[-1][1])
        usage = token_snapshot(self.db, self.data)
        self.assertEqual(
            {
                key: usage["all_history"][key]
                for key in (
                    "total_tokens",
                    "input_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "output_tokens",
                )
            },
            {
                "total_tokens": 203,
                "input_tokens": 160,
                "cached_input_tokens": 100,
                "uncached_input_tokens": 60,
                "output_tokens": 43,
            },
        )
        self.assertAlmostEqual(
            usage["all_history"]["ratios"]["input_per_output"], 160 / 43
        )
        self.assertAlmostEqual(
            usage["all_history"]["ratios"]["cached_per_uncached"], 100 / 60
        )
        self.assertEqual(usage["today"]["total_tokens"], 203)
        self.assertEqual(usage["today_projects"][0]["project_id"], "usage")
        self.assertEqual(usage["today_projects"][0]["total_tokens"], 203)
        self.assertEqual(usage["trend"][-1]["total_tokens"], 203)
        self.assertEqual(usage["projects"][0]["project_id"], "usage")
        self.assertEqual(usage["models"][0]["model"], "deterministic")
        self.assertEqual(usage["sessions"][0]["task_session_id"], row["session_id"])
        self.assertTrue(usage["sessions"][0]["worker_id"])
        self.assertEqual(usage["sessions"][0]["worker_role"], "deterministic")
        self.assertEqual(provider_facts("deterministic")[0]["available"], True)
        self.assertTrue(all(not item["available"] for item in provider_facts("planner")))
        self.assertIsNone(provider_adapter("local").report_context_usage())
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            provider_adapter("codex_cli")

    def test_project_archive_preserves_history_and_monitor_is_read_only(self):
        self._create_project("archive-me", "archive-create")
        history = conversation(self.db, self.data, "archive-me")
        self.assertEqual(history["project"]["id"], "archive-me")
        self.assertEqual(history["messages"], [])

        before = self._row_counts()
        state = monitor_snapshot(self.db, self.data)
        after = self._row_counts()
        self.assertEqual(before, after)
        self.assertTrue(state["read_only"])
        self.assertEqual(state["database"]["journal_mode"], "wal")
        self.assertEqual(state["database"]["quick_check"], ["ok"])
        self.assertEqual(state["database"]["schema_version"], 10)

        archive_project(
            self.store, "archive-me", "archive-me", "archive-confirmed"
        )
        self.assertEqual(tick(self.store)[0]["action"], "archive_project")
        self.assertEqual(projects_snapshot(self.db, self.data)["projects"], [])
        archived_history = conversation(self.db, self.data, "archive-me")
        self.assertEqual(archived_history["messages"], [])
        state = monitor_snapshot(self.db, self.data)
        self.assertEqual(state["summary"]["projects"], 0)
        self.assertEqual(state["summary"]["archived_projects"], 1)

        create_project(self.store, "archive-me", "archive-restore")
        self.assertEqual(tick(self.store)[0]["action"], "restore_project")
        self.assertEqual(
            projects_snapshot(self.db, self.data)["projects"][0]["project_id"],
            "archive-me",
        )
        submit_message(
            self.store, "archive-me", "写入 active.txt: active", "archive-active"
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        with self.assertRaisesRegex(ValueError, "active task"):
            archive_project(
                self.store, "archive-me", "archive-me", "archive-rejected"
            )

    def test_chinese_project_name_and_semantic_create_deduplication(self):
        created = create_project(
            self.store,
            None,
            "chinese-create",
            "/workspace/review",
            "审查代码",
        )
        self.assertEqual(created["result"], "created")
        self.assertRegex(created["project_id"], r"^project-[0-9a-f]{32}$")
        self.assertEqual(tick(self.store)[0]["action"], "create_project")

        project_id = str(created["project_id"])
        state = projects_snapshot(self.db, self.data)["projects"][0]
        self.assertEqual(
            (state["project_id"], state["display_name"], state["host_path"]),
            (project_id, "审查代码", "/workspace/review"),
        )
        connection = self.store.connect_readonly()
        try:
            before = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        unchanged = create_project(
            self.store,
            None,
            "chinese-repeat",
            "/workspace/review",
            "审查代码",
        )
        self.assertEqual(
            unchanged,
            {
                "message_id": None,
                "project_id": project_id,
                "result": "unchanged",
            },
        )
        connection = self.store.connect_readonly()
        try:
            after = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(after, before)
        self.assertEqual(conversation(self.db, self.data, project_id)["messages"], [])

        routed = route_global_message(
            self.store,
            "@审查代码 写入 review.txt: ok",
            "chinese-route",
        )
        self.assertEqual(routed["project_id"], project_id)
        self.assertEqual(tick(self.store)[0]["action"], "intake")

    def test_global_butler_routes_search_without_creating_a_task(self):
        self._create_project("alpha", "alpha-create")
        submit_message(
            self.store, "alpha", "写入 unique-alpha.txt: found", "alpha-task"
        )
        run_until_idle(self.store)
        self._create_project("beta", "beta-create")
        before = self._row_counts()[3]
        routed = route_global_message(
            self.store, "找 unique-alpha.txt 任务", "global-search"
        )
        self.assertEqual(routed["project_id"], "alpha")
        self.assertTrue(routed["routed_only"])
        self.assertTrue(routed["results"])
        self.assertEqual(tick(self.store)[0]["action"], "global_route")
        self.assertEqual(self._row_counts()[3], before)
        transfer = json.loads(
            (
                self.data
                / "global"
                / "conversations"
                / f"{routed['message_id']}.json"
            ).read_text()
        )
        self.assertEqual(
            set(transfer),
            {"message_id", "routed_project_id", "created_at"},
        )
        instruction = route_global_message(
            self.store,
            "@beta 写入 routed.txt: routed",
            "global-instruction",
        )
        self.assertEqual(instruction["project_id"], "beta")
        self.assertFalse(instruction["routed_only"])
        self.assertEqual(tick(self.store)[0]["action"], "intake")

    def test_settings_and_library_are_indexed_and_read_only(self):
        state = settings_library_snapshot(self.db, self.data)
        self.assertEqual(len(state["settings"]), 16)
        self.assertEqual(
            next(
                item["value"]
                for item in state["settings"]
                if item["setting_key"] == "max_total_tokens"
            ),
            10_000_000,
        )
        self.assertEqual(len(state["library"]), 12)
        self.assertEqual(
            {item["kind"] for item in state["library"]},
            {"role", "rule", "worker_template"},
        )
        role_path = self.data / "library" / "roles" / "deterministic.md"
        role_path.write_text(role_path.read_text() + "\nExtra deterministic boundary.\n")
        self.store.initialize()
        updated = settings_library_snapshot(self.db, self.data)
        role = next(item for item in updated["library"] if item["item_key"] == "deterministic")
        self.assertEqual(role["revision"], 2)
        self.assertTrue(all(item["sha256_matches"] for item in updated["library"]))

    def test_hot_warm_cold_continuity_is_bounded_and_append_only(self):
        submit_message(
            self.store,
            "continuity",
            "写入 result.txt: continuity",
            "continuity-1",
        )
        run_until_idle(self.store)
        connection = self.store.connect()
        try:
            task = connection.execute(
                "SELECT * FROM tasks WHERE project_id = 'continuity'"
            ).fetchone()
            capsule = compile_hot_context(
                self.store, connection, task, "deterministic"
            )
            segment_rows = connection.execute(
                """
                SELECT path, acceptance_id FROM artifacts
                WHERE task_id = ? AND kind = 'log'
                  AND acceptance_id LIKE 'session_segment:%'
                ORDER BY path
                """,
                (task["id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertLessEqual(len(capsule.encode()), 16_384)
        capsule_payload = json.loads(capsule)
        self.assertNotIn("objective", capsule_payload["goal"])
        self.assertRegex(capsule_payload["goal"]["objective_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(segment_rows), 4)
        manifests = [
            json.loads(self.store.resolve_data_path(row["path"]).read_text())
            for row in segment_rows
        ]
        self.assertEqual(
            {manifest["role_key"] for manifest in manifests},
            {
                "planner",
                "independent_checker",
                "deterministic",
                "deterministic_checker",
            },
        )
        self.assertTrue(all(manifest["host_jobs"] for manifest in manifests))
        self.assertFalse(any(self.data.rglob("current.md")))
        before = [path for path in self.data.rglob("segment-*.json")]
        checkpoint_project(self.store, "continuity")
        self.assertEqual(before, [path for path in self.data.rglob("segment-*.json")])

    def test_checkpoint_overflow_converges_to_explicit_needs_decision(self):
        self._create_project(
            "checkpoint-overflow", "checkpoint-overflow", str(self.root)
        )
        submit_message(
            self.store,
            "checkpoint-overflow",
            "检查当前代码并给出结论",
            "checkpoint-overflow-message",
        )
        with patch(
            "plowwhip.cronner.checkpoint_project",
            side_effect=ValueError("handoff exceeds configured 512-byte cap"),
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "checkpoint_needs_decision")
        state = snapshot(self.db, self.data, "checkpoint-overflow")
        self.assertEqual(state["task"]["public_status"], "needs_decision")
        self.assertEqual(state["task"]["fault_code"], "scope")
        self.assertIn(
            "checkpoint_failed", {event["kind"] for event in state["events"]}
        )

    def test_deadline_reconciles_and_gracefully_stops_active_host_job(self):
        self._create_project(
            "deadline", "deadline-project", "/workspace/deadline"
        )
        submit_message(
            self.store,
            "deadline",
            "实现截止时间处理",
            "deadline-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        with patch(
            "plowwhip.execution.workspace_snapshot",
            return_value={"git": {"head": "before"}},
        ):
            self.assertEqual(tick(self.store)[0]["action"], "snapshot")
        running_state = {
            "status": "running",
            "session_id": "deadline-session",
        }
        with (
            patch(
                "plowwhip.execution.start_provider_job",
                return_value=running_state,
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={"chunks": []},
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "dispatch")
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET deadline_at = ?, next_action_at = ?
                WHERE project_id = 'deadline'
                """,
                (time.time() - 1, time.time() + 3_600),
            )
        result = tick(self.store)[0]
        self.assertEqual(result["action"], "deadline_reconcile")
        reconciling = snapshot(self.db, self.data, "deadline")
        self.assertEqual(
            reconciling["task"]["phase"], "timeout_reconcile"
        )
        self.assertEqual(reconciling["host_jobs"][0]["status"], "running")
        with (
            patch(
                "plowwhip.execution.provider_job_status",
                return_value={
                    **running_state,
                    "deadline_reached_at": time.time(),
                },
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={
                    "status": "running",
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": "bounded output before stop\n",
                        }
                    ],
                    "stream_refs": {
                        "stdout": {
                            "path": "deadline/stdout.segment-000001.log",
                            "bytes": 27,
                            "sha256": "a" * 64,
                            "complete": True,
                        }
                    },
                },
            ),
            patch(
                "plowwhip.execution.cancel_provider_job"
            ) as cancel_before_checkpoint,
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "deadline_stop")
        cancel_before_checkpoint.assert_not_called()
        stopped = snapshot(self.db, self.data, "deadline")
        self.assertEqual(stopped["task"]["phase"], "stopping")
        self.assertEqual(stopped["task"]["next_action_kind"], "cancel")
        self.assertEqual(stopped["host_jobs"][0]["status"], "cancelling")
        self.assertIn(
            "timeout_reconcile",
            {
                item["acceptance_id"]
                for item in stopped["artifacts"]
                if item["kind"] == "evidence"
            },
        )
        self.assertTrue(
            bool(stopped["handoffs"])
        )
        with (
            patch(
                "plowwhip.execution.cancel_provider_job",
                return_value={"status": "running"},
            ) as graceful_stop,
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={"chunks": []},
            ),
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "cancel")
        graceful_stop.assert_called_once_with(
            stopped["host_jobs"][0]["id"],
            force=False,
        )
        with self.store.transaction() as connection:
            job = connection.execute(
                """
                SELECT id, dispatch_json FROM host_jobs
                WHERE task_id = ? AND status = 'cancelling'
                """,
                (stopped["task"]["id"],),
            ).fetchone()
            dispatch = json.loads(job["dispatch_json"])
            dispatch["cancel_sent_at"] = time.time() - 60
            connection.execute(
                "UPDATE host_jobs SET dispatch_json = ? WHERE id = ?",
                (json.dumps(dispatch, sort_keys=True), job["id"]),
            )
            connection.execute(
                "UPDATE tasks SET next_action_at = ? WHERE id = ?",
                (time.time(), stopped["task"]["id"]),
            )
        with (
            patch(
                "plowwhip.execution.cancel_provider_job",
                return_value={"status": "cancelled", "returncode": -9},
            ) as forced_stop,
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={"chunks": []},
            ),
            patch(
                "plowwhip.execution.workspace_snapshot",
                return_value={"git": {"head": "before"}},
            ),
        ):
            result = tick(self.store)[0]
        forced_stop.assert_called_once_with(job["id"], force=True)
        self.assertEqual(result["status"], "needs_decision")
        final = snapshot(self.db, self.data, "deadline")
        self.assertIsNone(final["task"]["outcome"])
        self.assertEqual(final["task"]["phase"], "provider_recovery")
        self.assertIn(
            "deadline_stopped", {item["kind"] for item in final["events"]}
        )

    def test_deadline_reconcile_accepts_late_terminal_result_without_stop(self):
        self._create_project(
            "deadline-late", "deadline-late-project", "/workspace/deadline-late"
        )
        submit_message(
            self.store,
            "deadline-late",
            "分析当前实现并给出完整结论",
            "deadline-late-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        before = {
            "git": {
                "head": "late-head",
                "workspace_fingerprint": "a" * 64,
            }
        }
        with patch(
            "plowwhip.execution.workspace_snapshot",
            return_value=before,
        ):
            self.assertEqual(tick(self.store)[0]["action"], "snapshot")
        with (
            patch(
                "plowwhip.execution.start_provider_job",
                return_value={
                    "status": "running",
                    "session_id": "late-session",
                },
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={"status": "running", "chunks": []},
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "dispatch")
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET deadline_at = ?, next_action_at = ?
                WHERE project_id = 'deadline-late'
                """,
                (time.time() - 1, time.time()),
            )
        self.assertEqual(
            tick(self.store)[0]["action"], "deadline_reconcile"
        )
        with (
            patch(
                "plowwhip.execution.provider_job_status",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "session_id": "late-session",
                    "input_tokens": 8,
                    "cached_input_tokens": 3,
                    "output_tokens": 2,
                    "model": "late-test",
                    "deadline_reached_at": time.time(),
                },
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={
                    "status": "completed",
                    "complete": True,
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": "complete late analysis result",
                        }
                    ],
                },
            ),
            patch(
                "plowwhip.execution.workspace_snapshot",
                return_value=before,
            ),
            patch(
                "plowwhip.execution.cancel_provider_job"
            ) as cancel_job,
        ):
            result = tick(self.store)[0]
        self.assertEqual(result["action"], "execute")
        cancel_job.assert_not_called()
        late = snapshot(self.db, self.data, "deadline-late")
        self.assertEqual(late["task"]["phase"], "verify")
        self.assertIsNone(late["task"]["deadline_at"])
        self.assertEqual(late["host_jobs"][0]["status"], "succeeded")
        self.assertIn(
            "deadline_terminal_reconciled",
            {item["kind"] for item in late["events"]},
        )
        self.assertTrue(
            any(
                item["acceptance_id"] == "provider_report"
                for item in late["artifacts"]
            )
        )

    def test_context_policy_compaction_event_and_non_native_rotation(self):
        self._create_project("context-run", "context-project", "/workspace/context")
        set_project_setting(
            self.store,
            "context-run",
            "rotation_input_tokens",
            1_000,
            "context-threshold",
        )
        self.assertEqual(tick(self.store)[0]["action"], "set_project_setting")
        submit_message(
            self.store,
            "context-run",
            "实现上下文轮转",
            "context-task",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        self.assertEqual(tick(self.store)[0]["action"], "planner_check")
        self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
        with patch(
            "plowwhip.execution.workspace_snapshot",
            return_value={"git": {"head": "before"}},
        ):
            self.assertEqual(tick(self.store)[0]["action"], "snapshot")
        completed = {
            "status": "completed",
            "returncode": 0,
            "session_id": "cursor-context-session",
            "input_tokens": 1_200,
            "cached_input_tokens": 800,
            "output_tokens": 100,
            "model": "cursor-test",
        }
        compact_line = json.dumps(
            {
                "type": "session.compacted",
                "rotation_id": "compact-1",
                "before_bytes": 9_000,
                "after_bytes": 2_000,
            }
        )
        with (
            patch(
                "plowwhip.execution.start_provider_job",
                return_value=completed,
            ) as started,
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={
                    "chunks": [{"stream": "stdout", "text": compact_line + "\n"}]
                },
            ),
            patch(
                "plowwhip.execution.workspace_snapshot",
                return_value={"git": {"head": "after"}},
            ),
        ):
            self.assertEqual(tick(self.store)[0]["action"], "execute")
        policy = started.call_args.kwargs["context_policy"]
        self.assertEqual(policy["provider_compaction_token_limit"], 120_000)
        self.assertEqual(policy["rotation_max_bytes"], 65_536)
        connection = self.store.connect()
        try:
            task_id = connection.execute(
                "SELECT id FROM tasks WHERE project_id = 'context-run'"
            ).fetchone()["id"]
            generations = connection.execute(
                """
                SELECT generation.generation, generation.status
                FROM session_generations generation
                JOIN task_sessions session
                  ON session.id = generation.task_session_id
                WHERE session.task_id = ? AND session.role_key = 'fullstack'
                ORDER BY generation.generation
                """,
                (task_id,),
            ).fetchall()
            event_kinds = {
                row["kind"]
                for row in connection.execute(
                    "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
                )
            }
        finally:
            connection.close()
        self.assertEqual([tuple(row) for row in generations], [(1, "active")])
        self.assertNotIn("context_generation_rotated", event_kinds)
        self.assertIn("provider_compacted", event_kinds)
        self.assertEqual(
            parse_context_events(compact_line),
            [
                {
                    "type": "session.compacted",
                    "rotation_id": "compact-1",
                    "before_bytes": 9_000,
                    "after_bytes": 2_000,
                    "provider_event_type": "session.compacted",
                }
            ],
        )

    def test_provider_probe_tasks_record_zero_and_minimal_token_evidence(self):
        spec, _ = normalize_instruction("探测 Provider codex_cli: minimal")
        self.assertEqual(spec["kind"], "authorization_required")
        spec, _ = normalize_instruction("探测 Provider cursor_cli: minimal")
        self.assertEqual(spec["kind"], "authorization_required")
        spec, _ = normalize_instruction(
            "探测 Provider cursor_cli: minimal 确认 cursor_cli"
        )
        self.assertEqual(spec["kind"], "provider_probe")
        self.assertEqual(spec["provider_key"], "cursor_cli")
        requests = []
        store = self.store

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, self.headers["Authorization"], payload))
                with store.transaction() as connection:
                    updated = connection.execute(
                        """
                        UPDATE projects SET created_at = created_at + 0
                        WHERE id = 'monitor-probe-codex_cli'
                        """
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("probe project was not durable before call")
                if self.path == "/v1/probe":
                    body = {"available": True, "detail": "codex-cli test"}
                elif self.path == "/v1/jobs/start":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 0,
                        "input_tokens": 30,
                        "cached_input_tokens": 10,
                        "output_tokens": 2,
                        "model": "codex-test",
                        "session_id": "probe-session",
                    }
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "chunks": [
                            {
                                "stream": "stdout",
                                "text": "PLOWWHIP_PROBE_OK\n",
                            }
                        ],
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        environment = {
            "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
            "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
            "PLOW_WHIP_PROBE_PROJECT_PATH": str(self.root),
        }
        try:
            with patch.dict(os.environ, environment):
                submit_message(
                    self.store,
                    "monitor-probe-codex_cli",
                    "探测 Provider codex_cli: 0token",
                    "probe-zero",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    ["intake", "planner_check", "plan_applied", "execute", "verify"],
                )
                zero = snapshot(
                    self.db, self.data, "monitor-probe-codex_cli"
                )
                self.assertEqual(zero["task"]["public_status"], "done")
                executor = next(
                    item for item in zero["sessions"] if item["role_key"] == "provider_probe"
                )
                self.assertEqual(executor["provider_key"], "codex_cli")
                zero_evidence = json.loads(
                    next(
                        Path(item["path"]).read_text()
                        for item in zero["artifacts"]
                        if item["kind"] == "evidence"
                        and item["acceptance_id"] == "provider_zero_probe"
                    )
                )
                self.assertTrue(zero_evidence["passed"])
                self.assertFalse(zero_evidence["model_invoked"])
                self.assertEqual(zero_evidence["total_tokens"], 0)

                submit_message(
                    self.store,
                    "monitor-probe-codex_cli",
                    "探测 Provider codex_cli: minimal 确认 codex_cli",
                    "probe-minimal",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    ["intake", "planner_check", "plan_applied", "execute", "verify"],
                )
                minimal = snapshot(
                    self.db, self.data, "monitor-probe-codex_cli"
                )
                self.assertEqual(minimal["task"]["public_status"], "done")
                self.assertEqual(
                    minimal["task"]["checker_role_key"],
                    "independent_checker",
                )
                model_probe_evidence = json.loads(
                    next(
                        Path(item["path"]).read_text()
                        for item in minimal["artifacts"]
                        if item["kind"] == "evidence"
                        and item["acceptance_id"] == "provider_minimal_probe"
                    )
                )
                self.assertEqual(model_probe_evidence["subject"], "model_probe")
                self.assertTrue(model_probe_evidence["passed"])
                self.assertRegex(
                    model_probe_evidence["source_artifact"]["artifact_sha256"],
                    r"^[0-9a-f]{64}$",
                )
                probe_session_ids = {
                    item["task_session_id"]
                    for item in minimal["sessions"]
                    if item["role_key"] == "provider_probe"
                }
                self.assertEqual(
                    [
                        item["normalized_total"]
                        for item in minimal["model_usage"]
                        if item["task_session_id"] in probe_session_ids
                    ],
                    [32],
                )
                state = monitor_snapshot(self.db, self.data)
                codex = next(
                    item
                    for item in state["providers"]
                    if item["provider_key"] == "codex_cli"
                )
                self.assertEqual(codex["latest_probe"]["result"]["total_tokens"], 32)
                self.assertEqual(codex["latest_probe"]["public_status"], "done")
                self.assertEqual(codex["zero_probe"]["result"]["total_tokens"], 0)
                self.assertEqual(
                    codex["readiness"]["recent_execution_health"], "healthy"
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()
        self.assertEqual(
            [item[0] for item in requests],
            ["/v1/probe", "/v1/jobs/start", "/v1/jobs/output"],
        )
        self.assertTrue(all(item[1] == "Bearer test-token" for item in requests))

    def test_active_model_probe_stops_before_cancelled_terminal_state(self):
        requests = []
        jobs = {}

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append(self.path)
                if self.path == "/v1/jobs/start":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "running",
                        "session_id": "active-probe-session",
                    }
                    jobs[payload["job_id"]] = body
                elif self.path == "/v1/jobs/cancel":
                    body = {
                        **jobs[payload["job_id"]],
                        "status": "cancelled",
                        "returncode": -15,
                    }
                    jobs[payload["job_id"]] = body
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": jobs[payload["job_id"]]["status"],
                        "chunks": [],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        environment = {
            "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
            "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
            "PLOW_WHIP_PROBE_PROJECT_PATH": str(self.root),
        }
        try:
            with patch.dict(os.environ, environment):
                submit_message(
                    self.store,
                    "cancel-probe",
                    "探测 Provider codex_cli: minimal 确认 codex_cli",
                    "cancel-probe-message",
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(
                    tick(self.store)[0]["action"], "planner_check"
                )
                self.assertEqual(
                    tick(self.store)[0]["action"], "plan_applied"
                )
                self.assertEqual(tick(self.store)[0]["action"], "probe_wait")
                running = snapshot(self.db, self.data, "cancel-probe")
                submit_action(
                    self.store,
                    "cancel-probe",
                    running["task"]["id"],
                    "cancel",
                    "",
                    "cancel-probe-action",
                )
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
                stopping = snapshot(self.db, self.data, "cancel-probe")
                self.assertIsNone(stopping["task"]["outcome"])
                self.assertEqual(stopping["task"]["phase"], "stopping")
                self.assertIsNone(
                    stopping["task"]["terminal_capabilities_revoked_at"]
                )
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()
        cancelled = snapshot(self.db, self.data, "cancel-probe")
        self.assertEqual(cancelled["task"]["outcome"], "cancelled")
        self.assertIsNotNone(
            cancelled["task"]["terminal_capabilities_revoked_at"]
        )
        self.assertFalse(
            any(
                item["status"] in {"dispatching", "running", "cancelling"}
                for item in cancelled["host_jobs"]
            )
        )
        event_kinds = {item["kind"] for item in cancelled["events"]}
        self.assertIn("temporary_capabilities_revoked", event_kinds)
        self.assertEqual(
            requests,
            [
                "/v1/jobs/start",
                "/v1/jobs/output",
                "/v1/jobs/cancel",
                "/v1/jobs/output",
            ],
        )

    def test_general_code_task_uses_registered_workspace_and_independent_checker(self):
        requests = []
        snapshots = 0
        jobs = {}

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal snapshots
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, payload))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "project_path": payload["project_path"],
                        "git": {
                            "available": True,
                            "head": "abc123",
                            "status": "" if snapshots == 0 else " M plowwhip/app.py",
                            "diff_stat": "" if snapshots == 0 else " 1 file changed",
                        },
                    }
                    snapshots += 1
                elif self.path == "/v1/jobs/start":
                    failed = (
                        payload["access"] == "read"
                        and payload["adapter"] == "codex"
                    )
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 1 if failed else 0,
                        "duration_ms": 12,
                        "input_tokens": 20,
                        "cached_input_tokens": 5,
                        "output_tokens": 3,
                        "model": "codex-test",
                        "session_id": (
                            "checker-session"
                            if payload["access"] == "read"
                            else "executor-session"
                        ),
                    }
                    if payload["access"] == "read":
                        body.update(
                            {
                                "input_tokens": 10,
                                "cached_input_tokens": 4,
                                "output_tokens": 1,
                            }
                        )
                    jobs[payload["job_id"]] = {
                        **body,
                        "access": payload["access"],
                        "failed": failed,
                    }
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "chunks": [
                            {
                                "stream": "stdout",
                                "text": (
                                    (
                                        "checker provider failed"
                                        if jobs[payload["job_id"]]["failed"]
                                        else checker_output()
                                    )
                                    if jobs[payload["job_id"]]["access"] == "read"
                                    else "implemented and tests passed"
                                ),
                            }
                        ],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            self._create_project(
                "code-task",
                "code-project",
                str(self.root),
            )
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                submit_message(
                    self.store,
                    "code-task",
                    "给项目页增加一个可访问的刷新按钮并运行测试",
                    "code-message",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    [
                        "intake",
                        "planner_check",
                        "plan_applied",
                        "snapshot",
                        "execute",
                        "checker_fallback",
                        "verify",
                    ],
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

        view = snapshot(self.db, self.data, "code-task")
        self.assertEqual(view["task"]["public_status"], "done")
        self.assertEqual(view["task"]["outcome"], "done")
        self.assertEqual(view["host_path"], str(self.root))
        self.assertEqual(
            [
                (
                    item["role_key"],
                    item["generation"],
                    item["provider_key"],
                    item["external_session_id"],
                )
                for item in view["sessions"]
            ],
            [
                ("fullstack", 1, "cursor_cli", "executor-session"),
                ("independent_checker", 1, "codex_cli", "checker-session"),
                ("independent_checker", 2, "cursor_cli", "checker-session"),
                (
                    "planner",
                    1,
                    "codex_cli",
                    f"planner-{view['task']['id']}",
                ),
            ],
        )
        self.assertEqual(
            [item["purpose"] for item in reversed(view["host_jobs"])],
            ["command", "check", "execute", "check", "check"],
        )
        self.assertEqual(
            sum(item["normalized_total"] for item in view["model_usage"]),
            75,
        )
        evidence = json.loads(
            next(
                Path(item["path"]).read_text()
                for item in view["artifacts"]
                if item["kind"] == "evidence"
                and item["acceptance_id"] != "planner_contract"
            )
        )
        self.assertTrue(evidence["workspace_changed"])
        self.assertTrue(evidence["passed"])
        self.assertEqual(
            [item[0] for item in requests],
            [
                "/v1/evidence/snapshot",
                "/v1/jobs/start",
                "/v1/jobs/output",
                "/v1/evidence/snapshot",
                "/v1/jobs/start",
                "/v1/jobs/output",
                "/v1/jobs/start",
                "/v1/jobs/output",
            ],
        )
        self.assertEqual(requests[1][1]["adapter"], "cursor")
        self.assertEqual(requests[4][1]["access"], "read")
        self.assertEqual(requests[6][1]["adapter"], "cursor")

    def test_read_only_analysis_can_finish_from_evidence_without_workspace_delta(self):
        self._create_project(
            "analysis", "analysis-create", "/workspace/analysis"
        )
        submit_message(
            self.store,
            "analysis",
            "分析当前项目的刷新流程并给出有证据的结论",
            "analysis-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        unchanged = {
            "git": {
                "available": True,
                "head": "abc123",
                "status": "",
                "diff_stat": "",
            }
        }
        with (
            patch(
                "plowwhip.execution.workspace_snapshot",
                return_value=unchanged,
            ),
            patch(
                "plowwhip.execution.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "input_tokens": 5,
                    "cached_input_tokens": 2,
                    "output_tokens": 2,
                    "model": "cursor-test",
                    "session_id": "analysis-session",
                },
            ) as executor_start,
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": json.dumps(
                                {
                                    "type": "result",
                                    "subtype": "success",
                                    "result": (
                                        "F-001 · High · plowwhip/app.py:1 · "
                                        "bounded findings with file references"
                                    ),
                                }
                            ),
                        }
                    ]
                },
            ),
            patch(
                "plowwhip.verification.start_provider_job",
                return_value={
                    "status": "completed",
                    "returncode": 0,
                    "input_tokens": 4,
                    "cached_input_tokens": 1,
                    "output_tokens": 1,
                    "model": "codex-test",
                    "session_id": "analysis-checker",
                },
            ) as checker_start,
            patch(
                "plowwhip.verification.provider_job_output",
                return_value={
                    "chunks": [
                        {"stream": "stdout", "text": checker_output()}
                    ]
                },
            ),
        ):
            self.assertEqual(
                [item["action"] for item in run_until_idle(self.store)],
                ["planner_check", "plan_applied", "snapshot", "execute", "verify"],
            )
        self.assertEqual(executor_start.call_args.kwargs["access"], "read")
        checker_prompt = checker_start.call_args.args[3]
        self.assertIn("Control-plane executor provider: cursor_cli", checker_prompt)
        self.assertNotIn("F-001 · High", checker_prompt)
        self.assertIn("Frozen formal result manifest", checker_prompt)
        self.assertIn("Frozen execution target HEAD: abc123", checker_prompt)
        self.assertIn(
            "do not fail solely because live HEAD differs", checker_prompt
        )
        state = snapshot(self.db, self.data, "analysis")
        self.assertEqual(state["task"]["outcome"], "done")
        execution_manifest = next(
            json.loads(Path(item["path"]).read_text())
            for item in state["artifacts"]
            if item["kind"] == "artifact"
            and item["path"].endswith("provider-execution.json")
        )
        self.assertEqual(execution_manifest["provider_key"], "cursor_cli")
        report_path = self.store.resolve_data_path(
            execution_manifest["provider_report_ref"]
        )
        report_body = report_path.read_bytes()
        self.assertIn("F-001 · High", report_body.decode())
        self.assertEqual(
            hashlib.sha256(report_body).hexdigest(),
            execution_manifest["provider_report_sha256"],
        )
        self.assertEqual(
            len(report_body), execution_manifest["provider_report_bytes"]
        )
        self.assertFalse(execution_manifest["provider_report_truncated"])
        evidence = next(
            json.loads(Path(item["path"]).read_text())
            for item in state["artifacts"]
            if item["kind"] == "evidence"
            and item["acceptance_id"] is None
        )
        self.assertFalse(evidence["workspace_changed"])
        self.assertFalse(evidence["workspace_change_required"])
        self.assertTrue(evidence["passed"])

    def test_read_only_rerun_recovers_report_without_reinvoking_provider(self):
        self._create_project(
            "report-recovery", "report-recovery-create", "/workspace/recovery"
        )
        submit_message(
            self.store,
            "report-recovery",
            "使用 Cursor 只读审查当前代码并输出报告",
            "report-recovery-message",
        )
        self.assertEqual(tick(self.store)[0]["action"], "intake")
        unchanged = {"git": {"available": True, "head": "abc123", "status": ""}}
        provider_state = {
            "status": "completed",
            "returncode": 0,
            "session_id": "report-recovery-session",
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 3,
            "model": "cursor-test",
        }
        provider_output = {
            "chunks": [
                {
                    "stream": "stdout",
                    "text": json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "result": "F-001 High: persisted report finding",
                        }
                    ),
                }
            ]
        }
        with (
            patch("plowwhip.execution.workspace_snapshot", return_value=unchanged),
            patch(
                "plowwhip.execution.start_provider_job",
                return_value=provider_state,
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value=provider_output,
            ),
            patch(
                "plowwhip.verification.start_provider_job",
                return_value={
                    **provider_state,
                    "session_id": "report-recovery-checker",
                },
            ),
            patch(
                "plowwhip.verification.provider_job_output",
                return_value={
                    "chunks": [{"stream": "stdout", "text": checker_output()}]
                },
            ),
        ):
            self.assertEqual(
                [item["action"] for item in run_until_idle(self.store)],
                ["planner_check", "plan_applied", "snapshot", "execute", "verify"],
            )
        with self.store.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM tasks WHERE project_id = 'report-recovery'"
            ).fetchone()
            source_job_id = connection.execute(
                """
                SELECT id FROM host_jobs
                WHERE task_id = ? AND purpose = 'execute' AND status = 'succeeded'
                ORDER BY sequence LIMIT 1
                """,
                (task["id"],),
            ).fetchone()["id"]
            calls_before = connection.execute(
                "SELECT COUNT(*) AS value FROM model_calls WHERE task_id = ?",
                (task["id"],),
            ).fetchone()["value"]
            connection.execute(
                """
                UPDATE tasks SET public_status = 'done', outcome = 'cancelled',
                    phase = 'done', next_action_at = NULL, next_action_kind = NULL
                WHERE id = ?
                """,
                (task["id"],),
            )
        submit_action(
            self.store,
            "report-recovery",
            task["id"],
            "rerun",
            "",
            "report-recovery-rerun",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")
        with patch(
            "plowwhip.execution.workspace_snapshot", return_value=unchanged
        ) as unexpected_recovery_snapshot:
            self.assertEqual(tick(self.store)[0]["action"], "snapshot")
        unexpected_recovery_snapshot.assert_not_called()
        with (
            patch(
                "plowwhip.execution.provider_job_status",
                return_value=provider_state,
            ) as recovered_status,
            patch(
                "plowwhip.execution.provider_job_output",
                return_value=provider_output,
            ) as recovered_output,
            patch(
                "plowwhip.execution.workspace_snapshot",
                return_value=unchanged,
            ) as unexpected_recovery_after,
            patch("plowwhip.execution.start_provider_job") as unexpected_start,
        ):
            self.assertEqual(tick(self.store)[0]["action"], "execute")
        recovered_status.assert_called_once_with(source_job_id)
        recovered_output.assert_called_once_with(
            source_job_id, complete=True
        )
        unexpected_recovery_after.assert_not_called()
        unexpected_start.assert_not_called()
        connection = self.store.connect_readonly()
        try:
            recovered_task = connection.execute(
                "SELECT phase, public_status FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            calls_after = connection.execute(
                "SELECT COUNT(*) AS value FROM model_calls WHERE task_id = ?",
                (task["id"],),
            ).fetchone()["value"]
            manifest_path = connection.execute(
                """
                SELECT path FROM artifacts
                WHERE task_id = ? AND path LIKE '%/provider-execution.json'
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (task["id"],),
            ).fetchone()["path"]
            recovered_job_id = connection.execute(
                """
                SELECT id FROM host_jobs
                WHERE task_id = ? AND purpose = 'execute' AND status = 'succeeded'
                ORDER BY sequence DESC LIMIT 1
                """,
                (task["id"],),
            ).fetchone()["id"]
            root_job = _root_provider_execution(
                self.store,
                connection,
                task["id"],
                task["spec_revision"],
                recovered_job_id,
            )
        finally:
            connection.close()
        recovered_manifest = json.loads(
            self.store.resolve_data_path(manifest_path).read_text()
        )
        self.assertEqual(tuple(recovered_task), ("verify", "in_progress"))
        self.assertEqual(calls_after, calls_before)
        self.assertEqual(
            recovered_manifest["reused_from_host_job_id"], source_job_id
        )
        self.assertEqual(root_job[0], source_job_id)
        self.assertEqual(recovered_manifest["before"], unchanged["git"])
        self.assertEqual(recovered_manifest["after"], unchanged["git"])
        self.assertIn(
            "F-001",
            self.store.resolve_data_path(
                recovered_manifest["provider_report_ref"]
            ).read_text(),
        )
        self.store.resolve_data_path(
            recovered_manifest["provider_report_ref"]
        ).write_text("tampered")
        with patch(
            "plowwhip.verification.start_provider_job"
        ) as unexpected_checker:
            self.assertEqual(tick(self.store)[0]["action"], "needs_decision")
        unexpected_checker.assert_not_called()
        tampered = snapshot(self.db, self.data, "report-recovery")
        self.assertEqual(tampered["task"]["public_status"], "needs_decision")
        self.assertIn("SHA-256", tampered["task"]["wait_reason"])

    def test_terminal_provider_failure_falls_back_with_new_generation(self):
        snapshots = 0
        jobs = {}
        adapters = []

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal snapshots
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/evidence/snapshot":
                    states = ["", "", "", " M changed.py"]
                    body = {
                        "git": {
                            "available": True,
                            "head": "abc123",
                            "status": states[min(snapshots, len(states) - 1)],
                            "diff_stat": (
                                "1 file changed" if snapshots >= len(states) - 1 else ""
                            ),
                        }
                    }
                    snapshots += 1
                elif self.path == "/v1/jobs/start":
                    adapters.append(payload["adapter"])
                    checking = payload["access"] == "read"
                    failed = payload["adapter"] == "cursor" and not checking
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 1 if failed else 0,
                        "failure_class": "provider_unavailable" if failed else None,
                        "input_tokens": 2,
                        "cached_input_tokens": 1,
                        "output_tokens": 1,
                        "model": f"{payload['adapter']}-test",
                        "session_id": (
                            "checker-session"
                            if checking
                            else f"{payload['adapter']}-session"
                        ),
                    }
                    jobs[payload["job_id"]] = {**body, "access": payload["access"]}
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": jobs[payload["job_id"]]["status"],
                        "chunks": [
                            {
                                "stream": "stdout",
                                "text": (
                                    "provider failed"
                                    if jobs[payload["job_id"]]["returncode"]
                                    else (
                                        checker_output()
                                        if jobs[payload["job_id"]]["access"] == "read"
                                        else "implemented"
                                    )
                                ),
                            }
                        ],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                self._create_project("fallback", "fallback-project", str(self.root))
                submit_message(
                    self.store, "fallback", "先失败再自动递补完成", "fallback-message"
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    [
                        "intake",
                        "planner_check",
                        "plan_applied",
                        "snapshot",
                        "provider_fallback",
                        "snapshot",
                        "execute",
                        "verify",
                    ],
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

        view = snapshot(self.db, self.data, "fallback")
        self.assertEqual(view["task"]["outcome"], "done")
        fullstack = [
            (item["generation"], item["provider_key"], item["status"])
            for item in view["sessions"]
            if item["role_key"] == "fullstack"
        ]
        self.assertEqual(
            fullstack,
            [(1, "cursor_cli", "archived"), (2, "codex_cli", "archived")],
        )
        self.assertEqual(adapters, ["cursor", "codex", "codex"])
        self.assertIn("provider_fallback", {item["kind"] for item in view["events"]})

    def test_running_host_job_releases_sqlite_and_can_reconcile_or_cancel(self):
        entered = threading.Event()
        release = threading.Event()
        checker_entered = threading.Event()
        checker_release = threading.Event()
        snapshots = 0
        jobs = {}

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal snapshots
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "git": {
                            "available": True,
                            "head": "abc123",
                            "status": "" if snapshots % 2 == 0 else " M changed.py",
                            "diff_stat": "" if snapshots % 2 == 0 else "1 file changed",
                        }
                    }
                    snapshots += 1
                elif self.path == "/v1/jobs/start":
                    if payload["access"] == "read":
                        checker_entered.set()
                        checker_release.wait(2)
                        body = {
                            "job_id": payload["job_id"],
                            "status": "running",
                            "session_id": "durable-checker",
                        }
                    else:
                        entered.set()
                        release.wait(2)
                        body = {
                            "job_id": payload["job_id"],
                            "status": "running",
                            "session_id": "durable-executor",
                        }
                    jobs[payload["job_id"]] = {
                        **body,
                        "access": payload["access"],
                    }
                elif self.path == "/v1/jobs/status":
                    body = {
                        **jobs[payload["job_id"]],
                        "status": "completed",
                        "returncode": 0,
                        "input_tokens": 8,
                        "cached_input_tokens": 3,
                        "output_tokens": 2,
                        "model": "codex-test",
                    }
                    jobs[payload["job_id"]] = body
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": jobs[payload["job_id"]]["status"],
                        "chunks": [
                            {
                                "stream": "stdout",
                                "text": (
                                    checker_output()
                                    if jobs[payload["job_id"]]["access"] == "read"
                                    else "bounded progress\n"
                                ),
                            }
                        ],
                    }
                elif self.path == "/v1/jobs/cancel":
                    body = {
                        **jobs[payload["job_id"]],
                        "status": "cancelled",
                        "returncode": -15,
                    }
                    jobs[payload["job_id"]] = body
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        environment = {
            "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
            "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
        }
        try:
            with patch.dict(os.environ, environment):
                self._create_project("durable-code", "durable-project", str(self.root))
                submit_message(
                    self.store, "durable-code", "实现持久 HostJob", "durable-message"
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(tick(self.store)[0]["action"], "planner_check")
                self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
                self.assertEqual(tick(self.store)[0]["action"], "snapshot")
                dispatched = []
                dispatch_thread = threading.Thread(
                    target=lambda: dispatched.extend(tick(self.store)), daemon=True
                )
                dispatch_thread.start()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                submit_message(
                    self.store, "side-project", "写入 side.txt: ok", "side-message"
                )
                self.assertLess(time.monotonic() - started, 0.5)
                release.set()
                dispatch_thread.join(2)
                self.assertFalse(dispatch_thread.is_alive())
                self.assertEqual(dispatched[0]["action"], "dispatch")
                running = snapshot(self.db, self.data, "durable-code")
                self.assertEqual(running["task"]["phase"], "execute_wait")
                self.assertEqual(running["host_jobs"][0]["status"], "running")
                self.assertIsNone(running["host_jobs"][0]["ended_at"])
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET next_action_at = ? WHERE project_id = ?",
                        (time.time() + 60, "durable-code"),
                    )
                run_until_idle(self.store)
                self.assertEqual(
                    snapshot(self.db, self.data, "side-project")["task"]["outcome"],
                    "done",
                )
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET next_action_at = ? WHERE project_id = ?",
                        (time.time(), "durable-code"),
                    )
                self.assertEqual(tick(self.store)[0]["action"], "execute")
                submit_message(
                    self.store, "checker-side", "写入 checker-side.txt: ok", "checker-side"
                )
                verified = []
                verify_thread = threading.Thread(
                    target=lambda: verified.extend(tick(self.store)), daemon=True
                )
                verify_thread.start()
                self.assertTrue(checker_entered.wait(1))
                deadline = time.monotonic() + 0.5
                checker_side = snapshot(self.db, self.data, "checker-side")
                while checker_side["task"] is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                    checker_side = snapshot(self.db, self.data, "checker-side")
                self.assertIsNotNone(checker_side["task"])
                started = time.monotonic()
                submit_message(
                    self.store, "checker-write", "写入 write.txt: ok", "checker-write"
                )
                self.assertLess(time.monotonic() - started, 0.5)
                checker_release.set()
                verify_thread.join(2)
                self.assertFalse(verify_thread.is_alive())
                self.assertEqual(
                    {(item["project_id"], item["action"]) for item in verified},
                    {("durable-code", "check_wait"), ("checker-side", "intake")},
                )
                checker_wait = snapshot(self.db, self.data, "durable-code")
                self.assertEqual(checker_wait["task"]["phase"], "check_wait")
                restarted = Store(self.db, self.data)
                restarted.initialize()
                with restarted.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE tasks SET next_action_at = ?
                        WHERE project_id = 'durable-code'
                        """,
                        (time.time(),),
                    )
                self.assertEqual(tick(restarted)[0]["action"], "verify")
                self.assertEqual(
                    snapshot(self.db, self.data, "durable-code")["task"]["outcome"],
                    "done",
                )
                run_until_idle(self.store)
                self.assertEqual(
                    snapshot(self.db, self.data, "checker-side")["task"]["outcome"],
                    "done",
                )
                self.assertEqual(
                    snapshot(self.db, self.data, "checker-write")["task"]["outcome"],
                    "done",
                )

                entered.clear()
                release.set()
                self._create_project("cancel-code", "cancel-project", str(self.root))
                submit_message(self.store, "cancel-code", "实现后取消", "cancel-message")
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(tick(self.store)[0]["action"], "planner_check")
                self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
                self.assertEqual(tick(self.store)[0]["action"], "snapshot")
                self.assertEqual(tick(self.store)[0]["action"], "dispatch")
                cancel_view = snapshot(self.db, self.data, "cancel-code")
                submit_action(
                    self.store,
                    "cancel-code",
                    cancel_view["task"]["id"],
                    "cancel",
                    "",
                    "cancel-running",
                )
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
                cancelled = snapshot(self.db, self.data, "cancel-code")
                self.assertEqual(cancelled["task"]["outcome"], "cancelled")
                self.assertEqual(cancelled["host_jobs"][0]["status"], "cancelled")
        finally:
            release.set()
            checker_release.set()
            bridge.shutdown()
            bridge.server_close()
            bridge_thread.join()

    def test_ambiguous_dispatch_stops_for_decision_without_blind_replay(self):
        starts = 0

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal starts
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/jobs/start":
                    starts += 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/v1/jobs/cancel":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "cancelled",
                        "returncode": -15,
                    }
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "cancelled",
                        "chunks": [],
                    }
                else:
                    body = {
                        "git": {
                            "available": True,
                            "head": "abc123",
                            "status": "",
                            "diff_stat": "",
                        }
                    }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": f"http://127.0.0.1:{bridge.server_port}",
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                self._create_project("ambiguous", "ambiguous-project", str(self.root))
                submit_message(
                    self.store, "ambiguous", "不要盲目重复执行", "ambiguous-message"
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(tick(self.store)[0]["action"], "planner_check")
                self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
                self.assertEqual(tick(self.store)[0]["action"], "snapshot")
                self.assertEqual(tick(self.store)[0]["action"], "start")
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET next_action_at = ? WHERE project_id = 'ambiguous'",
                        (time.time(),),
                    )
                self.assertEqual(tick(self.store)[0]["action"], "needs_decision")
                view = snapshot(self.db, self.data, "ambiguous")
                self.assertEqual(view["task"]["public_status"], "needs_decision")
                self.assertEqual(view["task"]["fault_code"], "unsafe_unknown")
                self.assertEqual(view["host_jobs"][0]["status"], "dispatching")
                self.assertEqual(starts, 2)

                submit_action(
                    self.store,
                    "ambiguous",
                    view["task"]["id"],
                    "provide_decision",
                    "重新执行",
                    "unsafe-replay",
                )
                self.assertEqual(tick(self.store)[0]["action"], "provide_decision")
                self.assertEqual(
                    snapshot(self.db, self.data, "ambiguous")["task"]["public_status"],
                    "needs_decision",
                )
                submit_action(
                    self.store,
                    "ambiguous",
                    view["task"]["id"],
                    "confirm_not_executed",
                    view["host_jobs"][0]["id"],
                    "confirmed-not-accepted",
                )
                self.assertEqual(
                    tick(self.store)[0]["action"], "confirm_not_executed"
                )
                resolved = snapshot(self.db, self.data, "ambiguous")
                self.assertEqual(resolved["task"]["outcome"], "cancelled")
                self.assertEqual(resolved["host_jobs"][0]["status"], "failed")
                self.assertEqual(
                    resolved["host_jobs"][0]["failure_code"], "not_accepted"
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

    def test_git_publish_uses_structured_worker_and_deterministic_evidence(self):
        requests = []
        head = "a" * 40
        result = {
            "kind": "git_publish",
            "remote_ssh": "git@github.com:niugengtian/PlowWhip_Webv2.git",
            "branch": "blue",
            "local_head": head,
            "remote_head": head,
            "pushed": True,
            "secret_scan_passed": True,
            "files_scanned": 12,
            "bytes_scanned": 2048,
        }

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, payload))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "git": {
                            "kind": "workspace",
                            "available": True,
                            "fingerprint": "fixture",
                            "head": head,
                            "status": "",
                        }
                    }
                elif self.path == "/v1/jobs/start":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 0,
                        "duration_ms": 4,
                    }
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "chunks": [
                            {
                                "stream": "stdout",
                                "text": json.dumps(result),
                            }
                        ],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        instruction = (
            "你将本地代码上传到github 注意避免上传.env之类的指令。用ssh 上传到"
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue"
        )
        try:
            self._create_project(
                "git-publish", "git-publish-project", str(self.root)
            )
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": (
                        f"http://127.0.0.1:{bridge.server_port}"
                    ),
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                submit_message(
                    self.store,
                    "git-publish",
                    instruction,
                    "git-publish-message",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    [
                        "intake",
                        "planner_check",
                        "plan_applied",
                        "snapshot",
                        "execute",
                        "verify",
                    ],
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

        view = snapshot(self.db, self.data, "git-publish")
        spec = json.loads(view["task"]["spec_json"])
        self.assertEqual(view["task"]["outcome"], "done")
        self.assertEqual(view["task"]["role_key"], "git_publisher")
        self.assertEqual(view["task"]["checker_role_key"], "deterministic_checker")
        self.assertEqual(spec["kind"], "git_publish")
        self.assertEqual(spec["branch"], "blue")
        self.assertEqual(
            spec["remote_ssh"],
            "git@github.com:niugengtian/PlowWhip_Webv2.git",
        )
        self.assertEqual(
            sorted(item["normalized_total"] for item in view["model_usage"]),
            [10, 20],
        )
        self.assertEqual(
            {
                item["role_key"]: item["provider_key"]
                for item in view["sessions"]
            },
            {
                "git_publisher": "git_publish",
                "deterministic_checker": "local",
                "independent_checker": "codex_cli",
                "planner": "codex_cli",
            },
        )
        evidence = json.loads(
            next(
                Path(item["path"]).read_text()
                for item in view["artifacts"]
                if item["kind"] == "evidence"
                and item["acceptance_id"] == "git_publish_contract"
            )
        )
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["remote_head"], head)
        start = next(payload for path, payload in requests if path == "/v1/jobs/start")
        self.assertEqual(start["adapter"], "git-publish")
        self.assertEqual(start["access"], "write")
        dispatch = json.loads(start["prompt"])
        self.assertEqual(dispatch["expected_head"], head)
        self.assertEqual(dispatch["authorization"]["target_scope"], (
            "git@github.com:niugengtian/PlowWhip_Webv2.git#refs/heads/blue"
        ))
        self.assertEqual(dispatch["authorization"]["expected_head"], head)
        self.assertEqual(dispatch["publish_mode"], "fast_forward")

    def test_active_git_publish_stops_and_revokes_authorization_before_cancel(self):
        head = "e" * 40
        requests = []
        jobs = {}

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append(self.path)
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "project_path": payload["project_path"],
                        "git": {
                            "available": True,
                            "head": head,
                            "status": "",
                            "diff_stat": "",
                            "workspace_fingerprint": "f" * 64,
                        },
                    }
                elif self.path == "/v1/jobs/start":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "running",
                        "session_id": "active-git-publish",
                    }
                    jobs[payload["job_id"]] = body
                elif self.path == "/v1/jobs/cancel":
                    body = {
                        **jobs[payload["job_id"]],
                        "status": "cancelled",
                        "returncode": -15,
                    }
                    jobs[payload["job_id"]] = body
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": jobs[payload["job_id"]]["status"],
                        "chunks": [],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": (
                        f"http://127.0.0.1:{bridge.server_port}"
                    ),
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                self._create_project(
                    "cancel-git",
                    "cancel-git-project",
                    str(self.root),
                )
                submit_message(
                    self.store,
                    "cancel-git",
                    "用ssh上传到"
                    "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue",
                    "cancel-git-message",
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(
                    tick(self.store)[0]["action"], "planner_check"
                )
                self.assertEqual(
                    tick(self.store)[0]["action"], "plan_applied"
                )
                self.assertEqual(tick(self.store)[0]["action"], "snapshot")
                self.assertEqual(tick(self.store)[0]["action"], "dispatch")
                running = snapshot(self.db, self.data, "cancel-git")
                self.assertIn(
                    "authorization",
                    json.loads(running["task"]["spec_json"]),
                )
                submit_action(
                    self.store,
                    "cancel-git",
                    running["task"]["id"],
                    "cancel",
                    "",
                    "cancel-git-action",
                )
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
                stopping = snapshot(self.db, self.data, "cancel-git")
                self.assertIsNone(stopping["task"]["outcome"])
                self.assertIsNone(
                    stopping["task"]["terminal_capabilities_revoked_at"]
                )
                self.assertEqual(tick(self.store)[0]["action"], "cancel")
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()
        cancelled = snapshot(self.db, self.data, "cancel-git")
        self.assertEqual(cancelled["task"]["outcome"], "cancelled")
        self.assertIsNotNone(
            cancelled["task"]["terminal_capabilities_revoked_at"]
        )
        revocation = json.loads(
            next(
                item["detail_json"]
                for item in cancelled["events"]
                if item["kind"] == "temporary_capabilities_revoked"
            )
        )
        self.assertRegex(
            revocation["authorization_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(
            any(
                item["status"] in {"dispatching", "running", "cancelling"}
                for item in cancelled["host_jobs"]
            )
        )

        submit_action(
            self.store,
            "cancel-git",
            cancelled["task"]["id"],
            "rerun",
            "",
            "cancel-git-rerun",
        )
        self.assertEqual(tick(self.store)[0]["action"], "rerun")
        rerun = snapshot(self.db, self.data, "cancel-git")
        self.assertNotIn(
            "authorization",
            json.loads(rerun["task"]["spec_json"]),
        )
        self.assertEqual(rerun["task"]["public_status"], "needs_decision")
        self.assertEqual(rerun["task"]["fault_code"], "scope")
        self.assertIn("/v1/jobs/cancel", requests)

    def test_git_publish_conflict_has_two_scoped_recovery_actions(self):
        head = "c" * 40
        remote_head = "d" * 40
        failure = {
            "kind": "git_publish",
            "status": "failed",
            "code": "remote_history_conflict",
            "branch": "blue",
            "local_head": head,
            "remote_head": remote_head,
            "error": "non-fast-forward",
        }

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "git": {
                            "kind": "workspace",
                            "available": True,
                            "fingerprint": "fixture",
                            "head": head,
                            "status": "",
                        }
                    }
                elif self.path == "/v1/jobs/start":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 1,
                        "duration_ms": 4,
                    }
                elif self.path == "/v1/jobs/output":
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "chunks": [
                            {
                                "stream": "stderr",
                                "text": json.dumps(failure),
                            }
                        ],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        instruction = (
            "用ssh上传到"
            "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue"
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": (
                        f"http://127.0.0.1:{bridge.server_port}"
                    ),
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                for project_id in ("new-branch", "force-lease"):
                    self._create_project(
                        project_id, f"{project_id}-project", str(self.root)
                    )
                    submit_message(
                        self.store,
                        project_id,
                        instruction,
                        f"{project_id}-message",
                    )
                    self.assertEqual(
                        [item["action"] for item in run_until_idle(self.store)],
                        [
                            "intake",
                            "planner_check",
                            "plan_applied",
                            "snapshot",
                            "execute",
                        ],
                    )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

        new_view = snapshot(self.db, self.data, "new-branch")
        self.assertEqual(new_view["task"]["fault_code"], "scope")
        self.assertIn(remote_head, new_view["task"]["wait_reason"])
        self.assertTrue(new_view["decision_context"]["complete"])
        self.assertEqual(
            new_view["decision_context"]["reason_code"],
            "remote_history_conflict",
        )
        self.assertEqual(new_view["decision_context"]["local_head"], head)
        self.assertEqual(new_view["decision_context"]["remote_head"], remote_head)
        self.assertEqual(len(new_view["decision_context"]["options"]), 2)
        self.assertEqual(
            json.loads(new_view["events"][0]["detail_json"])["remote_head"],
            remote_head,
        )
        submit_action(
            self.store,
            "new-branch",
            new_view["task"]["id"],
            "publish_new_branch",
            "blue-v1",
            "publish-blue-v1",
        )
        self.assertEqual(tick(self.store)[0]["action"], "publish_new_branch")
        recovered = snapshot(self.db, self.data, "new-branch")
        recovered_spec = json.loads(recovered["task"]["spec_json"])
        self.assertEqual(recovered["task"]["public_status"], "pending")
        self.assertEqual(recovered["task"]["spec_revision"], 3)
        self.assertEqual(recovered_spec["branch"], "blue-v1")
        self.assertEqual(recovered_spec["publish_mode"], "fast_forward")
        self.assertEqual(
            recovered_spec["authorization"]["target_scope"],
            "git@github.com:niugengtian/PlowWhip_Webv2.git#refs/heads/blue-v1",
        )
        self.assertEqual(recovered_spec["authorization"]["expected_head"], head)
        self.assertIsInstance(
            recovered_spec["authorization"]["source_decision_event_id"], int
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET next_action_at = NULL WHERE id = ?",
                (recovered["task"]["id"],),
            )

        force_view = snapshot(self.db, self.data, "force-lease")
        with self.assertRaisesRegex(ValueError, "exact 40-character remote SHA"):
            submit_action(
                self.store,
                "force-lease",
                force_view["task"]["id"],
                "force_publish_with_lease",
                "short",
                "bad-lease",
            )
        with self.assertRaisesRegex(ValueError, "match the displayed evidence"):
            submit_action(
                self.store,
                "force-lease",
                force_view["task"]["id"],
                "force_publish_with_lease",
                "e" * 40,
                "wrong-valid-lease",
            )
        submit_action(
            self.store,
            "force-lease",
            force_view["task"]["id"],
            "force_publish_with_lease",
            remote_head,
            "force-with-exact-lease",
        )
        self.assertEqual(
            tick(self.store)[0]["action"], "force_publish_with_lease"
        )
        forced = snapshot(self.db, self.data, "force-lease")
        forced_spec = json.loads(forced["task"]["spec_json"])
        self.assertEqual(forced["task"]["public_status"], "pending")
        self.assertEqual(forced_spec["publish_mode"], "force_with_lease")
        self.assertEqual(forced_spec["expected_remote_head"], remote_head)
        self.assertEqual(
            forced_spec["authorization"]["action_kind"],
            "git_publish_force_with_lease",
        )

    def test_legacy_git_failure_requires_read_only_context_before_authorization(self):
        local_head = "a" * 40
        remote_head = "b" * 40
        operations = {}
        requests = []

        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                requests.append((self.path, payload))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "git": {
                            "kind": "workspace",
                            "available": True,
                            "fingerprint": "fixture",
                            "head": local_head,
                            "status": "",
                        }
                    }
                elif self.path == "/v1/jobs/start":
                    operation = json.loads(payload["prompt"]).get(
                        "operation", "publish"
                    )
                    operations[payload["job_id"]] = operation
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "returncode": 0 if operation == "inspect" else 1,
                        "duration_ms": 4,
                    }
                elif self.path == "/v1/jobs/output":
                    operation = operations[payload["job_id"]]
                    result = (
                        {
                            "kind": "git_publish_inspection",
                            "remote_ssh": (
                                "git@github.com:niugengtian/PlowWhip_Webv2.git"
                            ),
                            "branch": "blue",
                            "local_head": local_head,
                            "remote_head": remote_head,
                            "relationship": "different",
                            "external_write": False,
                        }
                        if operation == "inspect"
                        else {
                            "kind": "git_publish",
                            "status": "failed",
                            "error": (
                                "提示： 一个仓库已向该引用进行了推送。"
                                "如果您希望先与远程变更合并，请在推送前执行 'git pull'。"
                            ),
                        }
                    )
                    body = {
                        "job_id": payload["job_id"],
                        "status": "completed",
                        "chunks": [
                            {
                                "stream": (
                                    "stdout" if operation == "inspect" else "stderr"
                                ),
                                "text": json.dumps(result, ensure_ascii=False),
                            }
                        ],
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            self._create_project("legacy-git", "legacy-project", str(self.root))
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": (
                        f"http://127.0.0.1:{bridge.server_port}"
                    ),
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                submit_message(
                    self.store,
                    "legacy-git",
                    "用ssh上传到"
                    "https://github.com/niugengtian/PlowWhip_Webv2/tree/blue",
                    "legacy-message",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    [
                        "intake",
                        "planner_check",
                        "plan_applied",
                        "snapshot",
                        "execute",
                    ],
                )
                legacy = snapshot(self.db, self.data, "legacy-git")
                self.assertIsNone(legacy["decision_context"])
                with self.store.transaction() as connection:
                    previous = connection.execute(
                        """
                        SELECT task_session_id, session_generation FROM host_jobs
                        WHERE task_id = ? ORDER BY sequence DESC LIMIT 1
                        """,
                        (legacy["task"]["id"],),
                    ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO host_jobs(
                            id, task_id, task_session_id, session_generation,
                            spec_revision, sequence, purpose, status, started_at,
                            ended_at, returncode, failure_code
                        ) VALUES (?, ?, ?, ?, 2, 4, 'execute', 'failed', ?, ?, 125,
                                  'rejected')
                        """,
                        (
                            "rejected-before-inspection",
                            legacy["task"]["id"],
                            previous["task_session_id"],
                            previous["session_generation"],
                            time.time(),
                            time.time(),
                        ),
                    )
                with self.assertRaisesRegex(ValueError, "refresh Git publish"):
                    submit_action(
                        self.store,
                        "legacy-git",
                        legacy["task"]["id"],
                        "publish_new_branch",
                        "blue-v1",
                        "premature-publish",
                    )
                submit_action(
                    self.store,
                    "legacy-git",
                    legacy["task"]["id"],
                    "refresh_git_publish_context",
                    "",
                    "refresh-context",
                )
                self.assertEqual(
                    tick(self.store)[0]["action"],
                    "refresh_git_publish_context",
                )
                self.assertEqual(
                    [item["action"] for item in run_until_idle(self.store)],
                    ["snapshot", "inspect"],
                )
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()

        refreshed = snapshot(self.db, self.data, "legacy-git")
        self.assertEqual(refreshed["task"]["public_status"], "needs_decision")
        self.assertEqual(refreshed["task"]["spec_revision"], 3)
        self.assertTrue(refreshed["decision_context"]["complete"])
        self.assertEqual(
            refreshed["decision_context"]["reason_code"],
            "remote_history_conflict",
        )
        self.assertEqual(refreshed["decision_context"]["local_head"], local_head)
        self.assertEqual(refreshed["decision_context"]["remote_head"], remote_head)
        self.assertEqual(len(refreshed["decision_context"]["evidence"]), 3)
        starts = [payload for path, payload in requests if path == "/v1/jobs/start"]
        self.assertEqual(starts[-1]["access"], "read")
        self.assertEqual(json.loads(starts[-1]["prompt"])["operation"], "inspect")
        connection = self.store.connect_readonly()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM model_calls WHERE task_id = ?",
                    (refreshed["task"]["id"],),
                ).fetchone()["count"],
                2,
            )
        finally:
            connection.close()

    def test_rejected_start_is_terminal_not_unknown(self):
        class Bridge(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(length))
                if self.path == "/v1/evidence/snapshot":
                    body = {
                        "git": {
                            "available": True,
                            "fingerprint": "fixture",
                            "head": "b" * 40,
                            "status": "",
                        }
                    }
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/v1/jobs/start":
                    data = json.dumps(
                        {"detail": f"host job not found: {payload['job_id']}"}
                    ).encode()
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)

            def log_message(self, *_args):
                return

        bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            self._create_project("rejected", "rejected-project", str(self.root))
            with patch.dict(
                os.environ,
                {
                    "PLOW_WHIP_BRIDGE_URL": (
                        f"http://127.0.0.1:{bridge.server_port}"
                    ),
                    "PLOW_WHIP_BRIDGE_TOKEN": "test-token",
                },
            ):
                submit_message(
                    self.store,
                    "rejected",
                    "检查当前代码并给出结论",
                    "rejected-message",
                )
                self.assertEqual(tick(self.store)[0]["action"], "intake")
                self.assertEqual(tick(self.store)[0]["action"], "planner_check")
                self.assertEqual(tick(self.store)[0]["action"], "plan_applied")
                self.assertEqual(tick(self.store)[0]["action"], "snapshot")
                self.assertEqual(tick(self.store)[0]["action"], "provider_fallback")
        finally:
            bridge.shutdown()
            bridge.server_close()
            thread.join()
        state = snapshot(self.db, self.data, "rejected")
        first = state["host_jobs"][0]
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["failure_code"], "rejected")
        self.assertNotEqual(state["task"]["fault_code"], "unsafe_unknown")

    def test_failure_after_start_acceptance_is_not_misclassified_as_rejection(self):
        step = ProviderStep(
            "start",
            "project",
            "task",
            "job",
            "git_publish",
            str(self.root),
            "{}",
            None,
            60,
            "write",
            {},
        )
        with (
            patch(
                "plowwhip.execution.start_provider_job",
                return_value={"status": "completed", "returncode": 0},
            ),
            patch(
                "plowwhip.execution.provider_job_output",
                return_value={"chunks": []},
            ),
            patch(
                "plowwhip.execution.workspace_snapshot",
                side_effect=HostBridgeError(
                    "snapshot rejected",
                    status=400,
                    detail="workspace not found",
                ),
            ),
        ):
            facts = perform_provider_step(step)
        self.assertFalse(facts["ok"])
        self.assertEqual(facts["failure_kind"], "transport")
        self.assertEqual(facts["failure_stage"], "snapshot_after")

    def _row_counts(self):
        connection = sqlite3.connect(str(self.db))
        try:
            return tuple(
                connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in (
                    "projects",
                    "messages",
                    "goals",
                    "tasks",
                    "host_jobs",
                    "artifacts",
                    "task_events",
                )
            )
        finally:
            connection.close()


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = Store(root / "state.db", root / "data")
        self.store.initialize()
        self.planner_patcher = patch(
            "plowwhip.lifecycle.perform_planner_step",
            side_effect=self._perform_semantic_planner_step,
        )
        self.planner_patcher.start()
        self.planner_checker_patcher = patch(
            "plowwhip.lifecycle.perform_checker_step",
            side_effect=self._perform_http_checker_step,
        )
        self.planner_checker_patcher.start()
        self.server = make_server(self.store, "127.0.0.1", 0)
        self.stop = threading.Event()
        self.cronner = threading.Thread(
            target=run_cronner, args=(self.store, self.stop, 0.01), daemon=True
        )
        self.http = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.cronner.start()
        self.http.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.cronner.join()
        self.http.join()
        self.planner_checker_patcher.stop()
        self.planner_patcher.stop()
        self.temporary.cleanup()

    def _perform_http_checker_step(self, step):
        subject = step.execution.get("subject")
        if subject in {"planner", "model_probe"}:
            acceptance_id = (
                "planner_contract"
                if subject == "planner"
                else "provider_minimal_probe"
            )
            return {
                "ok": True,
                "state": {
                    "status": "completed",
                    "returncode": 0,
                    "session_id": f"checker-{step.task_id}",
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "model": "semantic-checker-http-test-double",
                },
                "output": {
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": CHECKER_RESULT_PREFIX
                            + json.dumps(
                                {
                                    "verdict": "PASS",
                                    "acceptances": [
                                        {
                                            "acceptance_id": acceptance_id,
                                            "passed": True,
                                            "actual_evidence": f"{acceptance_id} ok",
                                            "recheck_command": "true",
                                        }
                                    ],
                                    "decision_reason": None,
                                },
                                sort_keys=True,
                            ),
                        }
                    ]
                },
            }
        return real_perform_checker_step(step)

    def _perform_semantic_planner_step(self, step):
        connection = self.store.connect()
        try:
            instruction = connection.execute(
                """
                SELECT goal.objective FROM tasks task
                JOIN goals goal ON goal.id = task.goal_id
                WHERE task.id = ?
                """,
                (step.task_id,),
            ).fetchone()["objective"]
        finally:
            connection.close()
        if instruction.strip() == "需要主人决定":
            return {
                "ok": True,
                "state": {
                    "status": "completed",
                    "returncode": 0,
                    "session_id": f"planner-{step.task_id}",
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 10,
                    "model": "semantic-planner-http-test-double",
                },
                "output": {
                    "chunks": [
                        {
                            "stream": "stdout",
                            "text": "planner double deliberately omitted structured result",
                        }
                    ]
                },
            }
        spec, _ = normalize_instruction(instruction)
        simple = spec["kind"] == "write_text"
        planned = {
            "classification": {
                "size": "simple" if simple else "medium",
                "reasons": ["bounded HTTP vertical-slice test instruction"],
                "information_sufficient": True,
                "requires_owner_choice": False,
            },
            "confidence": 0.99,
            "plan": {
                "summary": "bounded HTTP test plan",
                "alternatives": [
                    {
                        "name": "bounded",
                        "scope": "owner instruction",
                        "cost": "bounded",
                        "risk": "bounded",
                        "reversible": True,
                        "acceptance": "frozen acceptance passes",
                    }
                ],
                "selected": 0,
                "tasks": [
                    {
                        "key": "task",
                        "instruction": instruction,
                        "role_key": "deterministic" if simple else "fullstack",
                    }
                ],
            },
        }
        planned = complete_planner_payload(planned)
        return {
            "ok": True,
            "state": {
                "status": "completed",
                "returncode": 0,
                "session_id": f"planner-{step.task_id}",
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "model": "semantic-planner-http-test-double",
            },
            "output": {
                "chunks": [
                    {
                        "stream": "stdout",
                        "text": PLANNER_RESULT_PREFIX + json.dumps(planned),
                    }
                ]
            },
        }

    def test_http_intake_decision_and_automatic_completion(self):
        with urlopen(self.base + "/", timeout=2) as response:
            html = response.read().decode()
            self.assertIn("Plow Whip · 无人值守控制台", html)
            self.assertIn("SQLite WAL", html)
            self.assertEqual(html.count("data-view="), 4)
            self.assertIn("全局首页", html)
            self.assertIn("项目详情", html)
            self.assertIn("Task 详情", html)
            self.assertIn("设置与资源库", html)
            self.assertIn("不是 Artifact、Evidence 或完成依据", html)
            self.assertIn("Goal Navigator", html)
            self.assertIn("Task Detail", html)
            self.assertIn("Artifact / Evidence / Handoff", html)
            self.assertIn("Token 计量", html)
            self.assertIn("Monitor 只读", html)
            self.assertIn("Provider 探针", html)
            self.assertIn("立即唤醒", html)
            self.assertIn("处理待决定", html)
            self.assertIn("授权发布到新分支", html)
            self.assertIn("明确授权 force-with-lease", html)
            self.assertIn("Why This Decision", html)
            self.assertIn("只读刷新原因与证据", html)
            self.assertIn("没有原因与证据不能授权", html)
            self.assertIn("contextComplete", html)
            self.assertIn("publish_new_branch", html)
            self.assertIn("refresh_git_publish_context", html)
            self.assertIn("force_publish_with_lease", html)
            self.assertIn("探测 Provider codex_cli: 0token", html)
            self.assertIn("受控语义归纳", html)
            self.assertIn("/api/semantic-search", html)
            self.assertIn("0 Token 版本探活", html)
            self.assertIn("未调用模型", html)
            self.assertIn("任务泳道", html)
            self.assertEqual(html.count("data-task-lane="), 4)
            self.assertIn("HostJob / Session", html)
            self.assertIn("本地会话分段", html)
            self.assertIn('id="monitor-session-count"', html)
            self.assertIn('id="monitor-job-count"', html)
            self.assertIn('id="monitor-artifact-count"', html)
            self.assertIn("今日 Token", html)
            self.assertIn("项目 Goal", html)
            self.assertNotIn("event.target.value?openProject", html)
            self.assertIn("[hidden]{display:none!important}", html)
            self.assertIn("项目管家", html)
            self.assertIn("设置与资源库", html)
            self.assertIn("项目名称", html)
            self.assertIn("内部稳定 ID 由系统维护", html)
            self.assertIn("未变化，未重复写入历史", html)
            self.assertIn("重新授权发布当前 HEAD", html)
            self.assertNotIn('id="new-project-id"', html)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with urlopen(self.base + "/api/settings-library", timeout=2) as response:
            settings_library = json.load(response)
        self.assertEqual(len(settings_library["library"]), 12)

        status, chinese = self._post(
            "/api/actions",
            {
                "kind": "create_project",
                "display_name": "审查代码",
                "host_path": "/workspace/http-review",
                "idempotency_key": "http-chinese-create",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(chinese["result"], "created")
        self.assertRegex(chinese["project_id"], r"^project-[0-9a-f]{32}$")

        status, _ = self._post(
            "/api/actions",
            {
                "kind": "create_project",
                "project_id": "empty-web",
                "idempotency_key": "empty-web-create",
            },
        )
        self.assertEqual(status, 202)
        status, _ = self._post(
            "/api/actions",
            {
                "kind": "archive_project",
                "project_id": "empty-web",
                "confirmation": "empty-web",
                "idempotency_key": "empty-web-archive",
            },
        )
        self.assertEqual(status, 202)

        status, _ = self._post(
            "/api/messages",
            {
                "project_id": "web",
                "content": "需要主人决定",
                "idempotency_key": "web-message-1",
            },
        )
        self.assertEqual(status, 202)
        waiting = self._wait_for("web", "needs_decision")

        status, decision = self._post(
            "/api/actions",
            {
                "project_id": "web",
                "task_id": waiting["task"]["id"],
                "kind": "provide_decision",
                "instruction": "写入 web.txt: 自动完成",
                "idempotency_key": "web-decision-1",
            },
        )
        self.assertEqual(status, 202)
        done = self._wait_for("web", "done")
        event_kinds = [event["kind"] for event in reversed(done["events"])]
        self.assertIn("planner_retry_requested", event_kinds)
        self.assertNotIn("decision_applied", event_kinds)
        self.assertEqual(event_kinds[:3], ["task_created", "planner_prepared", "planner_rejected"])
        self.assertIn("executed", event_kinds)
        self.assertIn("verified", event_kinds)
        self.assertIn(done["task"]["spec_revision"], {1, 2})
        self.assertTrue({item["revision"] for item in done["artifacts"]})
        with urlopen(self.base + "/api/search?q=web.txt", timeout=2) as response:
            found = json.load(response)
        self.assertTrue(
            {"task", "message", "artifact"}.issubset(
                {item["kind"] for item in found["results"]}
            )
        )
        status, semantic_exact = self._post(
            "/api/semantic-search",
            {
                "project_id": "web",
                "query": "web.txt",
                "idempotency_key": "web-semantic-exact",
            },
        )
        self.assertEqual(status, 202)
        self.assertFalse(semantic_exact["model_queued"])
        self.assertTrue(semantic_exact["routed_only"])

        with urlopen(f"{self.base}/api/projects", timeout=2) as response:
            projects = json.load(response)
        web_project = next(
            project
            for project in projects["projects"]
            if project["project_id"] == "web"
        )
        self.assertEqual(web_project["task_id"], done["task"]["id"])
        self.assertNotIn(
            "empty-web", {project["project_id"] for project in projects["projects"]}
        )
        with urlopen(
            f"{self.base}/api/tasks/{done['task']['id']}", timeout=2
        ) as response:
            task = json.load(response)
        self.assertEqual(task["task"]["id"], done["task"]["id"])
        formal_file = task["artifacts"][0]
        with urlopen(self.base + formal_file["open_url"], timeout=2) as response:
            full_body = response.read()
            self.assertEqual(
                response.headers["X-Content-SHA256"],
                hashlib.sha256(full_body).hexdigest(),
            )
            self.assertEqual(
                response.headers["X-Artifact-Revision"],
                str(formal_file["revision"]),
            )
        with self.assertRaises(HTTPError) as error:
            urlopen(
                f"{self.base}/api/tasks/{done['task']['id']}/files/"
                + "0" * 64,
                timeout=2,
            )
        self.assertEqual(error.exception.code, 404)
        with urlopen(self.base + "/api/token", timeout=2) as response:
            usage = json.load(response)
        self.assertGreaterEqual(usage["all_history"]["total_tokens"], 20)
        with urlopen(self.base + "/api/monitor", timeout=2) as response:
            monitor = json.load(response)
        self.assertTrue(monitor["read_only"])
        self.assertEqual(monitor["summary"]["archived_projects"], 1)
        with urlopen(
            self.base + "/api/butler?project_id=web", timeout=2
        ) as response:
            butler = json.load(response)
        self.assertEqual(butler["project"]["id"], "web")
        self.assertGreaterEqual(len(butler["messages"]), 2)

        status, duplicate = self._post(
            "/api/actions",
            {
                "project_id": "web",
                "task_id": waiting["task"]["id"],
                "kind": "provide_decision",
                "instruction": "写入 ignored.txt: ignored",
                "idempotency_key": "web-decision-1",
            },
        )
        self.assertEqual(status, 202)
        self.assertEqual(decision, duplicate)

        with self.assertRaises(HTTPError) as error:
            self._post("/api/actions", {"project_id": "web"})
        self.assertEqual(error.exception.code, 400)

        request = Request(
            self.base + "/api/messages",
            data=b"{}",
            headers={"Content-Type": "application/json", "Origin": "http://evil.invalid"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            make_server(self.store, "0.0.0.0", 0)
        server = make_server(self.store, "0.0.0.0", 0, allow_non_loopback=True)
        server.server_close()

    def _post(self, path, payload):
        request = Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def _wait_for(self, project_id, status):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with urlopen(f"{self.base}/api/projects/{project_id}", timeout=2) as response:
                state = json.load(response)
            if state["task"] and state["task"]["public_status"] == status:
                return state
            time.sleep(0.01)
        self.fail(f"project {project_id} did not reach {status}")


if __name__ == "__main__":
    unittest.main()
