from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from plow_whip_web.runtime.cron import CronExpression, validate_timezone

from plow_whip_web.domain.model import TaskRecord, TaskStatus


class CommandSpec(BaseModel):
    argv: Annotated[list[str] | None, Field(min_length=1, max_length=64)] = None
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    output_limit_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 131_072

    @field_validator("argv")
    @classmethod
    def argv_must_be_non_empty(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
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


class TokenEstimateBand(BaseModel):
    min: int
    max: int
    p90: int


class TaskSizingEstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers_touched: Annotated[int, Field(ge=0, le=32)] = 1
    components_touched: Annotated[int, Field(ge=0, le=128)] = 1
    estimated_files_changed: Annotated[int, Field(ge=0, le=512)] = 1
    has_migration: bool = False
    has_deploy: bool = False
    verification_commands_count: Annotated[int, Field(ge=0, le=64)] = 1
    estimated_verification_seconds: Annotated[int, Field(ge=0, le=86_400)] = 60
    external_dependencies_count: Annotated[int, Field(ge=0, le=64)] = 0
    risk_level: Literal["low", "medium", "high"] = "low"
    independent_review_required: bool = False
    gate_artifact: bool = False
    gate_boundary: bool = False
    gate_verification: bool = False
    gate_dependency: bool = False


class TaskSizingEstimateResponse(BaseModel):
    status: Literal["estimated", "needs_planning"]
    missing_gates: list[str]
    size_class: Literal["XS", "S", "M", "L", "XL"] | None
    rationale: list[str]
    estimated_input_tokens: TokenEstimateBand | None
    estimated_output_tokens: TokenEstimateBand | None
    soft_deadline_seconds: int | None
    hard_deadline_seconds: int | None
    max_turns: int | None
    max_attempts: int | None
    verification_timeout_seconds: int | None
    progress_extension_seconds: int | None
    total_token_hard_cap: int | None
    reserved_tokens: int | None
    model_invoked: Literal[False] = False
    bootstrap_version: str


class TaskCreate(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    project_path: str | None = None
    project_id: str | None = None
    role: Literal[
        "coordination",
        "backend",
        "frontend",
        "ui",
        "devops_sre",
        "verification",
        "fullstack",
        "web3",
    ] = "fullstack"
    resource_key: Annotated[str | None, Field(max_length=300)] = None
    network_requirement: Literal["none", "any", "domestic", "overseas"] = "none"
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")] = "generic-command"
    # Deprecated compatibility input. Every accepted value has one deterministic
    # runtime meaning; legacy names are not separate quality commitments.
    quality_profile: Literal["fast", "balanced", "strict", "deterministic"] = "deterministic"
    command: CommandSpec
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    max_attempts: Annotated[int | None, Field(ge=1, le=10)] = None
    token_budget: Annotated[int | None, Field(ge=0)] = None
    sizing_inputs: TaskSizingEstimateRequest | None = None
    manual_override_reason: Annotated[str | None, Field(min_length=1, max_length=500)] = None

    @field_validator("quality_profile")
    @classmethod
    def normalize_quality_profile(
        cls, value: Literal["fast", "balanced", "strict", "deterministic"]
    ) -> Literal["deterministic"]:
        return "deterministic"

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
        if self.provider == "generic-command" and not self.command.argv:
            raise ValueError("generic-command requires command.argv")
        return self


class ExpectedRevision(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]


class DeletionRequest(ExpectedRevision):
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    actor_id: Annotated[str | None, Field(max_length=200)] = None


class EvidenceRewriteRequest(ExpectedRevision):
    evidence_hash: Annotated[str, Field(min_length=8, max_length=256)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    actor_id: Annotated[str | None, Field(max_length=200)] = None


class ButlerIntakeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    source: Literal["structured", "natural_language"]
    instruction: Annotated[str, Field(min_length=1, max_length=8000)]
    structured_input: dict[str, Any] | None = None
    model_size: Literal["small", "medium", "large"] | None = None
    confidence: Annotated[int, Field(ge=0, le=100)] = 0
    proposal: dict[str, Any] | None = None


class ButlerAnswer(ExpectedRevision):
    answer: Annotated[str, Field(min_length=1, max_length=4000)]
    confidence: Annotated[int, Field(ge=0, le=100)]
    proposal: dict[str, Any] | None = None


class ButlerConfirm(ExpectedRevision):
    proposal_hash: Annotated[str, Field(min_length=64, max_length=64)]
    approved: bool
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class WorkerHelpCreate(BaseModel):
    project_id: str
    task_id: str
    worker_id: str | None = None
    category: Annotated[str, Field(min_length=1, max_length=100)]
    severity: Literal["normal", "blocking", "extreme"] = "normal"
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class WorkerHelpReply(ExpectedRevision):
    sender: Literal["butler", "owner", "system"]
    content: Annotated[str, Field(min_length=1, max_length=4000)]
    bounded_context: dict[str, Any] = Field(default_factory=dict)
    escalate: bool = False


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
    sizing: dict[str, Any]
    execution_budget: dict[str, Any] | None
    manual_override: bool
    override_reason: str | None
    budget_overrun_evidence: dict[str, Any] | None
    goal_id: str | None = None
    parent_task_id: str | None = None
    depends_on: list[str] | None = None
    work_item_kind: str | None = None
    ordinal: int | None = None
    blocked_reason: str | None = None
    handoff: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskView":
        return cls(**asdict(record))


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    project_id: str
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")] = "generic-command"
    role_providers: dict[
        str, Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")]
    ] = Field(default_factory=dict)
    network_requirement: Literal["none", "any", "domestic", "overseas"] = "none"
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    sizing_inputs: TaskSizingEstimateRequest
    command: CommandSpec | None = None
    # Optional bounded structured plan. Model PM is not implemented this sprint;
    # when omitted, a deterministic template (sizing flags only) is used.
    plan_items: list[dict[str, Any]] | None = None


class GoalView(BaseModel):
    id: str
    title: str
    objective: str
    project_id: str
    provider: str
    status: str
    revision: int = 0
    last_evidence_hash: str | None = None
    plan: dict[str, Any]
    sizing_inputs: dict[str, Any] | None
    parent_task_id: str | None
    created_at: str
    updated_at: str
    work_items: list[dict[str, Any]]


class TaskEventView(BaseModel):
    sequence: int
    event_type: str
    payload: dict[str, object]
    state_revision: int
    created_at: str


class TaskArtifactView(BaseModel):
    relative_path: str
    host_path: str
    exists: bool
    bytes: int | None
    sha256: str | None
    modified_at: str | None
    actions: list[Literal["finder", "cursor"]]


class ArtifactOpenRequest(BaseModel):
    relative_path: str
    action: Literal["finder", "cursor"]


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
    convention_refinement_token_budget: Annotated[int, Field(ge=1, le=100_000_000)] = 10_000
    max_same_failure: Annotated[int, Field(ge=1, le=20)] = 2
    max_no_progress: Annotated[int, Field(ge=1, le=20)] = 3
    session_no_progress_rotation_threshold: Annotated[int, Field(ge=1, le=20)] = 2
    context_max_bytes: Annotated[int, Field(ge=4096, le=1_048_576)] = 32_768
    checkpoint_max_bytes: Annotated[int, Field(ge=512, le=262_144)] = 4096
    handoff_max_bytes: Annotated[int, Field(ge=512, le=262_144)] = 4096
    observation_tail_lines: Annotated[int, Field(ge=1, le=200)] = 20
    observation_max_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 65_536
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
