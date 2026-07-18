from __future__ import annotations

import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from plow_whip_web.domain.model import DomainError
from plow_whip_web.runtime.connectivity import ConnectivityProbe, network_available
from plow_whip_web.runtime.recovery import RecoveryService
from plow_whip_web.runtime.task_service import TaskService
from plow_whip_web.store.health_repository import HealthRepository
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.task_repository import TaskRepository
from plow_whip_web.store.goal_repository import GoalRepository
from plow_whip_web.providers.pool import ProviderPool


class SchedulerService:
    """A deterministic database scan. The control path itself invokes no model."""

    model_invoked = False

    def __init__(
        self,
        scheduler: SchedulerRepository,
        settings: SettingsRepository,
        tasks: TaskRepository,
        task_service: TaskService,
        connectivity: ConnectivityProbe | None = None,
        health: HealthRepository | None = None,
        recovery: RecoveryService | None = None,
        provider_pool: ProviderPool | None = None,
        goals: GoalRepository | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.settings = settings
        self.tasks = tasks
        self.task_service = task_service
        self.connectivity = connectivity or ConnectivityProbe()
        self.health = health
        self.recovery = recovery
        self.provider_pool = provider_pool
        self.goals = goals

    def tick(self, *, owner: str | None = None) -> dict[str, Any]:
        try:
            return self._tick(owner=owner)
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                return {"status": "skipped_database_busy", "model_tokens": 0, "reason": "database_locked"}
            raise

    def _tick(self, *, owner: str | None = None) -> dict[str, Any]:
        values = self.settings.get()["values"]
        owner = owner or f"{socket.gethostname()}:{id(self)}"
        lease = self.scheduler.acquire(owner, lease_seconds=values["scheduler_lease_seconds"])
        if not lease.acquired:
            return {"status": "skipped_lease_busy", "model_tokens": 0, "fencing_token": lease.fencing_token}
        host_jobs_result = self.task_service.reconcile_host_jobs()
        recovery_result = self.recovery.reconcile() if self.recovery else {"recovered_tasks": [], "model_invoked": False}
        orchestration = self.goals.advance() if self.goals else {
            "unblocked": [], "replanned": [], "completed_goals": [],
            "blocked_goals": [], "model_invoked": False,
        }
        provider_status = self.provider_pool.probe_all() if self.provider_pool else []
        connectivity = self.connectivity.check()
        health_result = self.health.record(
            connectivity, expected_interval_seconds=values["scheduler_interval_seconds"]
        ) if self.health else {
            "connectivity": connectivity.state, "sleep_resumed": False, "model_invoked": False,
        }
        ready = self.tasks.list_ready(limit=1000)
        runnable = [task for task in ready if network_available(task.network_requirement, connectivity.state)]
        blocked = [task for task in ready if task not in runnable]
        active = self.tasks.in_flight_count()
        available_slots = max(0, values["max_parallel_workers"] - active)
        selected = runnable[:available_slots]
        result: dict[str, Any] = {
            "status": "completed", "scanned": len(ready), "selected": len(selected),
            "active": active, "available_slots": available_slots,
            "completed": [],
            "deferred": [{"task_id": task.id, "reason": f"network:{connectivity.state}"} for task in blocked],
            "orchestration": orchestration,
            "model_tokens": 0, "health": health_result, "recovery": recovery_result,
            "host_jobs": host_jobs_result,
            "providers": [
                {"name": item["name"], "status": item["status"], "model_invoked": False}
                for item in provider_status
            ],
            "fencing_token": lease.fencing_token,
        }
        if values["auto_dispatch"] and selected:
            with ThreadPoolExecutor(max_workers=values["max_parallel_workers"]) as pool:
                futures = {
                    pool.submit(
                        self.task_service.drive,
                        task.id,
                        expected_revision=task.revision,
                        idempotency_key=f"tick:{lease.fencing_token}:{task.id}",
                    ): task.id
                    for task in selected
                }
                for future in as_completed(futures):
                    task_id = futures[future]
                    try:
                        task = future.result()
                        result["completed"].append({"task_id": task.id, "status": task.status.value})
                    except DomainError as error:
                        result["deferred"].append({"task_id": task_id, "reason": str(error)})
                    except Exception as error:  # one task must not stop the global tick
                        result["deferred"].append({"task_id": task_id, "reason": type(error).__name__})
            if self.goals:
                result["orchestration"] = self.goals.advance()
        elif selected:
            result["deferred"].extend(
                {"task_id": task.id, "reason": "auto_dispatch_disabled"} for task in selected
            )
        self.scheduler.finish(lease, result)
        return result
