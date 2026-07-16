from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    TERMINAL_FAILED = "terminal_failed"
    NEEDS_HUMAN = "needs_human"
    CANCELLED = "cancelled"


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
    status: TaskStatus
    revision: int
    command: dict[str, Any]
    verification: list[dict[str, Any]]
    max_attempts: int
    attempts_used: int
    token_budget: int
    tokens_used: int
    last_evidence_hash: str | None
    last_error: str | None
    created_at: str
    updated_at: str


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
