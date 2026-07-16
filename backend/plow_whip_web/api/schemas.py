from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from plow_whip_web.domain.model import TaskRecord, TaskStatus


class CommandSpec(BaseModel):
    argv: Annotated[list[str], Field(min_length=1, max_length=64)]
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    output_limit_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 131_072

    @field_validator("argv")
    @classmethod
    def argv_must_be_non_empty(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and cannot contain NUL")
        return value


class VerificationSpec(BaseModel):
    kind: Literal["exit_code", "file_exists", "file_contains"]
    expected: int | None = None
    path: str | None = None
    contains: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "VerificationSpec":
        if self.kind in {"file_exists", "file_contains"}:
            if not self.path or Path(self.path).is_absolute() or ".." in Path(self.path).parts:
                raise ValueError("file verification requires a safe relative path")
        if self.kind == "file_contains" and self.contains is None:
            raise ValueError("file_contains requires contains")
        return self


class TaskCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    project_path: str | None = None
    project_id: str | None = None
    role: Literal["coordination", "fullstack", "web3", "devops_sre", "verification"] = "fullstack"
    resource_key: Annotated[str | None, Field(max_length=300)] = None
    command: CommandSpec
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 1
    token_budget: Annotated[int, Field(ge=0)] = 0

    @field_validator("project_path")
    @classmethod
    def project_path_must_exist(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("project_path must be an existing directory")
        return str(path)

    @model_validator(mode="after")
    def project_reference_required(self) -> "TaskCreate":
        if self.project_id is None and self.project_path is None:
            raise ValueError("project_id or project_path is required")
        return self


class ExpectedRevision(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]


class TaskView(BaseModel):
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
    command: dict[str, object]
    verification: list[dict[str, object]]
    max_attempts: int
    attempts_used: int
    token_budget: int
    tokens_used: int
    last_evidence_hash: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskView":
        return cls(**asdict(record))


class TaskEventView(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, object]
    state_revision: int
    created_at: str


class ProjectCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    path: str

    @field_validator("path")
    @classmethod
    def path_must_exist(cls, value: str) -> str:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("path must be an existing directory")
        return str(path)


class ProjectView(BaseModel):
    id: str
    name: str
    path: str
    status: str
    created_at: str
    roles: list[dict[str, Any]]
    workers: list[dict[str, Any]]


class RuntimeSettingsValues(BaseModel):
    scheduler_interval_seconds: Annotated[int, Field(ge=10, le=3600)] = 30
    scheduler_lease_seconds: Annotated[int, Field(ge=30, le=7200)] = 90
    max_parallel_workers: Annotated[int, Field(ge=1, le=64)] = 4
    auto_dispatch: bool = True
    system_scheduler_authorized: bool = False
    task_default_token_budget: Annotated[int, Field(ge=0, le=100_000_000)] = 50_000
    global_daily_token_budget: Annotated[int, Field(ge=0, le=1_000_000_000)] = 500_000
    max_same_failure: Annotated[int, Field(ge=1, le=20)] = 3
    max_no_progress: Annotated[int, Field(ge=1, le=20)] = 3
    context_max_bytes: Annotated[int, Field(ge=4096, le=1_048_576)] = 32_768
    rotation_max_bytes: Annotated[int, Field(ge=16_384, le=16_777_216)] = 262_144

    @model_validator(mode="after")
    def lease_must_exceed_interval(self) -> "RuntimeSettingsValues":
        if self.scheduler_lease_seconds < self.scheduler_interval_seconds * 2:
            raise ValueError("scheduler lease must be at least twice the interval")
        return self


class RuntimeSettingsView(BaseModel):
    revision: int
    values: RuntimeSettingsValues
    updated_at: str | None


class RuntimeSettingsUpdate(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]
    values: RuntimeSettingsValues
