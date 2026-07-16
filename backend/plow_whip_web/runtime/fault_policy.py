from __future__ import annotations


class FaultPolicy:
    model_invoked = False

    @staticmethod
    def decide(failure_class: str, *, occurrences: int, attempts_left: int) -> str:
        if failure_class in {"database_locked", "domestic_unavailable", "overseas_unavailable", "offline"}:
            return "defer"
        if failure_class in {"provider_auth", "permission_denied", "budget_exceeded"}:
            return "needs_human"
        if occurrences >= 3 or attempts_left <= 0:
            return "terminal_failed"
        if failure_class in {"timeout", "command_failed", "verification_failed", "no_progress"}:
            return "retry_backoff"
        return "needs_human"
