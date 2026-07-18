from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import TaskRecord
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.runtime.verification import VerificationResult


def snapshot_environment(
    project_path: Path, artifact_paths: list[str]
) -> dict[str, Any]:
    root = project_path.resolve()
    artifacts = []
    for relative_path in dict.fromkeys(artifact_paths):
        target = _safe_path(root, relative_path)
        artifacts.append(_artifact_snapshot(relative_path, target))
    return {
        "project_path": str(root),
        "artifacts": artifacts,
        "git": _git_snapshot(root),
    }


def build_evidence_manifest(
    *,
    task: TaskRecord,
    attempt_id: str,
    run_id: str,
    call_id: str,
    task_revision: int,
    baseline: dict[str, Any],
    after: dict[str, Any],
    execution: ExecutionResult,
    verification: VerificationResult,
) -> dict[str, Any]:
    before_by_path = {
        str(item["relative_path"]): item for item in baseline.get("artifacts", [])
    }
    after_by_path = {
        str(item["relative_path"]): item for item in after.get("artifacts", [])
    }
    artifacts = []
    for relative_path in task.spec["artifacts"]:
        before = before_by_path.get(relative_path, _missing_artifact(relative_path))
        current = after_by_path.get(relative_path, _missing_artifact(relative_path))
        produced = bool(current.get("exists")) and (
            not before.get("exists")
            or (
                before.get("sha256") is not None
                and current.get("sha256") is not None
                and before["sha256"] != current["sha256"]
            )
        )
        artifacts.append(
            {
                "relative_path": relative_path,
                "before": _artifact_hash_record(before),
                "after": _artifact_hash_record(current),
                "produced_by_run": produced,
            }
        )

    commands = []
    for spec, check in zip(task.verification, verification.checks, strict=False):
        commands.append(
            {
                "spec": spec,
                "command": (
                    list(task.command.get("argv") or [])
                    if spec["kind"] == "exit_code"
                    else spec
                ),
                "exit_code": 0 if check.get("passed") else 1,
                "check": check,
            }
        )
    artifact_contract_passed = all(item["produced_by_run"] for item in artifacts)
    passed = verification.passed and artifact_contract_passed
    summary = verification.summary
    if verification.passed and not artifact_contract_passed:
        stale = [
            item["relative_path"]
            for item in artifacts
            if not item["produced_by_run"]
        ]
        summary = f"artifact contract failed: not produced by this run: {', '.join(stale)}"

    environment = {
        **baseline.get("environment", {}),
        **after.get("environment", {}),
        "project_path": str(after.get("project_path") or baseline.get("project_path") or ""),
        "provider": task.provider,
        "worker_id": task.worker_id,
        "spec_revision": task.spec_revision,
    }
    environment_hash = _digest(environment)
    manifest = {
        "version": 1,
        "task_id": task.id,
        "attempt_id": attempt_id,
        "call_id": call_id,
        "run_id": run_id,
        "spec_revision": task.spec_revision,
        "task_revision": task_revision,
        "environment": environment,
        "environment_hash": environment_hash,
        "verification_commands": commands,
        "artifacts": artifacts,
        "test_report": {
            "passed": verification.passed,
            "checks_total": len(verification.checks),
            "checks_passed": sum(
                1 for check in verification.checks if check.get("passed")
            ),
            "execution_exit_code": execution.returncode,
            "verification_evidence_hash": verification.evidence_hash,
        },
        "git_diff_summary": {
            "before": baseline.get("git", {}),
            "after": after.get("git", {}),
        },
        "artifact_contract_passed": artifact_contract_passed,
        "passed": passed,
        "summary": summary,
        "failure_fingerprint": _digest(
            {
                "verification": [_stable_check(check) for check in verification.checks],
                "artifact_contract": [
                    {
                        "relative_path": item["relative_path"],
                        "produced_by_run": item["produced_by_run"],
                    }
                    for item in artifacts
                ],
            }
        ),
    }
    manifest["manifest_hash"] = _digest(manifest)
    return manifest


def manifest_hash(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    claimed = str(payload.pop("manifest_hash", ""))
    calculated = _digest(payload)
    if claimed != calculated:
        raise ValueError("EvidenceManifest checksum mismatch")
    if payload.get("environment_hash") != _digest(payload.get("environment", {})):
        raise ValueError("EvidenceManifest environment checksum mismatch")
    return claimed


def _artifact_snapshot(relative_path: str, target: Path) -> dict[str, Any]:
    if not target.is_file():
        return _missing_artifact(relative_path)
    content = target.read_bytes()
    return {
        "relative_path": relative_path,
        "exists": True,
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }


def _artifact_hash_record(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "exists": bool(artifact.get("exists")),
        "sha256": artifact.get("sha256"),
        "bytes": artifact.get("bytes"),
    }


def _missing_artifact(relative_path: str) -> dict[str, Any]:
    return {
        "relative_path": relative_path,
        "exists": False,
        "sha256": None,
        "bytes": None,
    }


def _git_snapshot(project_path: Path) -> dict[str, Any]:
    def run(*argv: str) -> tuple[int, str]:
        completed = subprocess.run(
            ["git", "-C", str(project_path), *argv],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return completed.returncode, completed.stdout.strip()

    inside_code, inside = run("rev-parse", "--is-inside-work-tree")
    if inside_code != 0 or inside != "true":
        return {"available": False, "reason": "not_a_git_worktree"}
    head_code, head = run("rev-parse", "HEAD")
    status_code, status = run("status", "--short")
    stat_code, stat = run("diff", "--stat", "--no-ext-diff")
    return {
        "available": head_code == status_code == stat_code == 0,
        "head": head or None,
        "status": status,
        "diff_stat": stat,
    }


def _safe_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute():
        raise ValueError("artifact path must be relative")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("artifact path escapes project root")
    return target


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_check(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_check(item)
            for key, item in value.items()
            if key not in {"modified_at", "modified_at_ns", "duration_ms"}
        }
    if isinstance(value, list):
        return [_stable_check(item) for item in value]
    return value
