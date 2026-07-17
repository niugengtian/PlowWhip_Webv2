from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plow_whip_web.providers.generic_command import ExecutionResult


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    checks: list[dict[str, Any]]
    evidence_hash: str
    summary: str


class VerificationEngine:
    def verify(
        self,
        project_path: Path,
        execution: ExecutionResult,
        specs: list[dict[str, Any]],
    ) -> VerificationResult:
        results: list[dict[str, Any]] = []
        for spec in specs:
            kind = spec["kind"]
            if kind == "exit_code":
                expected = int(spec.get("expected", 0))
                actual = execution.returncode
                results.append(
                    {"kind": kind, "passed": actual == expected, "expected": expected, "actual": actual}
                )
                continue
            target = _safe_project_path(project_path, spec["path"])
            artifact = _artifact_evidence(target)
            if kind == "file_exists":
                results.append({
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
                        "path": spec["path"],
                        "passed": expected_text in actual_text,
                        "contains": expected_text,
                        "artifact": artifact,
                    }
                )
                continue
            raise ValueError(f"unsupported verification kind: {kind}")

        passed = bool(results) and all(item["passed"] for item in results)
        evidence = {
            "execution": {
                "returncode": execution.returncode,
                "failure_class": execution.failure_class,
            },
            "checks": results,
        }
        canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        failed = [item["kind"] for item in results if not item["passed"]]
        summary = "verification passed" if passed else f"verification failed: {', '.join(failed)}"
        return VerificationResult(passed, results, evidence_hash, summary)


def _safe_project_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise ValueError("verification path must be relative")
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("verification path escapes project root")
    return target


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
