from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from plow_whip_web.runtime.cron import CronExpression, validate_timezone

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
    network_requirement: Literal["none", "any", "domestic", "overseas"] = "none"
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")] = "generic-command"
    quality_profile: Literal["fast", "balanced", "strict"] = "balanced"
    command: CommandSpec
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 1
    token_budget: Annotated[int | None, Field(ge=0)] = None

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
    network_requirement: str
    same_failure_count: int
    no_progress_count: int
    last_failure_fingerprint: str | None
    next_eligible_at: str | None
    provider: str
    quality_profile: str
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
    host_path: str | None = None

    @field_validator("path")
    @classmethod
    def path_must_exist(cls, value: str) -> str:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("path must be an existing directory")
        return str(path)

    @field_validator("host_path")
    @classmethod
    def host_path_must_be_absolute(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        path = Path(value).expanduser()
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("host_path must be an absolute path")
        return str(path)


class ProjectView(BaseModel):
    id: str
    name: str
    path: str
    host_path: str | None
    status: str
    created_at: str
    roles: list[dict[str, Any]]
    workers: list[dict[str, Any]]


class RuntimeSettingsValues(BaseModel):
    scheduler_interval_seconds: Annotated[int, Field(ge=10, le=3600)] = 30
    scheduler_lease_seconds: Annotated[int, Field(ge=30, le=7200)] = 90
    cron_enabled: bool = True
    cron_expression: Annotated[str, Field(min_length=9, max_length=100)] = "*/1 * * * *"
    cron_timezone: Annotated[str, Field(min_length=1, max_length=100)] = "Asia/Shanghai"
    cron_misfire_policy: Literal["catch_up_once", "skip"] = "catch_up_once"
    max_parallel_workers: Annotated[int, Field(ge=1, le=64)] = 4
    auto_dispatch: bool = True
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

    @field_validator("cron_expression")
    @classmethod
    def cron_expression_must_be_valid(cls, value: str) -> str:
        return CronExpression.parse(value).source

    @field_validator("cron_timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        return validate_timezone(value)


class RuntimeSettingsView(BaseModel):
    revision: int
    values: RuntimeSettingsValues
    updated_at: str | None


class RuntimeSettingsUpdate(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]
    values: RuntimeSettingsValues


class ConventionPut(BaseModel):
    scope: Literal["global", "project", "task"]
    scope_id: Annotated[str, Field(min_length=1, max_length=100)]
    content: Annotated[str, Field(max_length=100_000)]
    expected_revision: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def global_scope_id_is_fixed(self) -> "ConventionPut":
        if self.scope == "global" and self.scope_id != "global":
            raise ValueError("global convention scope_id must be global")
        return self


class ConventionRefineRequest(BaseModel):
    provider: Annotated[str, Field(min_length=1, max_length=80)] = "simple-worker"
    project_id: Annotated[str | None, Field(max_length=100)] = None
    instruction: Annotated[str, Field(min_length=1, max_length=2000)] = (
        "在不改变原意和权限边界的前提下，删除重复内容，改写成清晰、可执行、可验证的中文约束。"
    )


class ProviderPut(BaseModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    adapter: Literal["codex", "cursor", "json-worker", "generic-command"]
    transport: Literal["host-bridge", "container"]
    executable: Annotated[str | None, Field(max_length=500)] = None
    enabled: bool = True
    credential_env: Annotated[str | None, Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")] = None
    capabilities: Annotated[list[str], Field(min_length=1, max_length=16)]
    expected_revision: Annotated[int, Field(ge=0)] = 0


class RotateWorkerRequest(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=200)] = "context_rotation"


class RebindWorkerRequest(BaseModel):
    provider: Annotated[str, Field(min_length=1, max_length=80)]
    reason: Annotated[str, Field(min_length=1, max_length=200)] = "provider_rebind"


class TaskControl(BaseModel):
    action: Literal["pause", "resume", "cancel", "needs_human"]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    expected_revision: Annotated[int, Field(ge=0)]


class PermissionGrantCreate(BaseModel):
    project_id: str | None = None
    capability: Literal["project_read", "project_write", "network_domestic", "network_overseas", "secret_reference"]
    resource: Annotated[str, Field(min_length=1, max_length=500)]
    decision: Literal["allow", "deny"]
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class RestoreRequest(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=200)]
    confirm: Literal["RESTORE"]
