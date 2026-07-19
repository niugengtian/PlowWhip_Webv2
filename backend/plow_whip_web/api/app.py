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
    ButlerAnswer,
    ButlerConfirm,
    ButlerConversationStart,
    ButlerConversationView,
    ButlerMessageCreate,
    ExpectedRevision,
    ConventionPut,
    ConventionRefineRequest,
    GoalCreate,
    GoalView,
    GlobalButlerRoute,
    ProjectCreate,
    ProjectView,
    ProviderPut,
    PermissionGrantCreate,
    RestoreRequest,
    RuntimeSettingsUpdate,
    RuntimeSettingsOverrideUpdate,
    RuntimeSettingsView,
    RotateWorkerRequest,
    RebindWorkerRequest,
    TaskCreate,
    TaskControl,
    TaskDeleteRequest,
    TaskDeletionEligibilityView,
    TaskDeletionView,
    TaskEventView,
    TaskArtifactView,
    TaskAmendRequest,
    TaskSizingEstimateRequest,
    TaskSizingEstimateResponse,
    TaskView,
)
from plow_whip_web.runtime.sizing import TaskSizingInputs, estimate_task_sizing
from plow_whip_web.runtime.orchestration import plan_goal_work_items
from dataclasses import replace
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
from plow_whip_web.runtime.token_ledger import TokenLedger
from plow_whip_web.runtime.model_call_ledger import ModelCallLedger
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
from plow_whip_web.store.goal_repository import GoalRepository
from plow_whip_web.store.butler_repository import ButlerRepository
from plow_whip_web.store.host_job_repository import HostJobRepository
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.roles import ROLE_PROMPTS
from plow_whip_web.maintenance import MaintenanceService
from plow_whip_web.security import Redactor


def create_app(settings: Settings) -> FastAPI:
    settings.prepare()
    database = Database(settings.database_path)
    database.migrate()
    task_repository = TaskRepository(database)
    goal_repository = GoalRepository(database)
    butler_repository = ButlerRepository(database)
    host_jobs = HostJobRepository(database)
    project_repository = ProjectRepository(database)
    runtime_settings = SettingsRepository(database)
    conventions = ConventionRepository(database)
    token_ledger = TokenLedger(database)
    model_calls = ModelCallLedger(database)
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
        token_ledger=token_ledger,
        model_calls=model_calls,
    )
    maintenance = MaintenanceService(
        settings.data_dir, database, runtime_settings, health_repository, providers
    )
    task_service = TaskService(
        task_repository, settings=runtime_settings, token_ledger=token_ledger,
        context_compiler=context_compiler, journal=journal,
        provider_pool=provider_pool, host_jobs=host_jobs, projects=project_repository,
        model_calls=model_calls,
    )
    scheduler_repository = SchedulerRepository(database)
    scheduler_service = SchedulerService(
        scheduler_repository, runtime_settings, task_repository, task_service,
        connectivity=ConnectivityProbe(), health=health_repository, recovery=recovery,
        provider_pool=provider_pool, goals=goal_repository,
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
    app.state.goal_repository = goal_repository
    app.state.butler_repository = butler_repository
    app.state.host_jobs = host_jobs
    app.state.project_repository = project_repository
    app.state.task_service = task_service
    app.state.runtime_settings = runtime_settings
    app.state.scheduler_repository = scheduler_repository
    app.state.scheduler_service = scheduler_service
    app.state.embedded_cron_runner = embedded_cron_runner
    app.state.conventions = conventions
    app.state.token_ledger = token_ledger
    app.state.model_calls = model_calls
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
            "global_butler_resource_index": True,
            "project_butler_intake": True,
            "human_confirmation_gate": True,
            "parallel_capability_dag": True,
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
        request: Request, scope: str, scope_id: str, payload: ConventionRefineRequest,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=200
        ),
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
            idempotency_key=idempotency_key,
        )

    @app.get("/api/tasks/{task_id}/context", tags=["context"])
    def compile_context(request: Request, task_id: str) -> dict[str, object]:
        compiler: ContextCompiler = request.app.state.context_compiler
        return compiler.compile(task_id)

    @app.get("/api/usage", tags=["usage"])
    def usage(request: Request) -> dict[str, object]:
        ledger: ModelCallLedger = request.app.state.model_calls
        return ledger.summary()

    @app.get("/api/usage/daily", tags=["usage"])
    def usage_daily(
        request: Request,
        start: str | None = None,
        end: str | None = None,
        days: int | None = None,
    ) -> dict[str, object]:
        ledger: ModelCallLedger = request.app.state.model_calls
        try:
            start_day, end_day = ledger.resolve_history_range(
                start=start, end=end, days=days
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return ledger.daily_series(start=start_day, end=end_day)

    @app.get("/api/usage/daily/{day}", tags=["usage"])
    def usage_daily_day(request: Request, day: str) -> dict[str, object]:
        ledger: ModelCallLedger = request.app.state.model_calls
        try:
            target = ModelCallLedger.resolve_history_range(start=day, end=day)[0]
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return ledger.day_breakdown(target)

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
                    payload = json.dumps(
                        {**event, "revision": cursor},
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    yield f"id: {cursor}\nevent: aggregate.updated\ndata: {payload}\n\n"
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

    @app.get("/api/workers/{worker_id}", tags=["workforce"])
    def worker_detail(request: Request, worker_id: str) -> dict[str, object]:
        repository: ProjectRepository = request.app.state.project_repository
        return repository.worker_detail(worker_id)

    @app.get("/api/workers/{worker_id}/stream", tags=["workforce"])
    def worker_stream(
        request: Request,
        worker_id: str,
        cursor: str = "0:0:0",
        limit: int | None = None,
    ) -> dict[str, object]:
        initial_snapshot = cursor == "0:0:0"
        event_after, stdout_offset, stderr_offset = _worker_cursor(cursor)
        projects: ProjectRepository = request.app.state.project_repository
        detail = projects.worker_detail(worker_id)
        task = detail.get("task")
        job = detail.get("host_job")
        runtime: SettingsRepository = request.app.state.runtime_settings
        effective = runtime.effective(
            project_id=str(detail["ownership"]["project_id"]),
            task_id=str(task["id"]) if isinstance(task, dict) else None,
            role_id=str(detail["ownership"]["role_id"]),
        )
        observation_limit = min(
            max(
                int(limit or effective["values"]["observation_max_bytes"]),
                1024,
            ),
            262_144,
        )
        items: list[dict[str, object]] = []
        if isinstance(task, dict):
            events = (
                request.app.state.task_repository.latest_events(
                    str(task["id"]), limit=20
                )
                if initial_snapshot
                else request.app.state.task_repository.events(
                    str(task["id"]), after=event_after
                )[:20]
            )
            for event in events:
                items.append({
                    "kind": "status",
                    "ref": f"task-event:{event['sequence']}",
                    "text": Redactor.redact(
                        f"{event['event_type']}: "
                        f"{json.dumps(event['payload'], ensure_ascii=False)}"
                    )[:2048],
                    "created_at": event["created_at"],
                    "state_revision": event["state_revision"],
                })
            if events:
                event_after = int(events[-1]["sequence"])
        output: dict[str, object] = {"chunks": [], "next_offsets": {}}
        if isinstance(job, dict):
            pool: ProviderPool = request.app.state.provider_pool
            try:
                output = pool.read_task_job_output(
                    str(job["job_id"]),
                    stdout_offset=-1 if initial_snapshot else stdout_offset,
                    stderr_offset=-1 if initial_snapshot else stderr_offset,
                    limit=observation_limit,
                    tail_lines=int(effective["values"]["observation_tail_lines"]),
                )
                chunks = output.get("chunks")
                if isinstance(chunks, list):
                    for chunk in chunks:
                        if not isinstance(chunk, dict):
                            continue
                        items.append({
                            **chunk,
                            "text": Redactor.redact(str(chunk.get("text") or ""))[
                                :observation_limit
                            ],
                        })
            except ProviderUnavailableError as error:
                items.append({
                    "kind": "status",
                    "ref": f"host-job:{job['job_id']}",
                    "text": Redactor.redact(str(error))[:1000],
                    "state_revision": task.get("revision") if isinstance(task, dict) else 0,
                })
            offsets = output.get("next_offsets")
            if isinstance(offsets, dict):
                stdout_offset = int(offsets.get("stdout") or stdout_offset)
                stderr_offset = int(offsets.get("stderr") or stderr_offset)
        return {
            "worker_id": worker_id,
            "job_id": job.get("job_id") if isinstance(job, dict) else None,
            "items": items,
            "next_cursor": f"{event_after}:{stdout_offset}:{stderr_offset}",
            "has_more": bool(output.get("has_more")),
        }

    @app.get("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def get_settings(request: Request) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        return RuntimeSettingsView(**repository.get())

    @app.put("/api/settings", response_model=RuntimeSettingsView, tags=["settings"])
    def update_settings(request: Request, payload: RuntimeSettingsUpdate) -> RuntimeSettingsView:
        repository: SettingsRepository = request.app.state.runtime_settings
        updated = repository.update(payload.values.model_dump(), expected_revision=payload.expected_revision)
        return RuntimeSettingsView(**updated)

    @app.get("/api/settings/effective", tags=["settings"])
    def get_effective_settings(
        request: Request,
        project_id: str | None = None,
        task_id: str | None = None,
        role_id: str | None = None,
    ) -> dict[str, object]:
        repository: SettingsRepository = request.app.state.runtime_settings
        return repository.effective(
            project_id=project_id, task_id=task_id, role_id=role_id
        )

    @app.get("/api/settings/overrides/{scope}/{scope_id}", tags=["settings"])
    def get_settings_override(
        request: Request, scope: str, scope_id: str
    ) -> dict[str, object]:
        repository: SettingsRepository = request.app.state.runtime_settings
        return repository.get_override(scope=scope, scope_id=scope_id)

    @app.put("/api/settings/overrides/{scope}/{scope_id}", tags=["settings"])
    def update_settings_override(
        request: Request,
        scope: str,
        scope_id: str,
        payload: RuntimeSettingsOverrideUpdate,
    ) -> dict[str, object]:
        repository: SettingsRepository = request.app.state.runtime_settings
        return repository.update_override(
            scope=scope,
            scope_id=scope_id,
            values=payload.values,
            expected_revision=payload.expected_revision,
        )

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
            name=payload.name, path=payload.path, host_path=payload.host_path,
            execution_policy=payload.execution_policy,
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

    @app.get("/api/butlers/global/overview", tags=["butlers"])
    def global_butler_overview(
        request: Request, workspace_root: str | None = None
    ) -> dict[str, object]:
        repository: ButlerRepository = request.app.state.butler_repository
        return repository.global_overview(workspace_root=workspace_root)

    @app.post(
        "/api/butlers/global/route",
        response_model=ButlerConversationView,
        status_code=201,
        tags=["butlers"],
    )
    def route_global_butler_command(
        request: Request,
        payload: GlobalButlerRoute,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=200
        ),
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        draft = payload.model_dump(exclude={"project_id", "source_type", "source_id", "instruction"})
        record = repository.start_project_conversation(
            project_id=payload.project_id,
            source_type="global_butler",
            source_id=payload.source_id,
            instruction=payload.instruction,
            draft=draft,
            idempotency_key=idempotency_key,
        )
        return ButlerConversationView(**record)

    @app.post(
        "/api/projects/{project_id}/butler/conversations",
        response_model=ButlerConversationView,
        status_code=201,
        tags=["butlers"],
    )
    def start_project_butler_conversation(
        request: Request,
        project_id: str,
        payload: ButlerConversationStart,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=200
        ),
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        draft = payload.model_dump(exclude={"source_type", "source_id", "instruction"})
        record = repository.start_project_conversation(
            project_id=project_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
            instruction=payload.instruction,
            draft=draft,
            idempotency_key=idempotency_key,
        )
        return ButlerConversationView(**record)

    @app.get(
        "/api/projects/{project_id}/butler/conversations",
        response_model=list[ButlerConversationView],
        tags=["butlers"],
    )
    def list_project_butler_conversations(
        request: Request, project_id: str
    ) -> list[ButlerConversationView]:
        repository: ButlerRepository = request.app.state.butler_repository
        return [
            ButlerConversationView(**record)
            for record in repository.list_project(project_id)
        ]

    @app.get(
        "/api/projects/{project_id}/butler/conversations/{conversation_id}",
        response_model=ButlerConversationView,
        tags=["butlers"],
    )
    def get_project_butler_conversation(
        request: Request, project_id: str, conversation_id: str
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        record = repository.get(conversation_id)
        if record["project_id"] != project_id:
            raise NotFoundError(f"butler conversation not found: {conversation_id}")
        return ButlerConversationView(**record)

    @app.post(
        "/api/projects/{project_id}/butler/conversations/{conversation_id}/messages",
        response_model=ButlerConversationView,
        tags=["butlers"],
    )
    def post_project_butler_message(
        request: Request,
        project_id: str,
        conversation_id: str,
        payload: ButlerMessageCreate,
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        current = repository.get(conversation_id)
        if current["project_id"] != project_id:
            raise NotFoundError(f"butler conversation not found: {conversation_id}")
        return ButlerConversationView(**repository.post_message(
            conversation_id,
            expected_revision=payload.expected_revision,
            content=payload.content,
            sender_type=payload.sender_type,
            field=payload.field,
        ))

    @app.post(
        "/api/projects/{project_id}/butler/conversations/{conversation_id}/answers",
        response_model=ButlerConversationView,
        tags=["butlers"],
    )
    def answer_project_butler(
        request: Request,
        project_id: str,
        conversation_id: str,
        payload: ButlerAnswer,
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        current = repository.get(conversation_id)
        if current["project_id"] != project_id:
            raise NotFoundError(f"butler conversation not found: {conversation_id}")
        return ButlerConversationView(**repository.answer(
            conversation_id,
            expected_revision=payload.expected_revision,
            field=payload.field,
            values=payload.values,
            sender_type=payload.sender_type,
        ))

    @app.post(
        "/api/projects/{project_id}/butler/conversations/{conversation_id}/confirm",
        response_model=ButlerConversationView,
        tags=["butlers"],
    )
    def confirm_project_butler(
        request: Request,
        project_id: str,
        conversation_id: str,
        payload: ButlerConfirm,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=200
        ),
    ) -> ButlerConversationView:
        repository: ButlerRepository = request.app.state.butler_repository
        current = repository.get(conversation_id)
        if current["project_id"] != project_id:
            raise NotFoundError(f"butler conversation not found: {conversation_id}")
        spec = current["spec"]
        sizing_inputs = spec.get("sizing_inputs") or {
            "layers_touched": 1,
            "components_touched": 1,
            "estimated_files_changed": 1,
            "has_migration": False,
            "has_deploy": False,
            "verification_commands_count": max(1, len(spec.get("verification") or [])),
            "estimated_verification_seconds": 60,
            "external_dependencies_count": 0,
            "risk_level": "low",
            "independent_review_required": False,
            "gate_artifact": True,
            "gate_boundary": True,
            "gate_verification": True,
            "gate_dependency": True,
        }
        goal_payload = GoalCreate.model_validate({
            "title": spec["title"],
            "objective": spec["objective"],
            "project_id": project_id,
            "provider": spec.get("provider") or "cursor",
            "role_providers": spec.get("role_providers") or {},
            "network_requirement": spec.get("network_requirement") or "none",
            "verification": spec.get("verification") or [
                {"kind": "exit_code", "expected": 0}
            ],
            "scope": list(dict.fromkeys([
                *(spec.get("scope") or []),
                *(spec.get("boundaries") or []),
            ])),
            "acceptance": spec["acceptance"],
            "artifacts": spec.get("artifacts") or [],
            "constraints": spec.get("constraints") or [],
            "deadline": spec.get("deadline"),
            "sizing_inputs": sizing_inputs,
            "command": spec.get("command"),
            "plan_items": spec.get("plan_items"),
        })
        goal = _create_goal_record(
            request, goal_payload, f"butler-confirm:{conversation_id}:goal"
        )
        return ButlerConversationView(**repository.mark_dispatched(
            conversation_id,
            expected_revision=payload.expected_revision,
            proposal_hash=payload.proposal_hash,
            goal_id=goal.id,
        ))

    @app.post(
        "/api/tasks/estimate",
        response_model=TaskSizingEstimateResponse,
        tags=["tasks"],
    )
    def estimate_task(payload: TaskSizingEstimateRequest) -> TaskSizingEstimateResponse:
        preview = estimate_task_sizing(TaskSizingInputs(**payload.model_dump()))
        return TaskSizingEstimateResponse(**preview)

    @app.post("/api/goals", response_model=GoalView, status_code=201, tags=["goals"])
    def create_goal(
        request: Request,
        payload: GoalCreate,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> GoalView:
        return _create_goal_record(request, payload, idempotency_key)

    @app.get("/api/goals", response_model=list[GoalView], tags=["goals"])
    def list_goals(request: Request) -> list[GoalView]:
        goals: GoalRepository = request.app.state.goal_repository
        return [GoalView(**item) for item in goals.list()]

    @app.get("/api/goals/{goal_id}", response_model=GoalView, tags=["goals"])
    def get_goal(request: Request, goal_id: str) -> GoalView:
        goals: GoalRepository = request.app.state.goal_repository
        return GoalView(**goals.get(goal_id))

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

        create_kwargs: dict[str, object] = {
            "title": payload.title,
            "objective": payload.objective,
            "project_path": project_path,
            "command": payload.command.model_dump(),
            "verification": [
                item.model_dump(exclude_none=True) for item in payload.verification
            ],
            "scope": payload.scope,
            "acceptance": payload.acceptance,
            "artifacts": payload.artifacts,
            "constraints": payload.constraints,
            "deadline": payload.deadline.model_dump() if payload.deadline else None,
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
            sizing, execution_policy = _preview_to_persistence(preview)
            create_kwargs["sizing"] = sizing
            create_kwargs["execution_policy"] = execution_policy

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

    @app.post("/api/tasks/{task_id}/amend", response_model=TaskView, tags=["tasks"])
    def amend_task(
        request: Request,
        task_id: str,
        payload: TaskAmendRequest,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=8, max_length=200
        ),
    ) -> TaskView:
        repository: TaskRepository = request.app.state.task_repository
        values = payload.model_dump(exclude={"expected_revision", "reason"})
        values["verification"] = [
            item.model_dump(exclude_none=True) for item in payload.verification
        ]
        values["deadline"] = payload.deadline.model_dump()
        return TaskView.from_record(repository.amend_spec(
            task_id,
            spec=values,
            reason=payload.reason,
            expected_revision=payload.expected_revision,
            idempotency_key=idempotency_key,
        ))

    @app.get(
        "/api/tasks/{task_id}/deletion-eligibility",
        response_model=TaskDeletionEligibilityView,
        tags=["tasks"],
    )
    def task_deletion_eligibility(
        request: Request, task_id: str
    ) -> TaskDeletionEligibilityView:
        repository: TaskRepository = request.app.state.task_repository
        return TaskDeletionEligibilityView(**repository.deletion_eligibility(task_id))

    @app.delete(
        "/api/tasks/{task_id}", response_model=TaskDeletionView, tags=["tasks"]
    )
    def delete_task(
        request: Request,
        task_id: str,
        payload: TaskDeleteRequest,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ) -> TaskDeletionView:
        repository: TaskRepository = request.app.state.task_repository
        return TaskDeletionView(**repository.delete(
            task_id,
            expected_revision=payload.expected_revision,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        ))

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
        paths = [str(path) for path in task.spec["artifacts"]]
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
        declared = {str(path) for path in task.spec["artifacts"]}
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


def _create_goal_record(
    request: Request, payload: GoalCreate, idempotency_key: str
) -> GoalView:
    """One dispatch path for confirmed Butler proposals and compatibility clients."""
    projects: ProjectRepository = request.app.state.project_repository
    goals: GoalRepository = request.app.state.goal_repository
    provider_pool: ProviderPool = request.app.state.provider_pool
    model_calls: ModelCallLedger = request.app.state.model_calls
    project = projects.get(payload.project_id)
    sizing_inputs = TaskSizingInputs(**payload.sizing_inputs.model_dump())
    route_call = model_calls.prepare(
        idempotency_key=f"{idempotency_key}:router",
        call_kind="router",
        provider="internal",
        model="butler-v2",
        project_id=payload.project_id,
    )
    plan_call = model_calls.prepare(
        idempotency_key=f"{idempotency_key}:planner",
        call_kind="butler_planner",
        provider="internal",
        model="butler-v2",
        project_id=payload.project_id,
    )
    model_calls.dispatched(route_call["call_id"])
    model_calls.dispatched(plan_call["call_id"])
    try:
        plan = plan_goal_work_items(
            title=payload.title,
            objective=payload.objective,
            sizing_inputs=sizing_inputs,
            structured_items=payload.plan_items,
            execution_policy=project["execution_policy"],
            role_providers=payload.role_providers,
        )
        providers = {
            item.provider or payload.provider
            for item in plan.items
        }
        for provider in sorted(providers):
            provider_pool.require_ready(provider)
    except Exception as error:
        model_calls.settle(
            route_call["call_id"], failed=True, error_class=type(error).__name__
        )
        model_calls.settle(
            plan_call["call_id"], failed=True, error_class=type(error).__name__
        )
        raise
    model_calls.settle(route_call["call_id"])
    model_calls.settle(plan_call["call_id"])
    if plan.status == "needs_planning":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "dispatch gates incomplete; goal cannot be created",
                "code": "needs_planning",
                "missing_gates": list(plan.missing_gates),
            },
        )
    record = goals.create_with_plan(
        title=payload.title,
        objective=payload.objective,
        project_id=payload.project_id,
        project_path=project["path"],
        provider=payload.provider,
        plan=plan,
        sizing_inputs=payload.sizing_inputs.model_dump(),
        verification=[
            item.model_dump(exclude_none=True) for item in payload.verification
        ],
        scope=payload.scope,
        acceptance=payload.acceptance,
        artifacts=payload.artifacts,
        constraints=payload.constraints,
        deadline=payload.deadline.model_dump() if payload.deadline else None,
        idempotency_key=idempotency_key,
        network_requirement=payload.network_requirement,
        command=(
            payload.command.model_dump()
            if payload.command is not None
            else {
                "argv": None,
                "timeout_seconds": 60,
                "output_limit_bytes": 131_072,
            }
        ),
    )
    return GoalView(**record)


def _preview_to_persistence(
    preview: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    sizing = {
        "status": preview["status"],
        "size_class": preview["size_class"],
        "rationale": preview["rationale"],
        "bootstrap_version": preview["bootstrap_version"],
    }
    execution_policy = {
        "soft_deadline_seconds": preview["soft_deadline_seconds"],
        "hard_deadline_seconds": preview["hard_deadline_seconds"],
        "max_turns": preview["max_turns"],
        "max_attempts": preview["max_attempts"],
        "verification_timeout_seconds": preview["verification_timeout_seconds"],
        "progress_extension_seconds": preview["progress_extension_seconds"],
    }
    return sizing, execution_policy


def _worker_cursor(value: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in value.split(":"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid worker stream cursor") from error
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise HTTPException(status_code=400, detail="invalid worker stream cursor")
    return parts
