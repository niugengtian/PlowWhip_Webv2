from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MAX_EPISODE_WALL_SECONDS = 900
MAX_EPISODE_HOST_PROCESSES = 2
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
    ) -> EpisodeDecision:
        output = snapshot.get("output_bytes")
        output_bytes = (
            int(output.get("total") or 0) if isinstance(output, dict) else 0
        )
        previous_progress = int(episode.get("progress_bytes") or 0)
        running = str(snapshot.get("status") or "") in {
            "dispatching", "running", "orphan_running", "cancelling",
        }
        progressed = output_bytes > previous_progress
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
        elif zero_progress >= zero_progress_limit:
            reason = "zero_progress"
        return EpisodeDecision(
            bounded=reason is not None,
            reason=reason,
            progress_bytes=max(previous_progress, output_bytes),
            zero_progress_rounds=zero_progress,
            same_fault_count=same_faults,
            burn_rate_tokens_per_minute=burn_rate,
            burn_rate_alert=burn_rate >= TOKEN_BURN_RATE_ALERT_PER_MINUTE,
        )


def next_recovery_action(recovery_count: int) -> str:
    return {
        0: "resume",
        1: "replan",
        2: "replacement",
    }.get(recovery_count, "circuit_open")
