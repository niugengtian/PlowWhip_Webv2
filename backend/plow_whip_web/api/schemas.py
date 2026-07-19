from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from plow_whip_web.runtime.cron import CronExpression, validate_timezone
from plow_whip_web.runtime.butler import project_execution_policy

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
    soft_deadline_seconds: int | None
    hard_deadline_seconds: int | None
    max_turns: int | None
    max_attempts: int | None
    verification_timeout_seconds: int | None
    progress_extension_seconds: int | None
    model_invoked: Literal[False] = False
    bootstrap_version: str


class TaskDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_seconds: Annotated[int, Field(ge=1, le=4800)]


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    scope: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    acceptance: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    artifacts: Annotated[list[str] | None, Field(max_length=64)] = None
    constraints: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    deadline: TaskDeadline | None = None
    max_attempts: Annotated[int | None, Field(ge=1, le=10)] = None
    sizing_inputs: TaskSizingEstimateRequest | None = None

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

    @field_validator("artifacts")
    @classmethod
    def artifact_paths_must_be_safe(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(
            not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in value
        ):
            raise ValueError("artifacts require safe relative paths")
        return value


class ExpectedRevision(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]


class TaskDeleteRequest(ExpectedRevision):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class TaskAmendRequest(ExpectedRevision):
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    scope: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    acceptance: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    artifacts: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    constraints: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    deadline: TaskDeadline

    @field_validator("artifacts")
    @classmethod
    def artifact_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        if any(
            not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in value
        ):
            raise ValueError("artifacts require safe relative paths")
        return value


class TaskDeletionEligibilityView(BaseModel):
    deletable: bool
    reason: str | None


class TaskDeletionView(BaseModel):
    task_id: str
    title: str
    reason: str
    deleted_revision: int
    idempotency_key: str
    deleted_at: str


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
    spec_revision: int
    spec: dict[str, Any]
    execution_episode: dict[str, Any] | None = None

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskView":
        return cls(**asdict(record))


class GoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    project_id: str
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")] = "generic-command"
    network_requirement: Literal["none", "any", "domestic", "overseas"] = "none"
    verification: Annotated[list[VerificationSpec], Field(min_length=1, max_length=32)]
    scope: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    acceptance: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    artifacts: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    constraints: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    deadline: TaskDeadline | None = None
    sizing_inputs: TaskSizingEstimateRequest
    command: CommandSpec | None = None
    # Optional bounded structured plan. Model PM is not implemented this sprint;
    # when omitted, a deterministic template (sizing flags only) is used.
    plan_items: list[dict[str, Any]] | None = None
    role_providers: dict[
        Literal["backend", "frontend", "ui", "devops_sre", "verification", "fullstack"],
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")],
    ] = Field(default_factory=dict)

    @field_validator("artifacts")
    @classmethod
    def artifact_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        if any(
            not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in value
        ):
            raise ValueError("artifacts require safe relative paths")
        return value


class GoalView(BaseModel):
    id: str
    title: str
    objective: str
    project_id: str
    provider: str
    status: str
    plan: dict[str, Any]
    sizing_inputs: dict[str, Any] | None
    parent_task_id: str | None
    created_at: str
    updated_at: str
    work_items: list[dict[str, Any]]
    spec_revision: int
    spec: dict[str, Any]


class ButlerConversationStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["human", "global_butler", "agent"] = "human"
    source_id: Annotated[str | None, Field(max_length=200)] = None
    instruction: Annotated[str, Field(min_length=1, max_length=10_000)]
    title: Annotated[str | None, Field(max_length=200)] = None
    objective: Annotated[str | None, Field(max_length=4000)] = None
    boundaries: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    acceptance: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    scope: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    artifacts: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    constraints: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")] = "cursor"
    role_providers: dict[
        Literal["backend", "frontend", "ui", "devops_sre", "verification", "fullstack"],
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")],
    ] = Field(default_factory=dict)
    network_requirement: Literal["none", "any", "domestic", "overseas"] = "none"
    verification: Annotated[list[VerificationSpec], Field(max_length=32)] = Field(
        default_factory=lambda: [VerificationSpec(kind="exit_code", expected=0)]
    )
    sizing_inputs: TaskSizingEstimateRequest | None = None
    deadline: TaskDeadline | None = None
    command: CommandSpec | None = None
    plan_items: list[dict[str, Any]] | None = None

    @field_validator("artifacts")
    @classmethod
    def butler_artifact_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        if any(
            not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in value
        ):
            raise ValueError("artifacts require safe relative paths")
        return value


class GlobalButlerRoute(ButlerConversationStart):
    project_id: str
    source_type: Literal["human", "agent"] = "human"


class ButlerAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(ge=0)]
    field: Literal["objective", "boundaries", "acceptance"]
    values: Annotated[list[str], Field(min_length=1, max_length=64)]
    sender_type: Literal["human", "agent"] = "human"


class ButlerMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(ge=0)]
    content: Annotated[str, Field(min_length=1, max_length=10_000)]
    sender_type: Literal["human", "agent"] = "human"
    field: Literal["objective", "boundaries", "acceptance"] | None = None


class ButlerConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(ge=0)]
    proposal_hash: Annotated[str, Field(min_length=64, max_length=64)]
    actor_type: Literal["human"]


class ButlerConversationView(BaseModel):
    id: str
    scope: Literal["global", "project"]
    project_id: str | None
    source_type: Literal["human", "global_butler", "agent"]
    source_id: str | None
    status: Literal["clarifying", "awaiting_confirmation", "dispatched", "rejected"]
    revision: int
    confidence: int
    expected_field: Literal["objective", "boundaries", "acceptance"] | None
    spec: dict[str, Any]
    proposal_hash: str | None
    goal_id: str | None
    idempotency_key: str
    created_at: str
    updated_at: str
    messages: list[dict[str, Any]]
    direct_project_butler_url: str | None
    auto_dispatch: bool = False
    structured_goal_spec: bool = False
    semantic: dict[str, Any] | None = None


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
    execution_policy: dict[str, Any] | None = None

    @field_validator("execution_policy")
    @classmethod
    def execution_policy_must_match_butler_contract(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        return project_execution_policy(value) if value is not None else None

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
    execution_policy: dict[str, Any]
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
    max_same_failure: Annotated[int, Field(ge=1, le=20)] = 2
    max_no_progress: Annotated[int, Field(ge=1, le=20)] = 3
    context_max_bytes: Annotated[int, Field(ge=4096, le=1_048_576)] = 32_768
    rotation_max_bytes: Annotated[int, Field(ge=16_384, le=16_777_216)] = 262_144
    checkpoint_max_bytes: Annotated[int, Field(ge=512, le=262_144)] = 4_096
    handoff_max_bytes: Annotated[int, Field(ge=256, le=131_072)] = 2_048
    observation_tail_lines: Annotated[int, Field(ge=1, le=500)] = 20
    observation_max_bytes: Annotated[int, Field(ge=1024, le=262_144)] = 8_192
    @model_validator(mode="after")
    def lease_must_exceed_interval(self) -> "RuntimeSettingsValues":
        if self.scheduler_lease_seconds < self.scheduler_interval_seconds * 2:
            raise ValueError("scheduler lease must be at least twice the interval")
        if (
            self.checkpoint_max_bytes
            + self.handoff_max_bytes
            + 2048
            > self.context_max_bytes
        ):
            raise ValueError(
                "checkpoint + handoff + mandatory reserve exceeds context limit"
            )
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
    sources: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    override_revisions: dict[str, int] = Field(default_factory=dict)
    updated_at: str | None


class RuntimeSettingsUpdate(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]
    values: RuntimeSettingsValues


class RuntimeSettingsOverrideUpdate(BaseModel):
    expected_revision: Annotated[int, Field(ge=0)]
    values: dict[str, Annotated[int, Field(ge=1)]]


class ConventionPut(BaseModel):
    scope: Literal["global", "project", "task", "task_role"]
    scope_id: Annotated[str, Field(min_length=1, max_length=200)]
    content: Annotated[str, Field(max_length=100_000)]
    expected_revision: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def global_scope_id_is_fixed(self) -> "ConventionPut":
        if self.scope == "global" and self.scope_id != "global":
            raise ValueError("global convention scope_id must be global")
        if self.scope == "task_role" and ":" not in self.scope_id:
            raise ValueError("task_role scope_id must be task_id:role_id")
        return self


class WorkerHelpCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    worker_id: str | None = None
    blocker: Annotated[str, Field(min_length=1, max_length=4000)]
    evidence: dict[str, Any] | list[Any]
    attempted_actions: Annotated[list[str], Field(min_length=1, max_length=32)]
    minimal_question: Annotated[str, Field(min_length=1, max_length=2000)]


class ProjectRoleRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: Annotated[str, Field(min_length=1, max_length=200)]
    rule_revision: int | None = None
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    source: Annotated[str, Field(min_length=1, max_length=200)] = "owner"
    capability: Annotated[str | None, Field(max_length=80)] = None
    template_id: Annotated[str | None, Field(max_length=200)] = None


class WorkerHelpResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Literal["answered", "replanned", "replaced", "closed"]
    detail: dict[str, Any] | None = None


class TaskEscalationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    reason_class: Literal[
        "credential_or_permission",
        "safety_or_irreversible",
        "conflicting_owner_directives",
        "unresolvable_requirement_ambiguity",
    ]
    detail: Annotated[str, Field(min_length=1, max_length=4000)]
    help_request_id: str | None = None


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
    action: Literal[
        "pause", "resume", "cancel", "needs_human", "retry", "restart",
    ]
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
