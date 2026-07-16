from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from plow_whip_web import __version__
from plow_whip_web.api.schemas import (
    ExpectedRevision,
    ConventionPut,
    ProjectCreate,
    ProjectView,
    PermissionGrantCreate,
    RestoreRequest,
    RuntimeSettingsUpdate,
    RuntimeSettingsView,
    RotateWorkerRequest,
    TaskCreate,
    TaskControl,
    TaskEventView,
    TaskView,
)
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
from plow_whip_web.system_scheduler import SystemScheduler
from plow_whip_web.roles import ROLE_PROMPTS
from plow_whip_web.maintenance import MaintenanceService


def create_app(settings: Settings) -> FastAPI:
    settings.prepare()
    database = Database(settings.database_path)
    database.migrate()
    task_repository = TaskRepository(database)
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
    maintenance = MaintenanceService(
        settings.data_dir, database, runtime_settings, health_repository, providers
    )
    task_service = TaskService(
        task_repository, budget=budget, context_compiler=context_compiler, journal=journal
    )
    scheduler_repository = SchedulerRepository(database)
    scheduler_service = SchedulerService(
        scheduler_repository, runtime_settings, task_repository, task_service,
        connectivity=ConnectivityProbe(), health=health_repository, recovery=recovery,
    )
    system_scheduler = SystemScheduler(settings.data_dir)

    app = FastAPI(
        title="plow-whip Web v2",
        version=__version__,
        description="Quality-first unattended workflow control plane",
    )
    app.state.settings = settings
    app.state.database = database
    app.state.task_repository = task_repository
    app.state.project_repository = project_repository
    app.state.task_service = task_service
    app.state.runtime_settings = runtime_settings
    app.state.scheduler_repository = scheduler_repository
    app.state.scheduler_service = scheduler_service
    app.state.system_scheduler = system_scheduler
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
            "system_scheduler": system_scheduler.plan().as_dict(),
            "sprint": 6,
        }

    @app.get("/api/providers", tags=["providers"])
    def list_providers(request: Request) -> list[dict[str, object]]:
        repository: ProviderRepository = request.app.state.providers
        return repository.list()

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

    @app.get("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def get_settings(request: Request) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        return RuntimeSettingsView(**repository.get())

    @app.put("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def update_settings(request: Request, payload: RuntimeSettingsUpdate) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        updated = repository.update(payload.values.model_dump(), expected_revision=payload.expected_revision)
        if updated["values"]["system_scheduler_authorized"]:
            permissions_repository: PermissionRepository = request.app.state.permissions
            target = request.app.state.system_scheduler.plan().target or "unsupported"
            if not permissions_repository.is_allowed(
                project_id=None, capability="system_scheduler", resource=target
            ):
                permissions_repository.grant(
                    project_id=None, capability="system_scheduler", resource=target,
                    decision="allow", reason="authorized from Settings",
                )
        return RuntimeSettingsView(**updated)

    @app.get("/api/scheduler/status", tags=["scheduler"])
    def scheduler_status(request: Request) -> dict[str, object]:
        repository: SchedulerRepository = request.app.state.scheduler_repository
        manager: SystemScheduler = request.app.state.system_scheduler
        runtime: SettingsRepository = request.app.state.runtime_settings
        values = runtime.get()["values"]
        target = manager.plan().target or "unsupported"
        permissions_repository: PermissionRepository = request.app.state.permissions
        authorized = values["system_scheduler_authorized"] and permissions_repository.is_allowed(
            project_id=None, capability="system_scheduler", resource=target
        )
        return {
            "runtime": repository.status(), "system": manager.plan().as_dict(),
            "authorization_required": not authorized,
            "model_invoked": False,
        }

    @app.post("/api/scheduler/tick", tags=["scheduler"])
    def scheduler_tick(request: Request) -> dict[str, object]:
        service: SchedulerService = request.app.state.scheduler_service
        return service.tick(owner="web-api")

    @app.post("/api/scheduler/install", tags=["scheduler"])
    def scheduler_install(request: Request) -> dict[str, object]:
        manager: SystemScheduler = request.app.state.system_scheduler
        runtime: SettingsRepository = request.app.state.runtime_settings
        values = runtime.get()["values"]
        target = manager.plan().target or "unsupported"
        permissions_repository: PermissionRepository = request.app.state.permissions
        authorized = values["system_scheduler_authorized"] and permissions_repository.is_allowed(
            project_id=None, capability="system_scheduler", resource=target
        )
        return manager.install(
            interval_seconds=values["scheduler_interval_seconds"],
            authorized=authorized,
        )

    @app.post("/api/projects", response_model=ProjectView, status_code=201, tags=["projects"])
    def create_project(request: Request, payload: ProjectCreate) -> ProjectView:
        repository: ProjectRepository = request.app.state.project_repository
        return ProjectView(**repository.create(name=payload.name, path=payload.path))

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

    @app.post("/api/tasks", response_model=TaskView, status_code=201, tags=["tasks"])
    def create_task(
        request: Request,
        payload: TaskCreate,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskView:
        repository: TaskRepository = request.app.state.task_repository
        project_path = payload.project_path
        role_id = None
        if payload.project_id:
            projects: ProjectRepository = request.app.state.project_repository
            binding = projects.resolve_role(payload.project_id, payload.role)
            project_path = binding["project_path"]
            role_id = binding["role_id"]
        assert project_path is not None
        default_budget = runtime_settings.get()["values"]["task_default_token_budget"]
        record = repository.create(
            title=payload.title,
            objective=payload.objective,
            project_path=project_path,
            command=payload.command.model_dump(),
            verification=[item.model_dump(exclude_none=True) for item in payload.verification],
            max_attempts=payload.max_attempts,
            token_budget=payload.token_budget if payload.token_budget is not None else default_budget,
            idempotency_key=idempotency_key,
            project_id=payload.project_id,
            role_id=role_id,
            resource_key=payload.resource_key,
            network_requirement=payload.network_requirement,
            provider=payload.provider,
            quality_profile=payload.quality_profile,
        )
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

    @app.post("/api/tasks/{task_id}/control", response_model=TaskView, tags=["tasks"])
    def control_task(
        request: Request,
        task_id: str,
        payload: TaskControl,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskView:
        repository: TaskRepository = request.app.state.task_repository
        return TaskView.from_record(repository.control(
            task_id, action=payload.action, reason=payload.reason,
            expected_revision=payload.expected_revision, idempotency_key=idempotency_key,
        ))

    package_static = Path(__file__).resolve().parents[1] / "static"
    source_static = Path(__file__).resolve().parents[3] / "web" / "dist"
    web_dist = package_static if package_static.is_dir() else source_static
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app
