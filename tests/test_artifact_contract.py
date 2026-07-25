import json
import tempfile
import time
import unittest
from pathlib import Path

from plowwhip.artifact_contract import (
    register_artifact,
    task_data_scope,
    verified_artifact,
)
from plowwhip.continuity import _dependency_results
from plowwhip.store import Store


class ArtifactContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = Store(root / "state.db", root / "data")
        self.store.initialize()
        self.now = time.time()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(id, display_name, created_at)
                VALUES ('artifact-project', 'artifact-project', ?)
                """,
                (self.now,),
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, project_id, role, content, idempotency_key, created_at
                ) VALUES ('artifact-message', 'artifact-project', 'owner',
                          'artifact contract', 'artifact-message', ?)
                """,
                (self.now,),
            )
            connection.execute(
                """
                INSERT INTO goals(
                    id, project_id, source_message_id, objective,
                    boundary_json, acceptance_json, created_at
                ) VALUES ('artifact-goal', 'artifact-project', 'artifact-message',
                          'artifact contract', '{}', '[]', ?)
                """,
                (self.now,),
            )
            for index in range(4):
                task_id = f"artifact-task-{index}"
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id, project_id, goal_id, spec_json, acceptance_json,
                        public_status, phase, outcome, created_at, updated_at
                    ) VALUES (?, 'artifact-project', 'artifact-goal', '{}', '[]',
                              'done', 'done', 'done', ?, ?)
                    """,
                    (task_id, self.now, self.now),
                )
            for index in range(3):
                connection.execute(
                    """
                    INSERT INTO task_dependencies(task_id, depends_on_task_id)
                    VALUES ('artifact-task-3', ?)
                    """,
                    (f"artifact-task-{index}",),
                )
                self._record_dependency(connection, index)

    def tearDown(self):
        self.temporary.cleanup()

    def _record_dependency(self, connection, index):
        task_id = f"artifact-task-{index}"
        root = (
            self.store.data_root
            / "projects"
            / "artifact-project"
            / "tasks"
            / task_id
        )
        output_path = root / "output" / "report.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_body = (
            b"A" * (1024 * 1024 + 127)
            if index == 0
            else f"dependency-{index}".encode()
        )
        output_path.write_bytes(output_body)
        register_artifact(
            self.store,
            connection,
            project_id="artifact-project",
            task_id=task_id,
            kind="output",
            path=output_path,
            stored_path=self.store.relative_data_path(output_path),
            acceptance_id="provider_report",
            revision=1,
            scope=task_data_scope([f"coverage-{index}"]),
            source_task_id=task_id,
            created_at=self.now,
        )
        evidence_path = root / "evidence" / "checker.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "checker_verdict": "PASS",
                    "acceptances": [
                        {
                            "acceptance_id": f"acceptance-{index}",
                            "passed": True,
                        }
                    ],
                }
            )
        )
        register_artifact(
            self.store,
            connection,
            project_id="artifact-project",
            task_id=task_id,
            kind="evidence",
            path=evidence_path,
            stored_path=self.store.relative_data_path(evidence_path),
            acceptance_id=None,
            revision=1,
            scope=task_data_scope([f"coverage-{index}"]),
            source_task_id=task_id,
            created_at=self.now,
        )

    def test_all_dependencies_and_large_artifact_are_consumed_by_manifest(self):
        connection = self.store.connect_readonly()
        try:
            results = _dependency_results(
                self.store, connection, "artifact-task-3"
            )
        finally:
            connection.close()
        self.assertEqual(
            [result["task_id"] for result in results],
            ["artifact-task-0", "artifact-task-1", "artifact-task-2"],
        )
        first = results[0]["artifacts"][0]
        self.assertGreater(first["bytes"], 1024 * 1024)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["source_task_id"], "artifact-task-0")
        self.assertEqual(first["scope"]["coverage"], ["coverage-0"])

    def test_path_hash_revision_and_source_tampering_fail_closed(self):
        connection = self.store.connect_readonly()
        try:
            row = connection.execute(
                """
                SELECT kind, path, sha256, bytes, acceptance_id, revision,
                       scope_json, source_task_id
                FROM artifacts
                WHERE task_id = 'artifact-task-0' AND kind = 'output'
                """
            ).fetchone()
            with self.assertRaisesRegex(ValueError, "revision"):
                verified_artifact(
                    self.store,
                    row,
                    expected_source_task_id="artifact-task-0",
                    expected_revision=2,
                )
        finally:
            connection.close()
        path = self.store.resolve_data_path(row["path"])
        path.write_bytes(path.read_bytes() + b"tampered")
        connection = self.store.connect_readonly()
        try:
            with self.assertRaisesRegex(ValueError, "byte count"):
                _dependency_results(
                    self.store, connection, "artifact-task-3"
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
