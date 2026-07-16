from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from plow_whip_web import __version__
from plow_whip_web.api.schemas import (
    ExpectedRevision,
    ProjectCreate,
    ProjectView,
    TaskCreate,
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
)
from plow_whip_web.runtime.task_service import TaskService
from plow_whip_web.store.database import Database
from plow_whip_web.store.project_repository import ProjectRepository
from plow_whip_web.store.task_repository import TaskRepository


def create_app(settings: Settings) -> FastAPI:
    settings.prepare()
    database = Database(settings.database_path)
    database.migrate()
    task_repository = TaskRepository(database)
    project_repository = ProjectRepository(database)
    task_service = TaskService(task_repository)

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
            "sprint": 2,
        }

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
        record = repository.create(
            title=payload.title,
            objective=payload.objective,
            project_path=project_path,
            command=payload.command.model_dump(),
            verification=[item.model_dump(exclude_none=True) for item in payload.verification],
            max_attempts=payload.max_attempts,
            token_budget=payload.token_budget,
            idempotency_key=idempotency_key,
            project_id=payload.project_id,
            role_id=role_id,
            resource_key=payload.resource_key,
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

    return app
