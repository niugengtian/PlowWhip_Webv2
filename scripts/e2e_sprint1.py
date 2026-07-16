from __future__ import annotations

import json
import sys
import tempfile
import uuid
from pathlib import Path

import httpx


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="plow-whip-web-s1-") as directory:
        project = Path(directory)
        payload = {
            "title": "Sprint 1 HTTP E2E",
            "objective": "Create an artifact and finish only after deterministic verification",
            "project_path": str(project),
            "command": {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('evidence.txt').write_text('verified', encoding='utf-8')",
                ]
            },
            "verification": [
                {"kind": "exit_code", "expected": 0},
                {"kind": "file_exists", "path": "evidence.txt"},
                {"kind": "file_contains", "path": "evidence.txt", "contains": "verified"},
            ],
        }
        with httpx.Client(base_url="http://127.0.0.1:8742", timeout=10) as client:
            created_response = client.post(
                "/api/tasks",
                headers={"Idempotency-Key": f"e2e-create-{uuid.uuid4()}"},
                json=payload,
            )
            created_response.raise_for_status()
            created = created_response.json()
            driven_response = client.post(
                f"/api/tasks/{created['id']}/drive",
                headers={"Idempotency-Key": f"e2e-drive-{uuid.uuid4()}"},
                json={"expected_revision": created["revision"]},
            )
            driven_response.raise_for_status()
            completed = driven_response.json()
            events_response = client.get(f"/api/tasks/{created['id']}/events")
            events_response.raise_for_status()
            events = events_response.json()

        assert completed["status"] == "completed"
        assert completed["tokens_used"] == 0
        assert project.joinpath("evidence.txt").read_text(encoding="utf-8") == "verified"
        assert [event["event_type"] for event in events][-1] == "task.completed"
        print(
            json.dumps(
                {
                    "task_id": completed["id"],
                    "status": completed["status"],
                    "revision": completed["revision"],
                    "events": len(events),
                    "tokens_used": completed["tokens_used"],
                    "evidence_hash": completed["last_evidence_hash"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
