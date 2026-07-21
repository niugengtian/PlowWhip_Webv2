from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "engineering_ledger.py"


def run_ledger(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_structured_ledger_and_generated_views_are_in_sync() -> None:
    result = run_ledger("check")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["entries"] == 28
    assert payload["open_incidents"] == 15


def test_ui_sorting_context_pack_is_bounded_and_routed() -> None:
    result = run_ledger("context", "--domains", "ui.sorting", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ids = {entry["id"] for entry in payload["entries"]}
    assert ids == {"R-001", "R-004", "R-007", "R-009", "R-013", "I-014"}
    assert "I-011" not in ids
    assert len(ids) <= payload["limits"]["entries"]
    assert payload["summary_chars"] <= payload["limits"]["summary_chars"]
    assert all(entry["sha256"] for entry in payload["entries"])
    assert all(entry["reasons"] for entry in payload["entries"])


def test_unknown_context_domain_fails_closed() -> None:
    result = run_ledger("context", "--domains", "not-a-domain")

    assert result.returncode == 1
    assert "unknown domains" in result.stderr


def test_retry_context_includes_state_flapping_incident() -> None:
    result = run_ledger("context", "--domains", "task.retry", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    ids = {entry["id"] for entry in payload["entries"]}
    assert {"R-004", "R-006", "I-003", "I-010", "I-015"} <= ids
    assert len(ids) <= payload["limits"]["entries"]
