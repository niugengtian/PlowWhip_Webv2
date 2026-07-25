import re
import tempfile
import time
import unittest
from pathlib import Path

from plowwhip.lifecycle_state import write_task_fields
from plowwhip.store import Store


LIFECYCLE_SQL = re.compile(
    r"(?is)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:goals|tasks)\b"
)


class LifecycleOwnershipTest(unittest.TestCase):
    def test_only_lifecycle_modules_contain_goal_or_task_write_sql(self):
        package = Path(__file__).resolve().parents[1] / "plowwhip"
        allowed = {"lifecycle.py", "lifecycle_state.py"}
        violations = {
            path.name: [match.group(0) for match in LIFECYCLE_SQL.finditer(text)]
            for path in package.glob("*.py")
            if path.name not in allowed
            if (text := path.read_text(encoding="utf-8"))
            if LIFECYCLE_SQL.search(text)
        }
        self.assertEqual(violations, {})

    def test_only_lifecycle_entry_and_schema_migration_open_write_scope(self):
        package = Path(__file__).resolve().parents[1] / "plowwhip"
        allowed = {"lifecycle.py", "lifecycle_state.py", "store.py"}
        violations = [
            path.name
            for path in package.glob("*.py")
            if path.name not in allowed
            if "lifecycle_write_scope" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(violations, [])

    def test_task_lifecycle_writer_rejects_calls_outside_advance_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "state.db", root / "data")
            store.initialize()
            now = time.time()
            with store.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO projects(id, display_name, created_at)
                    VALUES ('ownership', 'ownership', ?)
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, project_id, role, content, idempotency_key, created_at
                    ) VALUES ('ownership-message', 'ownership', 'owner', 'x',
                              'ownership-message', ?)
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO goals(
                        id, project_id, source_message_id, objective,
                        boundary_json, acceptance_json, created_at
                    ) VALUES ('ownership-goal', 'ownership', 'ownership-message',
                              'x', '{}', '[]', ?)
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id, project_id, goal_id, spec_json, acceptance_json,
                        public_status, phase, created_at, updated_at
                    ) VALUES ('ownership-task', 'ownership', 'ownership-goal',
                              '{}', '[]', 'pending', 'plan', ?, ?)
                    """,
                    (now, now),
                )
            with store.transaction() as connection:
                with self.assertRaisesRegex(
                    RuntimeError, "only be written by advance_project"
                ):
                    write_task_fields(
                        connection,
                        "ownership-task",
                        {"public_status": "done"},
                    )
            connection = store.connect_readonly()
            try:
                status = connection.execute(
                    "SELECT public_status FROM tasks WHERE id = 'ownership-task'"
                ).fetchone()["public_status"]
            finally:
                connection.close()
            self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
