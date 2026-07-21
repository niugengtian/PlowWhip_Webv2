from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


TOKEN_BURN_RATE_ALERT_PER_MINUTE = 100_000


@dataclass(frozen=True, slots=True)
class EpisodeDecision:
    bounded: bool
    reason: str | None
    progress_bytes: int
    zero_progress_rounds: int
    same_fault_count: int
    burn_rate_tokens_per_minute: float
    burn_rate_alert: bool
    verifiable_progress: bool
    progress_evidence: dict[str, Any]


class ExecutionEpisodeWatchdog:
    """One deterministic boundary for every Host execution fault."""

    model_invoked = False

    @staticmethod
    def evaluate(
        episode: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        fault_class: str | None,
        elapsed_seconds: int,
        deadline_reached: bool,
        wall_clock_reached: bool,
        same_fault_limit: int,
        zero_progress_limit: int,
        seconds_since_progress: int | None = None,
        no_progress_seconds: int | None = None,
    ) -> EpisodeDecision:
        output = snapshot.get("output_bytes")
        output_bytes = (
            int(output.get("total") or 0) if isinstance(output, dict) else 0
        )
        previous_progress = int(episode.get("progress_bytes") or 0)
        running = str(snapshot.get("status") or "") in {
            "dispatching", "running", "orphan_running", "cancelling",
        }
        progress_evidence = _progress_evidence(snapshot)
        previous_evidence = _parse_evidence(
            episode.get("progress_evidence_json")
        )
        progressed = bool(
            progress_evidence
            and previous_evidence
            and progress_evidence.get("fingerprint")
            != previous_evidence.get("fingerprint")
        )
        zero_progress = (
            0
            if progressed
            else int(episode.get("zero_progress_rounds") or 0) + (1 if running else 0)
        )
        previous_fault = str(episode.get("last_fault_class") or "")
        same_faults = (
            int(episode.get("same_fault_count") or 0) + 1
            if fault_class and fault_class == previous_fault
            else (
                1
                if fault_class
                else 0 if running else int(episode.get("same_fault_count") or 0)
            )
        )
        tokens = int(snapshot.get("input_tokens") or 0) + int(
            snapshot.get("output_tokens") or 0
        )
        burn_rate = tokens * 60 / max(1, elapsed_seconds)
        reason = None
        if deadline_reached:
            reason = "deadline"
        elif wall_clock_reached:
            reason = "wall_clock"
        elif fault_class and int(episode["host_process_count"]) >= int(
            episode["max_host_processes"]
        ):
            reason = "host_processes"
        elif fault_class and same_faults >= same_fault_limit:
            reason = "same_fault"
        elif (
            no_progress_seconds is not None
            and seconds_since_progress is not None
            and seconds_since_progress >= no_progress_seconds
        ):
            reason = "zero_progress"
        elif no_progress_seconds is None and zero_progress >= zero_progress_limit:
            reason = "zero_progress"
        return EpisodeDecision(
            bounded=reason is not None,
            reason=reason,
            progress_bytes=max(previous_progress, output_bytes),
            zero_progress_rounds=zero_progress,
            same_fault_count=same_faults,
            burn_rate_tokens_per_minute=burn_rate,
            burn_rate_alert=burn_rate >= TOKEN_BURN_RATE_ALERT_PER_MINUTE,
            verifiable_progress=progressed,
            progress_evidence=progress_evidence or previous_evidence,
        )


def next_recovery_action(recovery_count: int) -> str:
    return {
        0: "resume",
        1: "replan",
        2: "replacement",
    }.get(recovery_count, "circuit_open")


def _parse_evidence(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _progress_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    supplied = snapshot.get("progress_evidence")
    structured: dict[str, Any] = {}
    if isinstance(supplied, dict) and supplied.get("available") is not False:
        supplied_without_fingerprint = {
            key: value
            for key, value in supplied.items()
            if key != "fingerprint"
        }
        if (
            supplied.get("kind") != "workspace"
            or int(supplied.get("changed_files") or 0) > 0
        ):
            structured["supplied"] = supplied_without_fingerprint
    output_segments = []
    for segment in snapshot.get("output_segments") or []:
        if not isinstance(segment, dict):
            continue
        ref = str(segment.get("ref") or "")
        sha256 = str(segment.get("sha256") or "")
        byte_count = int(segment.get("bytes") or 0)
        if not ref or not sha256 or byte_count <= 0:
            continue
        output_segments.append({
            "stream": str(segment.get("stream") or ""),
            "ref": ref,
            "sha256": sha256,
            "bytes": byte_count,
        })
    if output_segments:
        structured["provider_output_segments"] = sorted(
            output_segments,
            key=lambda item: (item["stream"], item["ref"]),
        )
    payload = {
        "workspace_revision": snapshot.get("workspace_revision"),
        "artifact_hashes": snapshot.get("artifact_hashes"),
        "verified_acceptance_ids": snapshot.get("verified_acceptance_ids"),
        "checkpoint_ref": snapshot.get("checkpoint_ref"),
    }
    if any(value for value in payload.values()):
        structured["explicit"] = payload
    if not structured:
        return {}
    canonical = json.dumps(
        structured, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "kind": "structured",
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        **structured,
    }
