from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
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
        "captured_at": datetime.now(timezone.utc).isoformat(),
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
    execution_context: dict[str, Any] | None = None,
    inherited_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    execution_context = dict(execution_context or {})
    inherited_by_path = {
        str(item["relative_path"]): item for item in (inherited_artifacts or [])
    }
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
        inherited = inherited_by_path.get(relative_path)
        inherited_verified = bool(
            current.get("exists")
            and current.get("sha256")
            and inherited
            and inherited.get("sha256") == current.get("sha256")
            and inherited.get("task_id") == task.id
            and inherited.get("session_generation")
            == execution_context.get("session_generation")
        )
        artifacts.append(
            {
                "relative_path": relative_path,
                "before": _artifact_hash_record(before),
                "after": _artifact_hash_record(current),
                "produced_by_run": produced,
                "provenance": "current_run" if produced else (
                    "same_task_session_generation" if inherited_verified else "unverified"
                ),
                "inherited_from": inherited if inherited_verified else None,
            }
        )

    task_acceptance_ids = [
        f"acceptance-{index + 1:03d}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
        for index, text in enumerate(task.spec.get("acceptance") or [])
    ]
    commands = []
    for index, (spec, check) in enumerate(
        zip(task.verification, verification.checks, strict=False)
    ):
        command_gate = spec.get("kind") == "command"
        gate_argv = (
            list(check.get("argv") or [])
            if command_gate
            else list(
                execution_context.get("argv")
                or task.command.get("argv")
                or []
            )
        )
        gate_cwd = (
            str(check.get("cwd") or "")
            if command_gate
            else str(execution_context.get("cwd") or task.project_path)
        )
        gate_exit_code = (
            int(check.get("actual", 124))
            if command_gate
            else execution.returncode
        )
        artifact_paths = (
            [str(spec["path"])] if spec.get("path") else list(task.spec["artifacts"])
        )
        artifact_evidence = [
            {
                "path": item["relative_path"],
                "sha256": item["after"].get("sha256"),
                "provenance": item["provenance"],
            }
            for item in artifacts
            if item["relative_path"] in artifact_paths
        ]
        commands.append(
            {
                "acceptance_id": (
                    check.get("acceptance_id")
                    or spec.get("acceptance_id")
                    or (
                        task_acceptance_ids[min(index, len(task_acceptance_ids) - 1)]
                        if task_acceptance_ids else f"gate-{index + 1:03d}"
                    )
                ),
                "spec": spec,
                "argv": gate_argv,
                "cwd": gate_cwd,
                "started_at": (
                    check.get("started_at")
                    if command_gate else execution_context.get("started_at")
                ) or baseline.get("captured_at"),
                "finished_at": (
                    check.get("finished_at")
                    if command_gate else execution_context.get("finished_at")
                ) or after.get("captured_at"),
                "exit_code": gate_exit_code,
                "stdout_sha256": check.get("stdout_sha256"),
                "stderr_sha256": check.get("stderr_sha256"),
                "stdout_bytes": check.get("stdout_bytes"),
                "stderr_bytes": check.get("stderr_bytes"),
                "host_job_id": execution_context.get("host_job_id"),
                "run_id": run_id,
                "session": {
                    "external_session_id": execution_context.get("external_session_id"),
                    "session_generation": execution_context.get("session_generation"),
                    "fencing_token": execution_context.get("fencing_token"),
                },
                "summary": (
                    "gate passed" if check.get("passed") else "gate failed"
                ),
                "artifact_evidence": artifact_evidence,
                "check": check,
            }
        )
    artifact_contract_passed = all(
        item["provenance"] in {"current_run", "same_task_session_generation"}
        for item in artifacts
    )
    required_acceptance_ids = task_acceptance_ids
    recorded_acceptance_ids = {
        str(item.get("acceptance_id")) for item in commands if item.get("acceptance_id")
    }
    missing_acceptance_ids = [
        item for item in required_acceptance_ids if item not in recorded_acceptance_ids
    ]
    evidence_fields_complete = bool(commands) and all(
        item.get("acceptance_id")
        and item.get("argv")
        and item.get("cwd")
        and item.get("started_at")
        and item.get("finished_at")
        and isinstance(item.get("exit_code"), int)
        and item.get("run_id") == run_id
        for item in commands
    )
    reason_codes = list(verification.reason_codes)
    if not artifact_contract_passed:
        reason_codes.append("ARTIFACT_PROVENANCE_UNVERIFIED")
    if missing_acceptance_ids:
        reason_codes.append("REQUIRED_ACCEPTANCE_MISSING")
    if not evidence_fields_complete:
        reason_codes.append("COMMAND_EVIDENCE_INCOMPLETE")
    browser_required = any(
        marker in str(value).lower()
        for value in [
            *(task.spec.get("acceptance") or []),
            *(task.spec.get("constraints") or []),
        ]
        for marker in ("browser", "浏览器", "e2e")
    )
    browser_checks = [
        item for item in verification.checks
        if item.get("kind") == "browser_evidence" and item.get("passed") is True
    ]
    if browser_required and not browser_checks:
        reason_codes.append("BROWSER_GATE_MISSING")
    reason_codes = list(dict.fromkeys(reason_codes))
    failed_acceptance_ids = list(dict.fromkeys([
        *verification.failed_acceptance_ids,
        *missing_acceptance_ids,
    ]))
    passed = (
        verification.verdict == "PASS"
        and artifact_contract_passed
        and not missing_acceptance_ids
        and evidence_fields_complete
        and not reason_codes
    )
    verdict = "PASS" if passed else "CHANGES_REQUIRED"
    summary = verification.summary
    if not passed:
        stale = [
            item["relative_path"]
            for item in artifacts
            if item["provenance"] == "unverified"
        ]
        detail = f"; unverified artifacts: {', '.join(stale)}" if stale else ""
        summary = (
            f"artifact contract failed: not produced by this run: {', '.join(stale)}"
            if reason_codes == ["ARTIFACT_PROVENANCE_UNVERIFIED"] and stale
            else f"verification failed (CHANGES_REQUIRED): {', '.join(reason_codes)}{detail}"
        )

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
        "version": 2,
        "task_id": task.id,
        "attempt_id": attempt_id,
        "call_id": call_id,
        "run_id": run_id,
        "spec_revision": task.spec_revision,
        "task_revision": task_revision,
        "environment": environment,
        "environment_hash": environment_hash,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "failed_acceptance_ids": failed_acceptance_ids,
        "required_acceptance_ids": required_acceptance_ids,
        "browser_evidence": browser_checks,
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
            "verdict": verification.verdict,
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
                        "provenance": item["provenance"],
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
