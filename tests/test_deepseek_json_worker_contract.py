"""Contract fixes for DeepSeek/simple-worker JSON Worker path (LIVE-DS-03/06/07/08)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from plowwhip.host_bridge import (
    _append_json_worker_assistant_stdout,
    _context_policy,
    _execution_argv,
    _find_json_worker_session_file,
    _harvest_structured_result_files,
    _structured_marker_texts,
)
from plowwhip.planner import PLANNER_RESULT_PREFIX, parse_planner_result
from plowwhip.provider import provider_agent_text


class DeepSeekJsonWorkerContractTest(unittest.TestCase):
    def test_provider_agent_text_reads_simple_worker_message_events(self):
        result = {
            "classification": {
                "size": "medium",
                "reasons": ["单一只读审查责任边界", "非确定性写入故非 simple"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [
                    {
                        "from": "simple",
                        "to": "medium",
                        "facts": ["需要跨文件语义综合"],
                    }
                ],
            },
            "confidence": 0.9,
            "plan": None,
            "blocking_reason": None,
        }
        # information_sufficient true with plan null is invalid; use blocking form
        result["classification"]["information_sufficient"] = False
        result["blocking_reason"] = "范围需主人确认"
        del result["plan"]
        line = json.dumps(
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": (
                        "analysis done\n"
                        + PLANNER_RESULT_PREFIX
                        + json.dumps(result, ensure_ascii=False)
                    ),
                },
            },
            ensure_ascii=False,
        )
        stdout = "\n".join(
            [
                json.dumps({"type": "worker.progress", "turn": 1}),
                line,
                json.dumps(
                    {
                        "type": "worker.completed",
                        "status": "completed",
                        "summary": "done",
                    }
                ),
            ]
        )
        text = provider_agent_text(stdout)
        self.assertIn(PLANNER_RESULT_PREFIX, text)
        parsed = parse_planner_result(stdout)
        self.assertEqual(parsed["classification"]["size"], "medium")
        self.assertIsNone(parsed["plan"])

    def test_parse_planner_coerces_list_responsibility_and_artifact_path(self):
        """LIVE-DS-17: responsibility list / artifact_path / thin alternatives."""
        coverage_path = "docs/runtime-audits/DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一交付物边界清晰", "一名 Worker 完整负责"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["跨文件综合超出单次确定性写入"],
            },
            "confidence": 0.95,
            "plan": {
                "summary": "readonly audit",
                "required_coverage": [coverage_path],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一方案"]},
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        # cost/risk/acceptance intentionally omitted
                        "objective_metrics": {
                            "plan_coverage": [coverage_path],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "audit_delivery",
                        "instruction": f"write {coverage_path}",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "report file exists"}
                        ],
                        "responsibility": [
                            {
                                "component": "plowwhip 核心机制审查",
                                "deliverable": coverage_path,
                            }
                        ],
                        "inputs": [
                            "plowwhip/progress_policy.py",
                            "plowwhip/lifecycle.py",
                        ],
                        "result": {
                            "type": "artifact",
                            "coverage": [coverage_path],
                            "acceptance_ids": ["report_exists"],
                            "artifact_path": coverage_path,
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists_typo"],
                        },
                        "authorization_boundary": {
                            "type": "none",
                            "allowed_actions": ["read", "write_specific_path"],
                            "target_scope": coverage_path,
                            "description": "只读审查并写入报告",
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
            task["responsibility"],
            {"component": "audit_delivery", "deliverable": coverage_path},
        )
        self.assertEqual(task["result"]["artifact"]["path"], coverage_path)
        self.assertEqual(task["checker"]["acceptance_ids"], ["report_exists"])
        self.assertEqual(
            task["inputs"],
            [{"kind": "owner_instruction", "source": "goal"}],
        )

    def test_parse_planner_coerces_path_string_inputs(self):
        """LIVE-DS-15: DeepSeek inputs as file-path strings → owner_instruction."""
        coverage_path = "docs/runtime-audits/DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一交付物边界清晰", "多文件只读分析同一责任边界"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": ["跨文件综合超出单次确定性写入"],
            },
            "confidence": 0.95,
            "plan": {
                "summary": "readonly audit",
                "required_coverage": [coverage_path],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一方案"]},
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        "cost": "low",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "report ready",
                        "objective_metrics": {
                            "coverage": [coverage_path],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "audit_and_report",
                        "instruction": "write the report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "report file exists"}
                        ],
                        "responsibility": {
                            "component_id": "readonly-audit",
                            "deliverable": coverage_path,
                        },
                        "inputs": [
                            "plowwhip/progress_policy.py",
                            "plowwhip/lifecycle.py",
                            "plowwhip/host_bridge.py",
                        ],
                        "result": {
                            "type": "artifact",
                            "coverage": [coverage_path],
                            "acceptance_ids": ["report_exists"],
                            "path": coverage_path,
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists"],
                        },
                        "authorization_boundary": {
                            "capability": "none",
                            "allowed_actions": ["read", "write_artifact"],
                            "target_scope": coverage_path,
                        },
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        )
        self.assertEqual(
            parsed["plan"]["tasks"][0]["inputs"],
            [{"kind": "owner_instruction", "source": "goal"}],
        )

    def test_parse_planner_accepts_arrow_upgrade_path_and_path_coverage(self):
        coverage_path = "docs/runtime-audits/DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"
        payload = {
            "classification": {
                "size": "medium",
                "reasons": [
                    "单一交付物边界清晰",
                    "多文件只读分析仍在同一责任边界",
                ],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple→medium"],
                "upgrade_evidence": ["跨文件综合超出单次确定性写入"],
            },
            "confidence": 0.98,
            "plan": {
                "summary": "readonly audit",
                "required_coverage": [coverage_path],
                "selected": 0,
                "selection": {
                    "mode": "objective",
                    "basis": ["唯一方案"],
                },
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        "cost": "low",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "report ready",
                        "objective_metrics": {
                            "coverage": [coverage_path],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "readonly_audit",
                        "instruction": "write the report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "report file exists"}
                        ],
                        "responsibility": {
                            "component_id": "readonly-audit",
                            "deliverable": coverage_path,
                        },
                        "inputs": {"owner_instruction": "audit"},
                        "result": {
                            "type": "artifact",
                            "coverage": [coverage_path],
                            "acceptance_ids": ["report_exists"],
                            "path": coverage_path,
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists"],
                        },
                        "authorization_boundary": {
                            "capability": "none",
                            "allowed_actions": ["read", "write_artifact"],
                            "target_scope": coverage_path,
                        },
                    }
                ],
            },
        }
        output = PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        parsed = parse_planner_result(output)
        self.assertEqual(parsed["classification"]["upgrade_path"], ["simple", "medium"])
        self.assertEqual(
            parsed["plan"]["required_coverage"],
            ["DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"],
        )
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["coverage"],
            ["DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"],
        )
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["artifact"]["path"],
            coverage_path,
        )

    def test_json_worker_execution_argv_omits_model_flag(self):
        context = {
            "provider_compaction_token_limit": 120_000,
            "rotation_max_bytes": 65_536,
            "hot_max_bytes": 16_384,
            "warm_max_bytes": 8_192,
            "max_turns": 24,
            "tool_no_progress_limit": 6,
        }
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            argv = _execution_argv(
                "json-worker",
                "simple-worker",
                project,
                "session-1",
                "prompt",
                "read",
                context,
                model="deepseek-v4-flash",
            )
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[0], "simple-worker")

    def test_context_policy_exploration_default_and_cap(self):
        policy = _context_policy({}, access="read", workspace_kind="planner")
        self.assertEqual(policy["progress_mode"], "exploration")
        self.assertGreaterEqual(int(policy["tool_no_progress_limit"]), 96)
        capped = _context_policy(
            {"tool_no_progress_limit": 300},
            access="read",
            workspace_kind="planner",
        )
        self.assertEqual(int(capped["tool_no_progress_limit"]), 256)

    def test_parse_rejects_empty_sandbox_false_insufficient_without_plan(self):
        payload = {
            "classification": {
                "size": "medium",
                "reasons": [
                    "单一职责只读审查",
                    "所需审查的源文件在当前工作区中完全不存在；工作区为空",
                ],
                "information_sufficient": False,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple→medium"],
                "upgrade_evidence": ["跨文件综合"],
            },
            "confidence": 0.99,
            "plan": None,
        }
        output = PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        from plowwhip.planner_errors import (
            SANDBOX_FALSE_INSUFFICIENT,
            PlannerContractError,
        )

        with self.assertRaises(PlannerContractError) as raised:
            parse_planner_result(output)
        self.assertIn("sandbox is empty by design", str(raised.exception))
        self.assertEqual(raised.exception.code, SANDBOX_FALSE_INSUFFICIENT)

    def test_parse_allows_alternative_coverage_superset(self):
        coverage = ["report_chapter"]
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一责任边界", "一份交付物"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [
                    {
                        "from": "simple",
                        "to": "medium",
                        "facts": ["跨文件综合"],
                    }
                ],
            },
            "confidence": 0.96,
            "plan": {
                "summary": "audit",
                "required_coverage": coverage,
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一方案"]},
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        "cost": "low",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "ok",
                        "objective_metrics": {
                            "coverage": ["totally_different_coverage_id"],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "readonly_audit",
                        "instruction": "write report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "exists"}
                        ],
                        "responsibility": {
                            "component_id": "readonly-audit",
                            "deliverable": "docs/runtime-audits/x.md",
                        },
                        "inputs": {"owner_instruction": "audit"},
                        "result": {
                            "type": "artifact",
                            "coverage": ["another_divergent_id"],
                            "acceptance_ids": ["report_exists"],
                            "path": "docs/runtime-audits/x.md",
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists"],
                        },
                        "authorization_boundary": {
                            "capability": "recoverable",
                            "allowed_actions": ["write_workspace"],
                            "target_scope": "project_workspace",
                        },
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        )
        self.assertEqual(
            parsed["plan"]["alternatives"][0]["objective_metrics"]["coverage"],
            coverage,
        )
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["coverage"],
            coverage,
        )

    def test_append_json_worker_assistant_stdout_from_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_id = "abcdef0123456789abcdef0123456789"
            session_dir = root / "simple-worker" / "deadbeef"
            session_dir.mkdir(parents=True)
            session_path = session_dir / f"{session_id}.jsonl"
            payload = {
                "classification": {
                    "size": "medium",
                    "reasons": ["a", "b"],
                    "information_sufficient": False,
                    "requires_owner_choice": False,
                    "workflow": "standard",
                    "upgrade_path": ["simple", "medium"],
                    "upgrade_evidence": ["need synthesis"],
                },
                "confidence": 0.9,
                "blocking_reason": "need owner",
            }
            marker = PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
            session_path.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "tool",
                            "content": json.dumps(
                                {
                                    "path": "planner-output.json",
                                    "content": marker,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            stdout_path = root / "stdout.segment-000001.log"
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "session.started",
                        "session_id": session_id,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "worker.completed",
                        "status": "completed",
                        "session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            found = _find_json_worker_session_file(session_id, search_roots=[root])
            self.assertEqual(found, session_path)
            _append_json_worker_assistant_stdout(
                stdout_path, session_id, search_roots=[root]
            )
            body = stdout_path.read_text(encoding="utf-8")
            self.assertIn(PLANNER_RESULT_PREFIX, body)
            parsed = parse_planner_result(body)
            self.assertEqual(parsed["blocking_reason"], "need owner")

    def test_structured_marker_ignores_prose_temp_file_claim(self):
        prose = (
            'PLOWWHIP_PLANNER_RESULT written to temp file.",'
            '"session_id":"abc"'
        )
        self.assertEqual(_structured_marker_texts(prose), [])

    def test_harvest_planner_result_file_from_isolated_workspace(self):
        coverage = "DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一交付物", "单一 fullstack 审查"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple", "medium"],
                "upgrade_evidence": [
                    {"from": "simple", "to": "medium", "facts": ["跨文件综合"]}
                ],
            },
            "confidence": 0.95,
            "plan": {
                "summary": "audit",
                "required_coverage": [coverage],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一方案"]},
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        "cost": "low",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "ok",
                        "objective_metrics": {
                            "coverage": [coverage],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "audit_delivery",
                        "instruction": "write report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {"id": "report_exists", "expected": "exists"}
                        ],
                        "responsibility": {
                            "component_id": "audit",
                            "deliverable": f"docs/runtime-audits/{coverage}",
                        },
                        "inputs": {"owner_instruction": "audit"},
                        "result": {
                            "type": "artifact",
                            "coverage": [coverage],
                            "acceptance_ids": ["report_exists"],
                            "path": f"docs/runtime-audits/{coverage}",
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": ["report_exists"],
                        },
                        "authorization_boundary": {
                            "capability": "none",
                            "allowed_actions": ["read", "write_artifact"],
                            "target_scope": f"docs/runtime-audits/{coverage}",
                        },
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "PLOWWHIP_PLANNER_RESULT.md").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            stdout_path = root / "stdout.segment-000001.log"
            stdout_path.write_text(
                json.dumps(
                    {
                        "type": "worker.completed",
                        "status": "completed",
                        "summary": "PLOWWHIP_PLANNER_RESULT written to temp file.",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                _harvest_structured_result_files(workspace, stdout_path)
            )
            body = stdout_path.read_text(encoding="utf-8")
            self.assertIn(PLANNER_RESULT_PREFIX, body)
            parsed = parse_planner_result(body)
            self.assertEqual(parsed["classification"]["size"], "medium")
            self.assertIsNotNone(parsed["plan"])

    def test_parse_coerces_chinese_acceptance_and_strips_provider_key(self):
        coverage = "DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md"
        payload = {
            "classification": {
                "size": "medium",
                "reasons": ["单一交付物", "单一角色"],
                "information_sufficient": True,
                "requires_owner_choice": False,
                "workflow": "standard",
                "upgrade_path": ["simple→medium"],
                "upgrade_evidence": ["跨文件综合"],
            },
            "confidence": 0.97,
            "plan": {
                "summary": "audit",
                "required_coverage": [coverage],
                "selected": 0,
                "selection": {"mode": "objective", "basis": ["唯一方案"]},
                "alternatives": [
                    {
                        "name": "single",
                        "scope": "one report",
                        "cost": "low",
                        "risk": "0",
                        "reversible": True,
                        "acceptance": "ok",
                        "objective_metrics": {
                            "coverage": [coverage],
                            "estimated_effort": 1,
                            "risk_level": 0,
                        },
                    }
                ],
                "tasks": [
                    {
                        "key": "audit_delivery",
                        "instruction": "write report",
                        "depends_on": [],
                        "sprint": 1,
                        "role_key": "fullstack",
                        "acceptance": [
                            {
                                "id": "chapter_总判",
                                "expected": "总判非空",
                            },
                            {
                                "id": "chapter_主线证据",
                                "expected": "主线证据非空",
                            },
                        ],
                        "responsibility": {
                            "component_id": "audit",
                            "deliverable": f"docs/runtime-audits/{coverage}",
                        },
                        "inputs": {"owner_instruction": "audit"},
                        "result": {
                            "type": "artifact",
                            "coverage": [coverage],
                            "acceptance_ids": [
                                "chapter_总判",
                                "chapter_主线证据",
                            ],
                            "path": f"docs/runtime-audits/{coverage}",
                            "format": "markdown",
                        },
                        "checker": {
                            "role": "independent_checker",
                            "acceptance_ids": [
                                "chapter_总判",
                                "chapter_主线证据",
                            ],
                        },
                        "authorization_boundary": {
                            "capability": "none",
                            "allowed_actions": ["read", "write_artifact"],
                            "target_scope": f"docs/runtime-audits/{coverage}",
                        },
                        "settings": {
                            "fullstack": {
                                "provider_order": ["deepseek"],
                                "provider_key": "deepseek",
                            }
                        },
                    }
                ],
            },
        }
        parsed = parse_planner_result(
            PLANNER_RESULT_PREFIX + json.dumps(payload, ensure_ascii=False)
        )
        acceptance = parsed["plan"]["tasks"][0]["acceptance"]
        ids = [item["id"] for item in acceptance]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)
        for acceptance_id in ids:
            self.assertRegex(acceptance_id, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
        self.assertEqual(
            parsed["plan"]["tasks"][0]["result"]["acceptance_ids"],
            ids,
        )
        self.assertNotIn(
            "provider_key",
            parsed["plan"]["tasks"][0]["settings"]["fullstack"],
        )


if __name__ == "__main__":
    unittest.main()
