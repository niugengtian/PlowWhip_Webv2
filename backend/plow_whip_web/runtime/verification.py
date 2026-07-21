from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plow_whip_web.providers.generic_command import ExecutionResult


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]]
    evidence_hash: str
    summary: str
    verdict: str = "PASS"
    reason_codes: list[str] = field(default_factory=list)
    failed_acceptance_ids: list[str] = field(default_factory=list)


class VerificationEngine:
    def verify(
        self,
        project_path: Path,
        execution: ExecutionResult,
        specs: list[dict[str, Any]],
        *,
        acceptance: list[str] | None = None,
        require_structured_verdict: bool = False,
    ) -> VerificationResult:
        acceptance_ids = _acceptance_ids(acceptance or [])
        results: list[dict[str, Any]] = []
        for index, spec in enumerate(specs):
            kind = spec["kind"]
            acceptance_id = str(
                spec.get("acceptance_id")
                or (acceptance_ids[min(index, len(acceptance_ids) - 1)] if acceptance_ids else f"gate-{index + 1:03d}")
            )
            if kind == "exit_code":
                expected = int(spec.get("expected", 0))
                actual = execution.returncode
                results.append(
                    {
                        "acceptance_id": acceptance_id,
                        "kind": kind,
                        "passed": actual == expected,
                        "expected": expected,
                        "actual": actual,
                    }
                )
                continue
            if kind == "command":
                argv = [str(item) for item in (spec.get("argv") or [])]
                cwd = _safe_project_directory(
                    project_path, str(spec.get("cwd") or "")
                )
                started_at = datetime.now(timezone.utc).isoformat()
                timed_out = False
                try:
                    completed = subprocess.run(
                        argv,
                        cwd=cwd,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=max(1, int(spec.get("timeout_seconds") or 600)),
                        check=False,
                    )
                    exit_code = int(completed.returncode)
                    stdout = _as_bytes(completed.stdout)
                    stderr = _as_bytes(completed.stderr)
                except subprocess.TimeoutExpired as error:
                    timed_out = True
                    exit_code = 124
                    stdout = _as_bytes(error.stdout)
                    stderr = _as_bytes(error.stderr)
                finished_at = datetime.now(timezone.utc).isoformat()
                expected = int(spec.get("expected", 0))
                results.append(
                    {
                        "acceptance_id": acceptance_id,
                        "kind": kind,
                        "passed": exit_code == expected,
                        "expected": expected,
                        "actual": exit_code,
                        "argv": argv,
                        "cwd": str(cwd),
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "timed_out": timed_out,
                        "stdout_bytes": len(stdout),
                        "stderr_bytes": len(stderr),
                        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    }
                )
                continue
            target = _safe_project_path(project_path, spec["path"])
            artifact = _artifact_evidence(target)
            if kind == "file_exists":
                results.append({
                    "acceptance_id": acceptance_id,
                    "kind": kind, "path": spec["path"],
                    "passed": artifact is not None, "artifact": artifact,
                })
                continue
            if kind == "file_contains":
                expected_text = str(spec["contains"])
                actual_text = (
                    target.read_text(encoding="utf-8") if artifact is not None else ""
                )
                results.append(
                    {
                        "kind": kind,
                        "acceptance_id": acceptance_id,
                        "path": spec["path"],
                        "passed": expected_text in actual_text,
                        "contains": expected_text,
                        "artifact": artifact,
                    }
                )
                continue
            if kind == "browser_evidence":
                payload: dict[str, Any] = {}
                if artifact is not None:
                    try:
                        loaded = json.loads(target.read_text(encoding="utf-8"))
                        payload = loaded if isinstance(loaded, dict) else {}
                    except (OSError, json.JSONDecodeError):
                        payload = {}
                required_viewports = list(
                    spec.get("required_viewports") or ["1440x900", "1024x768"]
                )
                screenshots = payload.get("screenshots")
                viewports = payload.get("viewports")
                passed_browser = bool(
                    artifact
                    and isinstance(screenshots, list)
                    and screenshots
                    and all(
                        isinstance(item, dict)
                        and item.get("path")
                        and item.get("sha256")
                        for item in screenshots
                    )
                    and isinstance(viewports, list)
                    and set(required_viewports).issubset(viewports)
                    and payload.get("console_errors") == []
                    and payload.get("network_errors") == []
                )
                results.append({
                    "acceptance_id": acceptance_id,
                    "kind": kind,
                    "path": spec["path"],
                    "passed": passed_browser,
                    "artifact": artifact,
                    "required_viewports": required_viewports,
                    "screenshots": screenshots if isinstance(screenshots, list) else [],
                    "console_errors": payload.get("console_errors"),
                    "network_errors": payload.get("network_errors"),
                })
                continue
            raise ValueError(f"unsupported verification kind: {kind}")

        reason_codes: list[str] = []
        failed_acceptance_ids = [
            str(item["acceptance_id"]) for item in results if not item["passed"]
        ]
        if not results:
            reason_codes.append("REQUIRED_GATE_MISSING")
        if failed_acceptance_ids:
            reason_codes.append("GATE_FAILED")

        structured_verdict = _structured_verdict(execution.stdout)
        authoritative_text = authoritative_assistant_text(execution.stdout)
        text_requires_changes = bool(
            require_structured_verdict
            and structured_verdict != "PASS"
            and re.search(
                r"\bCHANGES_REQUIRED\b",
                authoritative_text,
                re.IGNORECASE,
            )
        )
        if text_requires_changes:
            reason_codes.append("MODEL_TEXT_CHANGES_REQUIRED")
        if structured_verdict is not None and structured_verdict != "PASS":
            reason_codes.append("STRUCTURED_VERDICT_NOT_PASS")
        if require_structured_verdict and structured_verdict is None:
            reason_codes.append("STRUCTURED_VERDICT_MISSING")

        if reason_codes and not failed_acceptance_ids:
            failed_acceptance_ids = [
                str(item["acceptance_id"]) for item in results
            ]

        passed = not reason_codes
        verdict = "PASS" if passed else "CHANGES_REQUIRED"
        evidence = {
            "execution": {
                "returncode": execution.returncode,
                "failure_class": execution.failure_class,
            },
            "verdict": verdict,
            "structured_verdict": structured_verdict,
            "reason_codes": reason_codes,
            "failed_acceptance_ids": failed_acceptance_ids,
            "checks": results,
        }
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        summary = (
            "verification PASS"
            if passed
            else f"verification failed (CHANGES_REQUIRED): {', '.join(reason_codes)}"
        )
        return VerificationResult(
            passed=passed,
            checks=results,
            evidence_hash=evidence_hash,
            summary=summary,
            verdict=verdict,
            reason_codes=reason_codes,
            failed_acceptance_ids=list(dict.fromkeys(failed_acceptance_ids)),
        )


def _acceptance_ids(acceptance: list[str]) -> list[str]:
    return [
        f"acceptance-{index + 1:03d}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
        for index, text in enumerate(acceptance)
    ]


def _structured_verdict(output: str) -> str | None:
    """Read only the terminal JSON verdict from the last assistant message."""
    value = _terminal_json_value(authoritative_assistant_text(output))
    for _ in range(6):
        if isinstance(value, str):
            value = _terminal_json_value(value)
            continue
        if not isinstance(value, dict):
            break
        verdict = value.get("verdict")
        if verdict is not None:
            normalized = str(verdict).strip().upper()
            return (
                normalized
                if normalized in {"PASS", "CHANGES_REQUIRED"}
                else "INVALID"
            )
        value = next(
            (
                value[key]
                for key in (
                    "result",
                    "verification",
                    "output",
                    "content",
                    "message",
                )
                if key in value
            ),
            None,
        )
    return None


def authoritative_assistant_text(output: str) -> str:
    assistant_messages: list[str] = []
    result_messages: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        message: Any = None
        if event_type == "assistant":
            message = event.get("message")
        elif event_type == "agent_message":
            message = event
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item
        if message is not None:
            text = _message_text(message)
            if text:
                assistant_messages.append(text)
        elif event_type == "result":
            text = _message_text(event.get("result"))
            if text:
                result_messages.append(text)
    if assistant_messages:
        return assistant_messages[-1]
    if result_messages:
        return result_messages[-1]
    return output.strip()


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            text for item in value if (text := _message_text(item))
        ).strip()
    if not isinstance(value, dict):
        return ""
    for key in ("text", "output_text"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    for key in ("content", "message"):
        text = _message_text(value.get(key))
        if text:
            return text
    return ""


def _terminal_json_value(text: str) -> Any:
    candidate = (
        text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for start in reversed([
        index for index, character in enumerate(candidate) if character == "{"
    ]):
        try:
            return json.loads(candidate[start:])
        except json.JSONDecodeError:
            continue
    return None


def _safe_project_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("verification path must be relative")
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("verification path escapes project root")
    return target


def _safe_project_directory(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = (root / relative).resolve() if relative else root
    if not target.is_relative_to(root) or not target.is_dir():
        raise ValueError("verification cwd must be an existing project directory")
    return target


def _as_bytes(value: bytes | str | None) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8", errors="replace")


def _artifact_evidence(target: Path) -> dict[str, Any] | None:
    if not target.is_file():
        return None
    content = target.read_bytes()
    stat = target.stat()
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "modified_at_ns": stat.st_mtime_ns,
    }
