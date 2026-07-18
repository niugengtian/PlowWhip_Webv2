from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    TERMINAL_FAILED = "terminal_failed"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"
    PAUSED = "paused"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.TERMINAL_FAILED,
    TaskStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    title: str
    objective: str
    project_path: str
    project_id: str | None
    role_id: str | None
    worker_id: str | None
    resource_key: str | None
    network_requirement: str
    same_failure_count: int
    no_progress_count: int
    last_failure_fingerprint: str | None
    next_eligible_at: str | None
    provider: str
    quality_profile: str
    status: TaskStatus
    revision: int
    command: dict[str, Any]
    verification: list[dict[str, Any]]
    max_attempts: int
    attempts_used: int
    tokens_used: int
    last_evidence_hash: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    sizing: dict[str, Any]
    execution_policy: dict[str, Any] | None
    goal_id: str | None = None
    parent_task_id: str | None = None
    depends_on: list[str] | None = None
    work_item_kind: str | None = None
    ordinal: int | None = None
    blocked_reason: str | None = None
    handoff: dict[str, Any] | None = None
    spec_revision: int = 1
    spec: dict[str, Any] = field(default_factory=dict)


class DomainError(RuntimeError):
    pass


class NotFoundError(DomainError):
    pass


class RevisionConflictError(DomainError):
    pass


class InvalidTransitionError(DomainError):
    pass


class ResourceBusyError(DomainError):
    pass


class ProviderUnavailableError(DomainError):
    pass


class HostBridgeRejectedError(ProviderUnavailableError):
    """The bridge definitively rejected a call before launching a model process."""


class HostBridgeOutcomeUnknownError(ProviderUnavailableError):
    """The caller cannot prove whether the bridge accepted or launched the call."""


class PolicyViolationError(DomainError):
    pass
