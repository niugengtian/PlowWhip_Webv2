import json
import tempfile
import unittest
from pathlib import Path

from plowwhip.butler import search
from plowwhip.host_bridge import (
    SUPPORTED_EXECUTABLES,
    _execution_argv,
    _resolve_executable,
    _version_argv,
)
from plowwhip.intake import (
    extract_local_script_spec,
    set_project_setting,
    submit_message,
)
from plowwhip.cronner import tick
from plowwhip.local_script_worker import run_local_script
from plowwhip.provider_policy import (
    first_eligible_provider,
    provider_order_for_role,
)
from plowwhip.script_library import (
    extract_script_summary,
    list_project_scripts,
    search_script_library,
    suggested_item_key,
)
from plowwhip.store import DEFAULT_SETTINGS, Store


class ScriptModuleProviderTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state.db", self.root / "data")
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_provider_order_prefers_cursor_then_deepseek(self):
        order = DEFAULT_SETTINGS["provider_order"]
        self.assertEqual(order["planner"], ["cursor_cli", "deepseek"])
        self.assertEqual(order["fullstack"], ["cursor_cli", "deepseek"])
        self.assertNotIn("codex_cli", order["planner"])
        self.assertEqual(order["local_script_runner"], ["local_script"])

    def test_disabled_providers_are_skipped_in_order(self):
        values = {
            "provider_order": {
                "fullstack": ["cursor_cli", "deepseek", "kimi"],
            },
            "provider_disabled": {"fullstack": ["cursor_cli"]},
        }
        self.assertEqual(
            provider_order_for_role(values, "fullstack"),
            ["deepseek", "kimi"],
        )
        self.assertEqual(
            first_eligible_provider(values, "fullstack"),
            "deepseek",
        )

    def test_project_provider_disabled_setting_is_accepted(self):
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, created_at)
                VALUES ('demo', 'demo', 1)
                """
            )
        message_id = set_project_setting(
            self.store,
            "demo",
            "provider_disabled",
            {"fullstack": ["codex_cli"]},
            "disable-codex",
        )
        self.assertTrue(message_id)

    def test_script_summary_and_search(self):
        body = '''"""
summary: normalize CSV rows into JSON records
"""

def transform(rows):
    return rows

def main():
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, created_at)
                VALUES ('lib', 'lib', 1)
                """
            )
            path = (
                self.store.data_root
                / "projects"
                / "lib"
                / "library"
                / "scripts"
                / "normalize_csv.revision-000001.py"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            connection.execute(
                """
                INSERT INTO library_items(
                    id, scope, project_id, kind, item_key, revision,
                    path, sha256, created_at
                ) VALUES (?, 'project', 'lib', 'script', 'normalize_csv', 1, ?, ?, 1)
                """,
                (
                    "a" * 32,
                    self.store.relative_data_path(path),
                    __import__("hashlib").sha256(body.encode()).hexdigest(),
                ),
            )
            hits = search_script_library(
                connection, self.store, "normalize", project_id="lib"
            )
            listed = list_project_scripts(connection, self.store, "lib")
        self.assertEqual(extract_script_summary(body), "normalize CSV rows into JSON records")
        self.assertEqual(hits[0]["item_key"], "normalize_csv")
        self.assertEqual(listed[0]["item_key"], "normalize_csv")
        result = search(self.store.db_path, self.store.data_root, "normalize")
        self.assertTrue(any(item["kind"] == "script" for item in result["results"]))

    def test_script_goal_records_library_hit_or_miss_before_planning(self):
        body = '"""summary: normalize records"""\n'
        with self.store.transaction() as connection:
            for project_id in ("script-hit", "script-miss"):
                connection.execute(
                    "INSERT INTO projects(id, display_name, created_at) VALUES (?, ?, 1)",
                    (project_id, project_id),
                )
            path = self.store.data_root / "scripts" / "normalize.revision-000001.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            connection.execute(
                """
                INSERT INTO library_items(
                    id, scope, project_id, kind, item_key, revision, path, sha256, created_at
                ) VALUES ('b123456789012345678901234567890', 'project', 'script-hit',
                          'script', 'normalize', 1, ?, 'sha', 1)
                """,
                (self.store.relative_data_path(path),),
            )
        submit_message(
            self.store,
            "script-hit",
            "run local script library:normalize",
            "script-hit-message",
        )
        submit_message(
            self.store,
            "script-miss",
            "run local script library:missing",
            "script-miss-message",
        )
        tick(self.store, limit=2)
        connection = self.store.connect_readonly()
        try:
            rows = connection.execute(
                """
                SELECT project_id, detail_json FROM task_events
                WHERE kind = 'script_library_search' ORDER BY project_id
                """
            ).fetchall()
        finally:
            connection.close()
        results = {row["project_id"]: json.loads(row["detail_json"]) for row in rows}
        self.assertEqual(results["script-hit"]["hit_count"], 1)
        self.assertEqual(results["script-hit"]["hits"][0]["item_key"], "normalize")
        self.assertEqual(results["script-miss"]["hit_count"], 0)

    def test_local_script_instruction_and_worker(self):
        spec = extract_local_script_spec(
            "执行本地脚本 tools/hello.py -- --name world"
        )
        self.assertEqual(spec["kind"], "local_script")
        self.assertEqual(spec["script"]["path"], "tools/hello.py")
        self.assertEqual(spec["argv"], ["--name", "world"])
        library = extract_local_script_spec("run local script library:tool@2")
        self.assertEqual(library["script"]["item_key"], "tool")
        self.assertEqual(library["script"]["revision"], 2)

        project = self.root / "workspace"
        script = project / "tools" / "hello.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "import sys\nprint('ok', sys.argv[1])\n",
            encoding="utf-8",
        )
        result = run_local_script(
            project,
            {
                "kind": "local_script",
                "script": {"origin": "workspace", "path": "tools/hello.py"},
                "argv": ["world"],
            },
        )
        self.assertTrue(result["ok"])
        self.assertIn("ok world", result["stdout"])

    def test_host_bridge_local_script_adapter(self):
        self.assertIn("local-script", SUPPORTED_EXECUTABLES)
        executable = _resolve_executable("local-script", "local-script")
        self.assertTrue(executable)
        version = _version_argv("local-script", executable)
        self.assertTrue(any(str(part).endswith("local_script_worker.py") for part in version))
        self.assertEqual(version[-1], "--version")
        argv = _execution_argv(
            "local-script",
            executable,
            self.root,
            None,
            json.dumps({"kind": "local_script", "script": {"origin": "workspace", "path": "a.py"}, "argv": []}),
            "write",
            {
                "hot_max_bytes": 4096,
                "warm_max_bytes": 2048,
                "rotation_max_bytes": 16384,
                "provider_compaction_token_limit": 120000,
                "max_turns": 24,
                "tool_no_progress_limit": 6,
            },
        )
        self.assertTrue(any(str(part).endswith("local_script_worker.py") for part in argv))

    def test_suggested_item_key(self):
        self.assertEqual(suggested_item_key("modules/normalize_csv.py"), "normalize_csv")
        self.assertEqual(suggested_item_key("9bad.py"), "script-9bad")


if __name__ == "__main__":
    unittest.main()
