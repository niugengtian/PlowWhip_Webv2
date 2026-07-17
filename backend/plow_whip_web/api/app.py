from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from plow_whip_web import __version__
from plow_whip_web.api.schemas import (
    ArtifactOpenRequest,
    ExpectedRevision,
    ConventionPut,
    ConventionRefineRequest,
    ProjectCreate,
    ProjectView,
    ProviderPut,
    PermissionGrantCreate,
    RestoreRequest,
    RuntimeSettingsUpdate,
    RuntimeSettingsView,
    RotateWorkerRequest,
    RebindWorkerRequest,
    TaskCreate,
    TaskControl,
    TaskEventView,
    TaskArtifactView,
    TaskSizingEstimateRequest,
    TaskSizingEstimateResponse,
    TaskView,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.config import Settings
from plow_whip_web.domain.model import (
    DomainError,
    InvalidTransitionError,
    NotFoundError,
    RevisionConflictError,
    ResourceBusyError,
    ProviderUnavailableError,
    PolicyViolationError,
)
from plow_whip_web.runtime.task_service import TaskService
from plow_whip_web.runtime.scheduler import SchedulerService
from plow_whip_web.runtime.cron import EmbeddedCronRunner, schedule_view
from plow_whip_web.runtime.budget import BudgetManager
from plow_whip_web.runtime.context import ContextCompiler
from plow_whip_web.runtime.journal import SessionJournal
from plow_whip_web.runtime.connectivity import ConnectivityProbe
from plow_whip_web.runtime.recovery import RecoveryService
from plow_whip_web.store.database import Database
from plow_whip_web.store.convention_repository import ConventionRepository
from plow_whip_web.store.project_repository import ProjectRepository
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.health_repository import HealthRepository
from plow_whip_web.store.outbox_repository import OutboxRepository
from plow_whip_web.store.audit_repository import AuditRepository
from plow_whip_web.store.permission_repository import PermissionRepository
from plow_whip_web.store.provider_repository import ProviderRepository
from plow_whip_web.store.task_repository import TaskRepository
from plow_whip_web.store.host_job_repository import HostJobRepository
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.roles import ROLE_PROMPTS
from plow_whip_web.maintenance import MaintenanceService


def create_app(settings: Settings) -> FastAPI:
    settings.prepare()
    database = Database(settings.database_path)
    database.migrate()
    task_repository = TaskRepository(database)
    host_jobs = HostJobRepository(database)
    project_repository = ProjectRepository(database)
    runtime_settings = SettingsRepository(database)
    conventions = ConventionRepository(database)
    budget = BudgetManager(database, runtime_settings)
    context_compiler = ContextCompiler(settings.data_dir, database, task_repository, conventions, runtime_settings)
    journal = SessionJournal(settings.data_dir, runtime_settings)
    health_repository = HealthRepository(database)
    outbox = OutboxRepository(database)
    recovery = RecoveryService(database)
    audit = AuditRepository(database)
    permissions = PermissionRepository(database)
    providers = ProviderRepository(database)
    provider_pool = ProviderPool(
        database, providers, task_repository,
        HostBridgeClient(settings.host_bridge_url, settings.host_bridge_token),
    )
    maintenance = MaintenanceService(
        settings.data_dir, database, runtime_settings, health_repository, providers
    )
    task_service = TaskService(
        task_repository, budget=budget, context_compiler=context_compiler, journal=journal,
        provider_pool=provider_pool, host_jobs=host_jobs,
    )
    scheduler_repository = SchedulerRepository(database)
    scheduler_service = SchedulerService(
        scheduler_repository, runtime_settings, task_repository, task_service,
        connectivity=ConnectivityProbe(), health=health_repository, recovery=recovery,
        provider_pool=provider_pool,
    )
    embedded_cron_runner = EmbeddedCronRunner(
        scheduler_service, scheduler_repository, runtime_settings
    )

    app = FastAPI(
        title="plow-whip Web v2",
        version=__version__,
        description="Quality-first unattended workflow control plane",
    )
    app.state.settings = settings
    app.state.database = database
    app.state.task_repository = task_repository
    app.state.host_jobs = host_jobs
    app.state.project_repository = project_repository
    app.state.task_service = task_service
    app.state.runtime_settings = runtime_settings
    app.state.scheduler_repository = scheduler_repository
    app.state.scheduler_service = scheduler_service
    app.state.embedded_cron_runner = embedded_cron_runner
    app.state.conventions = conventions
    app.state.budget = budget
    app.state.context_compiler = context_compiler
    app.state.journal = journal
    app.state.health_repository = health_repository
    app.state.outbox = outbox
    app.state.recovery = recovery
    app.state.audit = audit
    app.state.permissions = permissions
    app.state.providers = providers
    app.state.provider_pool = provider_pool
    app.state.maintenance = maintenance

    @app.middleware("http")
    async def security_and_audit(request: Request, call_next):
        actor = "loopback-local"
        if not settings.is_loopback:
            expected = f"Bearer {settings.api_token}"
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(status_code=401, content={"detail": "local API authentication required"})
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).hostname != request.url.hostname:
                return JSONResponse(status_code=403, content={"detail": "origin rejected"})
            actor = "authenticated-local-client"
        response = await call_next(request)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            audit.record(
                actor=actor, method=request.method, path=request.url.path,
                status_code=response.status_code,
                detail={"client": request.client.host if request.client else None},
            )
        return response

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RevisionConflictError)
    async def revision_handler(_request: Request, error: RevisionConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "revision_conflict"})

    @app.exception_handler(InvalidTransitionError)
    async def transition_handler(_request: Request, error: InvalidTransitionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "invalid_transition"})

    @app.exception_handler(ResourceBusyError)
    async def resource_busy_handler(_request: Request, error: ResourceBusyError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "resource_busy"})

    @app.exception_handler(ProviderUnavailableError)
    async def provider_unavailable_handler(_request: Request, error: ProviderUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "provider_unavailable"})

    @app.exception_handler(PolicyViolationError)
    async def policy_violation_handler(_request: Request, error: PolicyViolationError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(error), "code": "policy_violation"})

    @app.exception_handler(DomainError)
    async def domain_handler(_request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(error)})

    @app.get("/health", tags=["system"])
    def health(request: Request) -> dict[str, object]:
        db: Database = request.app.state.database
        return {
            "status": "ok",
            "version": __version__,
            "database": db.health(),
        }

    @app.get("/api/system/capabilities", tags=["system"])
    def capabilities() -> dict[str, object]:
        return {
            "web_control_plane": True,
            "desktop_required": False,
            "model_invoked": False,
            "multi_project": True,
            "durable_worker_sessions": True,
            "zero_token_scheduler": True,
            "compiled_context": True,
            "three_scope_conventions": True,
            "fault_recovery": True,
            "anti_loop_guards": True,
            "audited_permissions": True,
            "loopback_default": settings.is_loopback,
            "container_runtime": True,
            "host_scheduler_required": False,
            "embedded_cron": True,
            "docker_managed_sqlite": True,
            "worker_provider_pool": True,
            "restricted_host_bridge": True,
            "durable_host_jobs": True,
            "early_cli_session_persistence": True,
            "safe_running_cancel": True,
            "convention_refinement": True,
            "platform_api_key_required": False,
            "sprint": 9,
        }

    @app.get("/api/providers", tags=["providers"])
    def list_providers(request: Request) -> list[dict[str, object]]:
        repository: ProviderRepository = request.app.state.providers
        return repository.list()

    @app.put("/api/providers/{provider_name}", tags=["providers"])
    def put_provider(request: Request, provider_name: str, payload: ProviderPut) -> dict[str, object]:
        if provider_name != payload.name:
            raise DomainError("provider path and payload name differ")
        repository: ProviderRepository = request.app.state.providers
        return repository.put(**payload.model_dump())

    @app.post("/api/providers/{provider_name}/probe", tags=["providers"])
    def probe_provider(request: Request, provider_name: str) -> dict[str, object]:
        pool: ProviderPool = request.app.state.provider_pool
        return pool.probe(provider_name)

    @app.get("/api/audit", tags=["audit"])
    def audit_log(request: Request, limit: int = 200) -> list[dict[str, object]]:
        repository: AuditRepository = request.app.state.audit
        return repository.list(limit=min(max(limit, 1), 1000))

    @app.get("/api/permissions", tags=["permissions"])
    def list_permissions(request: Request) -> list[dict[str, object]]:
        repository: PermissionRepository = request.app.state.permissions
        return repository.list()

    @app.post("/api/permissions", tags=["permissions"])
    def create_permission(request: Request, payload: PermissionGrantCreate) -> dict[str, object]:
        repository: PermissionRepository = request.app.state.permissions
        return repository.grant(**payload.model_dump())

    @app.post("/api/permissions/{grant_id}/revoke", tags=["permissions"])
    def revoke_permission(request: Request, grant_id: str) -> dict[str, bool]:
        repository: PermissionRepository = request.app.state.permissions
        return {"revoked": repository.revoke(grant_id)}

    @app.post("/api/maintenance/backup", tags=["maintenance"])
    def create_backup(request: Request) -> dict[str, object]:
        service: MaintenanceService = request.app.state.maintenance
        return service.backup()

    @app.get("/api/maintenance/export", tags=["maintenance"])
    def export_metadata(request: Request) -> dict[str, object]:
        service: MaintenanceService = request.app.state.maintenance
        return service.export_metadata()

    @app.post("/api/maintenance/diagnostics", tags=["maintenance"])
    def create_diagnostics(request: Request) -> dict[str, object]:
        service: MaintenanceService = request.app.state.maintenance
        return service.diagnostics()

    @app.post("/api/maintenance/restore", tags=["maintenance"])
    def restore_backup(request: Request, payload: RestoreRequest) -> dict[str, object]:
        service: MaintenanceService = request.app.state.maintenance
        return service.restore_backup(payload.filename)

    @app.get("/api/roles", tags=["context"])
    def role_templates() -> dict[str, str]:
        return ROLE_PROMPTS

    @app.get("/api/conventions/{scope}/{scope_id}", tags=["context"])
    def get_convention(request: Request, scope: str, scope_id: str) -> dict[str, object]:
        repository: ConventionRepository = request.app.state.conventions
        return repository.get(scope=scope, scope_id=scope_id)

    @app.put("/api/conventions", tags=["context"])
    def put_convention(request: Request, payload: ConventionPut) -> dict[str, object]:
        repository: ConventionRepository = request.app.state.conventions
        return repository.put(
            scope=payload.scope, scope_id=payload.scope_id, content=payload.content,
            expected_revision=payload.expected_revision,
        )

    @app.post("/api/conventions/{scope}/{scope_id}/refine", tags=["context"])
    def refine_convention(
        request: Request, scope: str, scope_id: str, payload: ConventionRefineRequest
    ) -> dict[str, object]:
        repository: ConventionRepository = request.app.state.conventions
        current = repository.get(scope=scope, scope_id=scope_id)
        projects: ProjectRepository = request.app.state.project_repository
        project_id = payload.project_id
        if scope == "project":
            project_id = scope_id
        elif scope == "task":
            task = request.app.state.task_repository.get(scope_id)
            project_id = task.project_id
        if not project_id:
            raise DomainError("全局 Convention 精炼需要指定一个可供 Worker 运行的项目")
        project = projects.get(project_id)
        project_path = project["host_path"] or project["path"]
        pool: ProviderPool = request.app.state.provider_pool
        return pool.refine_convention(
            scope=scope, scope_id=scope_id, content=current["content"],
            source_revision=current["revision"], provider_name=payload.provider,
            project_path=project_path, instruction=payload.instruction,
        )

    @app.get("/api/tasks/{task_id}/context", tags=["context"])
    def compile_context(request: Request, task_id: str) -> dict[str, object]:
        compiler: ContextCompiler = request.app.state.context_compiler
        return compiler.compile(task_id)

    @app.get("/api/usage", tags=["usage"])
    def usage(request: Request) -> dict[str, object]:
        manager: BudgetManager = request.app.state.budget
        return manager.summary()

    @app.get("/api/system/health", tags=["system"])
    def runtime_health(request: Request) -> dict[str, object]:
        repository: HealthRepository = request.app.state.health_repository
        return repository.status()

    @app.post("/api/system/recover", tags=["system"])
    def recover(request: Request) -> dict[str, object]:
        service: RecoveryService = request.app.state.recovery
        return service.reconcile()

    @app.get("/api/outbox", tags=["events"])
    def outbox_events(request: Request, after: int = 0) -> list[dict[str, object]]:
        repository: OutboxRepository = request.app.state.outbox
        return repository.list(after=after)

    @app.post("/api/outbox/{sequence}/ack", tags=["events"])
    def acknowledge_outbox(request: Request, sequence: int) -> dict[str, bool]:
        repository: OutboxRepository = request.app.state.outbox
        return {"acknowledged": repository.acknowledge(sequence)}

    @app.get("/api/events/stream", tags=["events"])
    async def event_stream(request: Request, after: int = 0, once: bool = False) -> StreamingResponse:
        repository: OutboxRepository = request.app.state.outbox

        async def generate():
            cursor = after
            while True:
                events = repository.list(after=cursor)
                for event in events:
                    cursor = event["sequence"]
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {payload}\n\n"
                if once or await request.is_disconnected():
                    return
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/workers/{worker_id}/rotate", tags=["workforce"])
    def rotate_worker(request: Request, worker_id: str, payload: RotateWorkerRequest) -> dict[str, object]:
        repository: ProjectRepository = request.app.state.project_repository
        return repository.rotate_worker(worker_id, reason=payload.reason)

    @app.post("/api/workers/{worker_id}/rebind", tags=["workforce"])
    def rebind_worker(request: Request, worker_id: str, payload: RebindWorkerRequest) -> dict[str, object]:
        repository: ProjectRepository = request.app.state.project_repository
        return repository.rebind_worker(
            worker_id, provider=payload.provider, reason=payload.reason
        )

    @app.get("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def get_settings(request: Request) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        return RuntimeSettingsView(**repository.get())

    @app.put("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def update_settings(request: Request, payload: RuntimeSettingsUpdate) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        updated = repository.update(payload.values.model_dump(), expected_revision=payload.expected_revision)
        return RuntimeSettingsView(**updated)

    @app.get("/api/scheduler/status", tags=["scheduler"])
    def scheduler_status(request: Request) -> dict[str, object]:
        repository: SchedulerRepository = request.app.state.scheduler_repository
        runtime: SettingsRepository = request.app.state.runtime_settings
        values = runtime.get()["values"]
        runtime_status = repository.status()
        return {
            "runtime": runtime_status,
            "engine": {
                "backend": "embedded-cron",
                "active": request.app.state.settings.embedded_cron and runtime_status["runner_active"],
                "managed_by": "docker" if request.app.state.settings.container_loopback else "process",
                "data_dir": str(request.app.state.settings.data_dir),
            },
            "schedule": schedule_view(values),
            "authorization_required": False,
            "model_invoked": False,
        }

    @app.post("/api/scheduler/tick", tags=["scheduler"])
    def scheduler_tick(request: Request) -> dict[str, object]:
        service: SchedulerService = request.app.state.scheduler_service
        return service.tick(owner="web-api")

    @app.post("/api/projects", response_model=ProjectView, status_code=201, tags=["projects"])
    def create_project(request: Request, payload: ProjectCreate) -> ProjectView:
        repository: ProjectRepository = request.app.state.project_repository
        return ProjectView(**repository.create(
            name=payload.name, path=payload.path, host_path=payload.host_path
        ))

    @app.get("/api/projects", response_model=list[ProjectView], tags=["projects"])
    def list_projects(request: Request) -> list[ProjectView]:
        repository: ProjectRepository = request.app.state.project_repository
        return [ProjectView(**project) for project in repository.list()]

    @app.get("/api/projects/{project_id}", response_model=ProjectView, tags=["projects"])
    def get_project(request: Request, project_id: str) -> ProjectView:
        repository: ProjectRepository = request.app.state.project_repository
        return ProjectView(**repository.get(project_id))

    @app.post("/api/projects/{project_id}/release", response_model=ProjectView, tags=["projects"])
    def release_project(request: Request, project_id: str) -> ProjectView:
        repository: ProjectRepository = request.app.state.project_repository
        return ProjectView(**repository.release(project_id))

    @app.post(
        "/api/tasks/estimate",
        response_model=TaskSizingEstimateResponse,
        tags=["tasks"],
    )
    def estimate_task(payload: TaskSizingEstimateRequest) -> TaskSizingEstimateResponse:
        preview = estimate_task_sizing(TaskSizingInputs(**payload.model_dump()))
        return TaskSizingEstimateResponse(**preview)

    @app.post("/api/tasks", response_model=TaskView, status_code=201, tags=["tasks"])
    def create_task(
        request: Request,
        payload: TaskCreate,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskView:
        repository: TaskRepository = request.app.state.task_repository
        settings_repo: SettingsRepository = request.app.state.runtime_settings
        project_path = payload.project_path
        role_id = None
        if payload.project_id:
            projects: ProjectRepository = request.app.state.project_repository
            binding = projects.resolve_role(payload.project_id, payload.role)
            project_path = binding["project_path"]
            role_id = binding["role_id"]
        assert project_path is not None

        create_kwargs: dict[str, object] = {
            "title": payload.title,
            "objective": payload.objective,
            "project_path": project_path,
            "command": payload.command.model_dump(),
            "verification": [
                item.model_dump(exclude_none=True) for item in payload.verification
            ],
            "max_attempts": (
                payload.max_attempts
                if payload.max_attempts is not None
                else (1 if payload.command.argv else 3)
            ),
            "idempotency_key": idempotency_key,
            "project_id": payload.project_id,
            "role_id": role_id,
            "resource_key": payload.resource_key,
            "network_requirement": payload.network_requirement,
            "provider": payload.provider,
            "quality_profile": payload.quality_profile,
        }

        if payload.sizing_inputs is not None:
            preview = estimate_task_sizing(
                TaskSizingInputs(**payload.sizing_inputs.model_dump())
            )
            if preview["status"] == "needs_planning":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "dispatch gates incomplete; task cannot be created",
                        "code": "needs_planning",
                        "missing_gates": preview["missing_gates"],
                    },
                )
            sizing, execution_budget = _preview_to_persistence(preview)
            estimated_hard_cap = int(execution_budget["total_token_hard_cap"])
            reserved_tokens = int(execution_budget["reserved_tokens"])
            if payload.token_budget is not None and payload.token_budget != estimated_hard_cap:
                reason = (payload.manual_override_reason or "").strip()
                if not reason:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": (
                                "token_budget differs from estimated hard cap; "
                                "manual_override_reason is required"
                            ),
                            "code": "manual_override_required",
                            "total_token_hard_cap": estimated_hard_cap,
                        },
                    )
                if payload.token_budget < reserved_tokens:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": (
                                "token_budget cannot be below reserved_tokens"
                            ),
                            "code": "token_budget_below_reserved",
                            "token_budget": payload.token_budget,
                            "reserved_tokens": reserved_tokens,
                        },
                    )
                execution_budget["estimated_total_token_hard_cap"] = estimated_hard_cap
                execution_budget["total_token_hard_cap"] = payload.token_budget
                create_kwargs["token_budget"] = payload.token_budget
                create_kwargs["manual_override"] = True
                create_kwargs["override_reason"] = reason
            else:
                create_kwargs["token_budget"] = estimated_hard_cap
                create_kwargs["manual_override"] = False
                create_kwargs["override_reason"] = None
            create_kwargs["sizing"] = sizing
            create_kwargs["execution_budget"] = execution_budget
        else:
            default_budget = settings_repo.get()["values"]["task_default_token_budget"]
            create_kwargs["token_budget"] = (
                payload.token_budget
                if payload.token_budget is not None else default_budget
            )

        provider_pool: ProviderPool = request.app.state.provider_pool
        provider_pool.require_ready(payload.provider)
        record = repository.create(**create_kwargs)
        return TaskView.from_record(record)

    @app.get("/api/tasks", response_model=list[TaskView], tags=["tasks"])
    def list_tasks(request: Request) -> list[TaskView]:
        repository: TaskRepository = request.app.state.task_repository
        return [TaskView.from_record(record) for record in repository.list()]

    @app.get("/api/tasks/{task_id}", response_model=TaskView, tags=["tasks"])
    def get_task(request: Request, task_id: str) -> TaskView:
        repository: TaskRepository = request.app.state.task_repository
        return TaskView.from_record(repository.get(task_id))

    @app.post("/api/tasks/{task_id}/drive", response_model=TaskView, tags=["tasks"])
    def drive_task(
        request: Request,
        task_id: str,
        payload: ExpectedRevision,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskView:
        service: TaskService = request.app.state.task_service
        return TaskView.from_record(
            service.drive(
                task_id,
                expected_revision=payload.expected_revision,
                idempotency_key=idempotency_key,
            )
        )

    @app.get("/api/tasks/{task_id}/events", response_model=list[TaskEventView], tags=["tasks"])
    def task_events(request: Request, task_id: str, after: int = 0) -> list[TaskEventView]:
        repository: TaskRepository = request.app.state.task_repository
        return [TaskEventView(**event) for event in repository.events(task_id, after=after)]

    @app.get(
        "/api/tasks/{task_id}/artifacts",
        response_model=list[TaskArtifactView],
        tags=["artifacts"],
    )
    def task_artifacts(request: Request, task_id: str) -> list[TaskArtifactView]:
        repository: TaskRepository = request.app.state.task_repository
        task = repository.get(task_id)
        paths = list(dict.fromkeys(
            str(spec["path"])
            for spec in task.verification
            if spec.get("kind") in {"file_exists", "file_contains"} and spec.get("path")
        ))
        if not paths:
            return []
        project_path = _task_host_path(request, task.project_id, task.project_path)
        pool: ProviderPool = request.app.state.provider_pool
        return [
            TaskArtifactView(**artifact)
            for artifact in pool.inspect_artifacts(project_path=project_path, paths=paths)
        ]

    @app.post("/api/tasks/{task_id}/artifacts/open", tags=["artifacts"])
    def open_task_artifact(
        request: Request, task_id: str, payload: ArtifactOpenRequest
    ) -> dict[str, object]:
        repository: TaskRepository = request.app.state.task_repository
        task = repository.get(task_id)
        declared = {
            str(spec["path"])
            for spec in task.verification
            if spec.get("kind") in {"file_exists", "file_contains"} and spec.get("path")
        }
        if payload.relative_path not in declared:
            raise PolicyViolationError("只能打开任务已声明验证的产物")
        project_path = _task_host_path(request, task.project_id, task.project_path)
        pool: ProviderPool = request.app.state.provider_pool
        return pool.open_artifact(
            project_path=project_path,
            relative_path=payload.relative_path,
            action=payload.action,
        )

    @app.post("/api/tasks/{task_id}/control", response_model=TaskView, tags=["tasks"])
    def control_task(
        request: Request,
        task_id: str,
        payload: TaskControl,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskView:
        service: TaskService = request.app.state.task_service
        return TaskView.from_record(service.control(
            task_id, action=payload.action, reason=payload.reason,
            expected_revision=payload.expected_revision, idempotency_key=idempotency_key,
        ))

    package_static = Path(__file__).resolve().parents[1] / "static"
    source_static = Path(__file__).resolve().parents[3] / "web" / "dist"
    web_dist = package_static if package_static.is_dir() else source_static
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _task_host_path(
    request: Request, project_id: str | None, fallback_path: str
) -> str:
    if not project_id:
        return fallback_path
    project: ProjectRepository = request.app.state.project_repository
    record = project.get(project_id)
    return str(record["host_path"] or record["path"])


def _preview_to_persistence(
    preview: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    sizing = {
        "status": preview["status"],
        "size_class": preview["size_class"],
        "rationale": preview["rationale"],
        "estimated_input_tokens": preview["estimated_input_tokens"],
        "estimated_output_tokens": preview["estimated_output_tokens"],
        "bootstrap_version": preview["bootstrap_version"],
    }
    execution_budget = {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
        "total_token_hard_cap": preview["total_token_hard_cap"],
        "reserved_tokens": preview["reserved_tokens"],
    }
    return sizing, execution_budget
