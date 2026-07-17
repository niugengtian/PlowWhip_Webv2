from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class HostFaultDecision:
    action: str
    failure_class: str
    reason: str


class FaultPolicy:
    model_invoked = False

    _TRANSIENT_PATTERNS = (
        r"^\s*(?:error:\s*)?(?:\[aborted\]\s*)?socket hang up\s*$",
        r"^\s*(?:error:\s*)?(?:read\s+|write\s+)?econnreset(?:\b.*)?$",
        r"^\s*(?:error:\s*)?tls handshake(?:\b.*)?$",
        r"^\s*(?:error:\s*)?websocket eof(?:\b.*)?$",
        r"^\s*(?:error:\s*)?bridge temporary unavailable(?:\b.*)?$",
    )

    @classmethod
    def from_host_snapshot(cls, snapshot: dict[str, Any]) -> HostFaultDecision:
        failure_class = str(snapshot.get("failure_class") or "").strip().lower()
        evidence = "\n".join(
            str(snapshot.get(key) or "").lower()
            for key in ("stderr", "last_error", "error_summary")
        )
        try:
            returncode = int(snapshot["returncode"])
        except (KeyError, TypeError, ValueError):
            returncode = None
        status = str(snapshot.get("status") or "").strip().lower()

        if failure_class == "provider_auth" or any(marker in evidence for marker in (
            "invalid api key", "authentication failed", "http 401", "unauthorized",
        )):
            return HostFaultDecision("needs_human", "provider_auth", "provider_auth")
        if failure_class == "permission_denied" or any(marker in evidence for marker in (
            "permission denied", "access denied", "http 403", "forbidden",
        )):
            return HostFaultDecision(
                "needs_human", "permission_denied", "permission_denied"
            )
        if failure_class == "timeout" or returncode == 124:
            return HostFaultDecision(
                "resume", "timeout", "external_execution_interrupted"
            )
        if (
            failure_class == "transient_transport"
            or any(
                re.search(pattern, evidence, flags=re.IGNORECASE | re.MULTILINE)
                for pattern in cls._TRANSIENT_PATTERNS
            )
        ):
            return HostFaultDecision(
                "defer", "transient_transport", "transient_provider_transport"
            )
        if failure_class in {"command_failed", "verification_failed"}:
            return HostFaultDecision("verify", failure_class, failure_class)
        if status in {"interrupted", "cancelled"}:
            return HostFaultDecision(
                "resume", "external_interruption", "external_execution_interrupted"
            )
        if returncode is not None and returncode != 0:
            return HostFaultDecision("verify", "command_failed", "command_failed")
        if status == "completed" and (returncode or 0) == 0:
            return HostFaultDecision("verify", "none", "completed")
        return HostFaultDecision(
            "needs_human", "unknown_host_failure", "unknown_host_failure"
        )

    @staticmethod
    def decide(failure_class: str, *, occurrences: int, attempts_left: int) -> str:
        if failure_class in {
            "database_locked", "domestic_unavailable", "overseas_unavailable",
            "offline", "transient_transport",
        }:
            return "defer"
        if failure_class in {"provider_auth", "permission_denied", "budget_exceeded"}:
            return "needs_human"
        if occurrences >= 3 or attempts_left <= 0:
            return "terminal_failed"
        if failure_class in {"timeout", "command_failed", "verification_failed", "no_progress"}:
            return "retry_backoff"
        return "needs_human"
