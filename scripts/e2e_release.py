from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        runtime = Path(directory) / "runtime"
        projects = [Path(directory) / "web3", Path(directory) / "it"]
        for project in projects:
            project.mkdir()
        port = free_port()
        process = subprocess.Popen(
            [sys.executable, "-m", "plow_whip_web", "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(runtime)],
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            wait_until_ready(base, process)
            with httpx.Client(base_url=base, timeout=10) as client:
                homepage = client.get("/")
                assert homepage.status_code == 200 and "plow-whip Web v2" in homepage.text
                task_ids = []
                for index, (path, role) in enumerate(zip(projects, ("web3", "fullstack"), strict=True)):
                    project = client.post("/api/projects", json={"name": role, "path": str(path)}).raise_for_status().json()
                    task = client.post(
                        "/api/tasks", headers={"Idempotency-Key": f"release-create-{index}"},
                        json={
                            "title": f"{role} release example", "objective": "produce verified evidence",
                            "project_id": project["id"], "role": role, "quality_profile": "strict",
                            "command": {"argv": [sys.executable, "-c", "from pathlib import Path; Path('evidence').write_text('verified')"]},
                            "verification": [{"kind": "file_contains", "path": "evidence", "contains": "verified"}],
                        },
                    ).raise_for_status().json()
                    task_ids.append(task["id"])
                tick = client.post("/api/scheduler/tick").raise_for_status().json()
                completed = [client.get(f"/api/tasks/{task_id}").raise_for_status().json() for task_id in task_ids]
                assert tick["model_tokens"] == 0
                assert all(task["status"] == "completed" and task["last_evidence_hash"] for task in completed)
                output = {
                    "homepage": "served",
                    "projects": 2,
                    "tasks": [{"id": task["id"], "status": task["status"], "evidence": task["last_evidence_hash"]} for task in completed],
                    "control_tokens": tick["model_tokens"],
                    "audit_entries": len(client.get("/api/audit").raise_for_status().json()),
                }
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until_ready(base: str, process: subprocess.Popen[str]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"server exited\n{stdout}\n{stderr}")
        try:
            if httpx.get(f"{base}/health", timeout=.2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.05)
    raise TimeoutError("server did not become ready")


if __name__ == "__main__":
    main()
