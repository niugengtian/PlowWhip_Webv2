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
        r"^\s*retriableerror:\s*connection stalled\s*$",
    )

    _NO_PROGRESS_MARKERS = (
        "tool call aborted",
        "tools aborted",
        "internal tool aborted",
        "awaiting tool",
        "waiting for tool",
        "no progress",
        "stalled",
    )
    _CAPACITY_MARKERS = (
        "selected model is at capacity",
        "model is at capacity",
        "rate limit exceeded",
        "rate_limit_exceeded",
        "too many requests",
        "http 429",
        "status 429",
        "server is overloaded",
        "overloaded_error",
    )

    @classmethod
    def evidence_text(cls, snapshot: dict[str, Any]) -> str:
        return "\n".join(
            str(snapshot.get(key) or "").lower()
            for key in (
                "stderr", "last_error", "error_summary", "failure_class", "status",
            )
        )

    @classmethod
    def is_no_progress(cls, snapshot: dict[str, Any]) -> bool:
        failure_class = str(snapshot.get("failure_class") or "").strip().lower()
        if failure_class in {"no_progress", "tool_aborted", "internal_tool_aborted"}:
            return True
        evidence = cls.evidence_text(snapshot)
        if not any(marker in evidence for marker in cls._NO_PROGRESS_MARKERS):
            return False
        try:
            returncode = int(snapshot["returncode"])
        except (KeyError, TypeError, ValueError):
            returncode = None
        if returncode is not None:
            return False
        # Internal tool abort/wait with no process exit status and no useful output.
        output_bytes = snapshot.get("output_bytes") or {}
        total = 0
        if isinstance(output_bytes, dict):
            total = int(output_bytes.get("total") or 0)
        return total < 256

    @classmethod
    def from_host_snapshot(cls, snapshot: dict[str, Any]) -> HostFaultDecision:
        failure_class = str(snapshot.get("failure_class") or "").strip().lower()
        evidence = cls.evidence_text(snapshot)
        try:
            returncode = int(snapshot["returncode"])
        except (KeyError, TypeError, ValueError):
            returncode = None
        status = str(snapshot.get("status") or "").strip().lower()

        if failure_class == "dispatch_rejected" or status == "rejected":
            return HostFaultDecision(
                "needs_human", "dispatch_rejected", "host_dispatch_rejected"
            )

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
        if failure_class == "provider_capacity" or any(
            marker in evidence for marker in cls._CAPACITY_MARKERS
        ):
            return HostFaultDecision(
                "defer", "provider_capacity", "transient_provider_capacity"
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
        if cls.is_no_progress(snapshot):
            return HostFaultDecision(
                "resume", "no_progress", "internal_tool_no_progress"
            )
        if failure_class == "timeout" or returncode == 124:
            return HostFaultDecision(
                "resume", "timeout", "external_execution_interrupted"
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
