import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plowwhip.cronner import tick
from plowwhip.intake import submit_action, submit_message
from plowwhip.planner import PLANNER_RESULT_PREFIX, parse_planner_result
from plowwhip.provider import provider_agent_text
from plowwhip.store import Store


def _medium_planner_payload() -> dict:
    return {
        "classification": {
            "size": "medium",
            "reasons": [
                "单一完整责任边界：有界只读审查并产出一份报告",
                "非确定性单次写入，故不是 simple",
            ],
            "information_sufficient": False,
            "requires_owner_choice": False,
            "workflow": "standard",
            "upgrade_path": ["simple", "medium"],
            "upgrade_evidence": [
                "需要跨模块语义审查，不能收敛为单次确定性写入"
            ],
        },
        "confidence": 0.96,
        "blocking_reason": "需要主人确认审查范围是否仅限本地仓库",
        "plan": None,
    }


class LiveP0OwnerDecisionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = Store(root / "state.db", root / "data")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def _seed_planner_needs_decision(self, project_id: str = "check-code") -> str:
        now = 1_700_000_000.0
        task_id = "a" * 32
        goal_id = "b" * 32
        plan_id = "c" * 32
        source_message_id = "e" * 32
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, archived_at, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                (project_id, project_id, now),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, action_json,
                    idempotency_key, created_at, processed_at
                ) VALUES (?, ?, 'owner', ?, NULL, ?, ?, ?)
                """,
                (
                    source_message_id,
                    project_id,
                    "审查无人值守主线并产出报告",
                    "seed-goal-message",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at
                ) VALUES (?, ?, ?, ?, '{}', '[]', ?)
                """,
                (
                    goal_id,
                    project_id,
                    source_message_id,
                    "审查无人值守主线并产出报告",
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO plans(id, goal_id, revision, selected, summary_json, created_at)
                VALUES (?, ?, 1, 0, '{}', ?)
                """,
                (plan_id, goal_id, now),
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, goal_id, plan_id, sprint, role_key,
                    checker_role_key, spec_json, acceptance_json, public_status,
                    phase, wait_reason, fault_code, next_action_at, next_action_kind,
                    outcome, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, 1, 'planner', 'independent_checker', ?, '[]',
                    'needs_decision', 'plan', ?, 'verification', NULL, NULL,
                    NULL, ?, ?
                )
                """,
                (
                    task_id,
                    project_id,
                    goal_id,
                    plan_id,
                    json.dumps(
                        {
                            "kind": "planner_intake",
                            "instruction": "审查无人值守主线并产出报告",
                            "normalized_candidate": {
                                "kind": "provider_task",
                                "instruction": "审查无人值守主线并产出报告",
                            },
                            "input_facts": {},
                            "planner_workspace_key": project_id,
                            "project_path": "/workspace",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "Planner output is invalid: Planner did not return a structured result",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, action_json,
                    idempotency_key, created_at, processed_at
                ) VALUES (?, ?, 'butler', ?, ?, ?, ?, ?)
                """,
                (
                    "d" * 32,
                    project_id,
                    "我现在只需要你决定一件事：Planner output is invalid",
                    json.dumps(
                        {
                            "kind": "question",
                            "task_id": task_id,
                            "spec_revision": 1,
                            "waiting": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"question:{task_id}:seed",
                    now,
                    now,
                ),
            )
        return task_id

    def test_provide_decision_retries_planner_without_rewriting_task_spec(self):
        task_id = self._seed_planner_needs_decision()
        submit_action(
            self.store,
            "check-code",
            task_id,
            "provide_decision",
            "主人决定：无人值守、质量优先、可省 Token",
            "decision-retry-planner",
        )
        actions = tick(self.store)
        self.assertEqual(actions[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                "SELECT public_status, phase, spec_json, wait_reason FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            goal = connection.execute(
                "SELECT objective FROM goals LIMIT 1"
            ).fetchone()
            event = connection.execute(
                """
                SELECT kind FROM task_events
                WHERE task_id = ? AND kind = 'planner_retry_requested'
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        spec = json.loads(task["spec_json"])
        self.assertEqual(spec["kind"], "planner_intake")
        self.assertNotEqual(task["phase"], "execute")
        self.assertEqual(task["public_status"], "pending")
        self.assertEqual(task["phase"], "plan")
        self.assertIsNone(task["wait_reason"])
        self.assertIn("无人值守、质量优先", goal["objective"])
        self.assertIsNotNone(event)

    def test_continue_after_checker_rejects_planner_retries_planner(self):
        """LIVE-DS-14: phase=plan + Checker rejected → 「继续」重试 Planner."""
        task_id = self._seed_planner_needs_decision()
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET wait_reason = ?, fault_code = 'verification'
                WHERE id = ?
                """,
                ("independent Checker rejected the planner result", task_id),
            )
        submit_action(
            self.store,
            "check-code",
            task_id,
            "provide_decision",
            "继续",
            "continue-after-checker-rejected-planner",
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                """
                SELECT public_status, phase, wait_reason, spec_json
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            event = connection.execute(
                """
                SELECT kind FROM task_events
                WHERE task_id = ? AND kind = 'planner_retry_requested'
                """,
                (task_id,),
            ).fetchone()
            rejected = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id = ? AND kind = 'decision_rejected'
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(json.loads(task["spec_json"])["kind"], "planner_intake")
        self.assertEqual(task["public_status"], "pending")
        self.assertEqual(task["phase"], "plan")
        self.assertIsNone(task["wait_reason"])
        self.assertIsNotNone(event)
        self.assertIsNone(rejected)

    def test_continue_after_checker_exhaustion_retries_checker_not_planner(self):
        from plowwhip.execution import ensure_task_sessions

        task_id = self._seed_planner_needs_decision()
        now = 1_700_000_100.0
        failed_job_id = "2" * 32
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET phase = 'provider_recovery',
                    wait_reason = ?, fault_code = 'provider',
                    checker_role_key = 'independent_checker'
                WHERE id = ?
                """,
                (
                    "independent Checker exhausted all frozen Provider candidates",
                    task_id,
                ),
            )
            ensure_task_sessions(
                connection,
                "check-code",
                task_id,
                now,
                executor_role="planner",
                checker_role="independent_checker",
                executor_provider="cursor_cli",
                checker_provider="cursor_cli",
                settings_overrides={"independent_checker": {"retry_count": 0}},
            )
            session = connection.execute(
                """
                SELECT id FROM task_sessions
                WHERE task_id = ? AND role_key = 'independent_checker'
                """,
                (task_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE session_generations
                SET status = 'archived', ended_at = ?
                WHERE task_session_id = ? AND status = 'active'
                """,
                (now, session["id"]),
            )
            connection.execute(
                """
                INSERT INTO host_jobs(
                    id, task_id, task_session_id, session_generation,
                    spec_revision, sequence, purpose, status, started_at,
                    ended_at, returncode, dispatch_json
                ) VALUES (?, ?, ?, 1, 1, 1, 'check', 'failed', ?, ?, 1, ?)
                """,
                (
                    failed_job_id,
                    task_id,
                    session["id"],
                    now,
                    now,
                    json.dumps(
                        {
                            "prompt": "check planner",
                            "check_subject": "planner",
                            "execution": {"subject": "planner"},
                            "workspace_kind": "planner",
                            "workspace_key": "check-code",
                        }
                    ),
                ),
            )
        submit_action(
            self.store,
            "check-code",
            task_id,
            "provide_decision",
            "继续",
            "decision-retry-checker",
        )
        actions = tick(self.store)
        self.assertEqual(actions[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                """
                SELECT public_status, phase, next_action_kind, wait_reason, spec_json
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            event = connection.execute(
                """
                SELECT kind, detail_json FROM task_events
                WHERE task_id = ? AND kind = 'checker_retry_requested'
                """,
                (task_id,),
            ).fetchone()
            planner_retry = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id = ? AND kind = 'planner_retry_requested'
                """,
                (task_id,),
            ).fetchone()
            retry_job = connection.execute(
                """
                SELECT id, status, purpose FROM host_jobs
                WHERE task_id = ? AND purpose = 'check' AND status = 'dispatching'
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(json.loads(task["spec_json"])["kind"], "planner_intake")
        self.assertEqual(task["public_status"], "in_progress")
        self.assertEqual(task["phase"], "check_call")
        self.assertEqual(task["next_action_kind"], "plan_check")
        self.assertIsNone(task["wait_reason"])
        self.assertIsNotNone(event)
        self.assertIsNone(planner_retry)
        self.assertIsNotNone(retry_job)
        self.assertIn("retry_host_job_id", json.loads(event["detail_json"]))

    def test_continue_after_planner_candidate_exhaustion_retries_planner(self):
        task_id = self._seed_planner_needs_decision()
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE tasks SET phase = 'provider_recovery',
                    wait_reason = ?, fault_code = 'provider'
                WHERE id = ?
                """,
                (
                    "Planner did not return a usable result; all frozen candidates are exhausted",
                    task_id,
                ),
            )
        submit_action(
            self.store,
            "check-code",
            task_id,
            "provide_decision",
            "继续",
            "continue-after-planner-exhausted",
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                """
                SELECT public_status, phase, wait_reason, spec_json
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            event = connection.execute(
                """
                SELECT kind FROM task_events
                WHERE task_id = ? AND kind = 'planner_retry_requested'
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(json.loads(task["spec_json"])["kind"], "planner_intake")
        self.assertEqual(task["public_status"], "pending")
        self.assertEqual(task["phase"], "plan")
        self.assertIsNone(task["wait_reason"])
        self.assertIsNotNone(event)

    def _seed_installed_plan_verify_needs_decision(self) -> str:
        """Execute Task with installed Plan TaskSpec stuck in verify."""
        now = 1_700_000_200.0
        task_id = "f" * 32
        goal_id = "b" * 32
        plan_id = "c" * 32
        source_message_id = "e" * 32
        spec = {
            "kind": "provider_task",
            "instruction": "write docs/runtime-audits/AUDIT_REPORT.md",
            "project_path": "/workspace",
            "workspace_change_required": True,
            "audit_delivery": True,
            "task_contract": {
                "result": {
                    "type": "artifact",
                    "coverage": ["AUDIT_REPORT.md"],
                    "acceptance_ids": ["report_exists"],
                    "artifact": {
                        "path": "docs/runtime-audits/AUDIT_REPORT.md",
                        "format": "markdown",
                    },
                },
                "acceptance": [
                    {
                        "id": "report_exists",
                        "expected": "exists",
                        "kind": "planner_acceptance",
                    }
                ],
                "checker": {
                    "role": "independent_checker",
                    "acceptance_ids": ["report_exists"],
                },
                "depends_on": [],
                "inputs": [{"kind": "owner_instruction", "source": "goal"}],
                "responsibility": {
                    "component": "audit_delivery",
                    "deliverable": "docs/runtime-audits/AUDIT_REPORT.md",
                },
                "authorization_boundary": {
                    "level": "recoverable",
                    "allowed_actions": ["write_workspace", "run_checks"],
                    "target_scope": "project_workspace",
                },
                "max_runtime_seconds": None,
            },
        }
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, archived_at, created_at)
                VALUES (?, ?, NULL, ?)
                """,
                ("check-code", "check-code", now),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, action_json,
                    idempotency_key, created_at, processed_at
                ) VALUES (?, ?, 'owner', ?, NULL, ?, ?, ?)
                """,
                (
                    source_message_id,
                    "check-code",
                    "seed audit goal",
                    "seed-installed-goal",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at
                ) VALUES (?, ?, ?, ?, '{}', '[]', ?)
                """,
                (goal_id, "check-code", source_message_id, "seed audit goal", now),
            )
            connection.execute(
                """
                INSERT INTO plans(id, goal_id, revision, selected, summary_json, created_at)
                VALUES (?, ?, 1, 1, ?, ?)
                """,
                (plan_id, goal_id, json.dumps({"tasks": []}), now),
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_id, goal_id, plan_id, sprint, role_key,
                    checker_role_key, spec_json, acceptance_json, public_status,
                    phase, wait_reason, fault_code, next_action_at, next_action_kind,
                    outcome, created_at, updated_at, spec_revision
                ) VALUES (
                    ?, ?, ?, ?, 1, 'fullstack', 'independent_checker', ?, ?,
                    'needs_decision', 'verify', ?, 'verification', NULL, NULL,
                    NULL, ?, ?, 2
                )
                """,
                (
                    task_id,
                    "check-code",
                    goal_id,
                    plan_id,
                    json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(
                        [
                            {
                                "id": "report_exists",
                                "expected": "exists",
                                "kind": "planner_acceptance",
                            }
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "independent checker contract did not prove every acceptance",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, action_json,
                    idempotency_key, created_at, processed_at
                ) VALUES (?, ?, 'butler', ?, ?, ?, ?, ?)
                """,
                (
                    "d" * 32,
                    "check-code",
                    "Checker 未证明全部验收",
                    json.dumps(
                        {
                            "kind": "question",
                            "task_id": task_id,
                            "spec_revision": 2,
                            "waiting": True,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    f"question:{task_id}:verify",
                    now,
                    now,
                ),
            )
        return task_id

    def test_queued_goal_does_not_rewrite_installed_plan_taskspec(self):
        """LIVE-DS-19: queued Goal while verify waits must not become decision_applied."""
        task_id = self._seed_installed_plan_verify_needs_decision()
        goal_text = (
            "[E2E_AUDIT_TMPL_QUEUED] 有界只读代码审查（audit_delivery）。"
            "仅写 docs/runtime-audits/DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md。"
        )
        submit_message(self.store, "check-code", goal_text, "queued-new-goal")
        # One tick: must stay blocked; Goal remains unprocessed.
        actions = tick(self.store)
        self.assertEqual(actions[0]["action"], "blocked")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                "SELECT spec_json, spec_revision, public_status, phase FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            message = connection.execute(
                """
                SELECT processed_at, action_json FROM messages
                WHERE idempotency_key = 'queued-new-goal'
                """
            ).fetchone()
            applied = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id = ? AND kind = 'decision_applied'
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        spec = json.loads(task["spec_json"])
        self.assertEqual(task["public_status"], "needs_decision")
        self.assertEqual(task["phase"], "verify")
        self.assertEqual(task["spec_revision"], 2)
        self.assertIn("task_contract", spec)
        self.assertIsNone(message["processed_at"])
        self.assertIsNone(message["action_json"])
        self.assertIsNone(applied)

    def test_verify_continue_retries_execute_without_rewriting_taskspec(self):
        """LIVE-DS-19: verify + independent checker failure + 继续 keeps TaskSpec."""
        task_id = self._seed_installed_plan_verify_needs_decision()
        before = json.loads(
            self.store.connect_readonly()
            .execute("SELECT spec_json FROM tasks WHERE id = ?", (task_id,))
            .fetchone()["spec_json"]
        )
        submit_action(
            self.store,
            "check-code",
            task_id,
            "provide_decision",
            "继续",
            "verify-continue-same-spec",
        )
        self.assertEqual(tick(self.store)[0]["action"], "provide_decision")
        connection = self.store.connect_readonly()
        try:
            task = connection.execute(
                """
                SELECT public_status, phase, next_action_kind, spec_json, spec_revision
                FROM tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            event = connection.execute(
                """
                SELECT kind, detail_json FROM task_events
                WHERE task_id = ? AND kind = 'decision_retry'
                ORDER BY created_at DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        finally:
            connection.close()
        after = json.loads(task["spec_json"])
        self.assertEqual(after, before)
        self.assertEqual(task["spec_revision"], 2)
        self.assertEqual(task["public_status"], "pending")
        self.assertEqual(task["phase"], "execute")
        self.assertEqual(task["next_action_kind"], "execute")
        self.assertIsNotNone(event)
        self.assertEqual(
            json.loads(event["detail_json"]).get("reason"),
            "verify_continue_execute_retry",
        )

    def test_owner_decision_message_does_not_create_business_task(self):
        task_id = self._seed_planner_needs_decision()
        before = self.store.connect_readonly()
        try:
            task_count = before.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
        finally:
            before.close()
        submit_message(
            self.store,
            "check-code",
            "主人决定：取消当前 Task，稍后重提 Goal",
            "owner-cancel-via-message",
        )
        actions = []
        for _ in range(5):
            batch = tick(self.store)
            if not batch:
                break
            actions.extend(batch)
        connection = self.store.connect_readonly()
        try:
            after_count = connection.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
            task = connection.execute(
                "SELECT public_status, outcome, phase FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            message = connection.execute(
                """
                SELECT processed_at, action_json FROM messages
                WHERE idempotency_key = 'owner-cancel-via-message'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(after_count, task_count)
        self.assertIsNotNone(message["processed_at"])
        self.assertEqual(json.loads(message["action_json"])["kind"], "cancel")
        self.assertEqual(task["outcome"], "cancelled")
        self.assertTrue(any(item["action"] == "cancel" for item in actions))

    def test_parse_planner_result_coerces_cursor_upgrade_evidence_strings(self):
        payload = _medium_planner_payload()
        cursor_jsonl = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "定级 medium。\n"
                                        + PLANNER_RESULT_PREFIX
                                        + json.dumps(payload, ensure_ascii=False)
                                    ),
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "result": "定级 medium。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        text = provider_agent_text(cursor_jsonl)
        self.assertIn(PLANNER_RESULT_PREFIX, text)
        parsed = parse_planner_result(cursor_jsonl)
        self.assertEqual(parsed["classification"]["size"], "medium")
        self.assertIsNone(parsed["plan"])
        self.assertEqual(
            parsed["classification"]["upgrade_evidence"][0]["from"], "simple"
        )
        self.assertEqual(
            parsed["classification"]["upgrade_evidence"][0]["to"], "medium"
        )
        self.assertTrue(parsed["classification"]["upgrade_evidence"][0]["facts"])

    def test_parse_planner_result_coerces_cursor_descriptive_inputs_dict(self):
        payload = {
            "classification": {
                "size": "medium",
                "reasons": [
                    "单一完整责任边界：有界只读审查并产出一份报告",
                    "非确定性单次写入，故不是 simple",
                ],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [
                    {
                        "from": "simple",
                        "to": "medium",
                        "facts": ["需要跨模块语义审查"],
                    }
                ],
            },
            "confidence": 0.96,
            "plan": {
                "summary": "cursor descriptive inputs",
                "alternatives": [
                    {
                        "name": "single_task",
                        "scope": "one audit report",
                        "cost": "lower",
                        "risk": "lower",
                        "reversible": True,
                        "acceptance": "report chapters pass",
                        "objective_metrics": {
                            "coverage": ["report"],
                            "estimated_effort": 2,
                            "risk_level": 1,
                        },
                    }
                ],
                "selected": 0,
                "selection": {
                    "mode": "objective",
                    "basis": ["唯一方案覆盖 required_coverage"],
                },
                "required_coverage": ["report"],
                "tasks": [
                    {
                        "key": "cursor_unattended_planner_to_done_audit",
                        "instruction": "只读审查并交付唯一 markdown 报告",
                        "role_key": "fullstack",
                        "depends_on": [],
                        "acceptance": [
                            {
                                "id": "report",
                                "expected": "交付非空审计报告",
                            }
                        ],
                        "responsibility": {
                            "component_id": "runtime-audit-docs",
                            "deliverable": "审计报告",
                        },
                        "inputs": {
                            "source_modules": ["plowwhip/planner.py"],
                            "related_tests": ["tests/test_live_p0_owner_decision.py"],
                            "write_allowlist": [
                                "docs/runtime-audits/CURSOR_UNATTENDED_PLANNER_TO_DONE.md"
                            ],
                        },
                        "result": {
                            "type": "artifact",
                            "coverage": ["report"],
                            "acceptance_ids": ["report"],
                            "path": (
                                "docs/runtime-audits/"
                                "CURSOR_UNATTENDED_PLANNER_TO_DONE.md"
                            ),
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report"],
                        },
                        "authorization_boundary": {
                            "capability": "recoverable",
                            "allowed_actions": [
                                "create_or_overwrite_single_audit_markdown"
                            ],
                            "target_scope": (
                                "docs/runtime-audits/"
                                "CURSOR_UNATTENDED_PLANNER_TO_DONE.md only"
                            ),
                        },
                        "settings": {
                            "fullstack": {"provider_order": ["cursor_cli"]}
                        },
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        )
        task = parsed["plan"]["tasks"][0]
        self.assertEqual(
            task["inputs"],
            [{"kind": "owner_instruction", "source": "goal"}],
        )

    def test_normalize_task_checker_accepts_cursor_descriptive_role(self):
        from plowwhip.planner import _normalize_task_checker

        acceptance = [{"id": "report", "expected": "non-empty"}]
        for role in (
            "independent_audit_doc_checker",
            "checker_docs_audit",
            "docs_audit_checker",
        ):
            normalized = _normalize_task_checker(
                "t1",
                {
                    "role": role,
                    "acceptance_ids": ["report"],
                },
                "independent_checker",
                acceptance,
            )
            self.assertEqual(
                normalized,
                {
                    "role_key": "independent_checker",
                    "acceptance_ids": ["report"],
                },
                msg=role,
            )


if __name__ == "__main__":
    unittest.main()
