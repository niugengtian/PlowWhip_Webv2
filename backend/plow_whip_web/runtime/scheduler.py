from __future__ import annotations

import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from plow_whip_web.domain.model import DomainError, TaskStatus
from plow_whip_web.runtime.connectivity import ConnectivityProbe, network_available
from plow_whip_web.runtime.recovery import RecoveryService
from plow_whip_web.runtime.task_service import TaskService
from plow_whip_web.store.health_repository import HealthRepository
from plow_whip_web.store.scheduler_repository import SchedulerRepository
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.task_repository import TaskRepository
from plow_whip_web.store.goal_repository import GoalRepository
from plow_whip_web.store.resilience_repository import ResilienceRepository
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
        resilience: ResilienceRepository | None = None,
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
        self.resilience = resilience

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
        connectivity = self.connectivity.check()
        network = (
            self.resilience.record_network(
                connectivity,
                failure_threshold=int(values["network_failure_threshold"]),
                recovery_successes=int(values["network_recovery_successes"]),
                debounce_seconds=int(values["alert_debounce_seconds"]),
            )
            if self.resilience
            else {
                "zones": {
                    "domestic": "available" if connectivity.domestic_ok else "unavailable",
                    "overseas": "available" if connectivity.overseas_ok else "unavailable",
                },
                "global_offline": connectivity.state == "offline",
                "changed": [],
            }
        )
        zone_availability = {
            zone: (
                state == "available"
                or (
                    state == "unknown"
                    and (
                        connectivity.domestic_ok
                        if zone == "domestic" else connectivity.overseas_ok
                    )
                )
            )
            for zone, state in network["zones"].items()
        }
        unavailable_zones = {
            zone for zone, available in zone_availability.items() if not available
        }
        provider_status = (
            self.provider_pool.probe_all(unavailable_zones=unavailable_zones)
            if self.provider_pool else []
        )
        provider_by_name = {
            str(item["name"]): item for item in provider_status
        }
        provider_incidents = (
            self.resilience.record_provider_health(
                provider_status,
                debounce_seconds=int(values["alert_debounce_seconds"]),
            )
            if self.resilience else []
        )
        unavailable_active_providers = {
            name
            for name, item in provider_by_name.items()
            if str(item.get("network_zone")) in unavailable_zones
        }
        if "deepseek" in unavailable_active_providers:
            unavailable_active_providers.add("simple-worker")
        suspended_active = self.task_service.suspend_active_provider_jobs(
            providers=unavailable_active_providers,
            kind="network_suspended",
            reason=(
                "global_network_unavailable"
                if network["global_offline"]
                else "provider_network_zone_unavailable"
            ),
        )
        host_jobs_result = self.task_service.reconcile_host_jobs()
        recovery_result = self.recovery.reconcile() if self.recovery else {"recovered_tasks": [], "model_invoked": False}
        orchestration = self.goals.advance() if self.goals else {
            "unblocked": [], "replanned": [], "completed_goals": [],
            "blocked_goals": [], "model_invoked": False,
        }
        health_result = self.health.record(
            connectivity, expected_interval_seconds=values["scheduler_interval_seconds"]
        ) if self.health else {
            "connectivity": connectivity.state, "sleep_resumed": False, "model_invoked": False,
        }
        available_providers = {
            str(item["name"])
            for item in provider_status
            if item.get("status") == "available"
            and item.get("circuit_state") == "closed"
            and not item.get("probe_skipped")
        }
        provider_switches: list[dict[str, str]] = []
        if self.resilience and self.provider_pool:
            for task in self.tasks.list(limit=1000):
                if task.status not in {
                    TaskStatus.NETWORK_SUSPENDED,
                    TaskStatus.PROVIDER_SUSPENDED,
                }:
                    continue
                routed = self.provider_pool.route_task(
                    task, zone_availability=zone_availability
                )
                if routed and routed["name"] != task.provider:
                    switched = self.task_service.switch_provider(
                        task,
                        provider=str(routed["name"]),
                        reason="automatic_provider_fallback",
                        idempotency_key=(
                            f"provider-fallback:{task.id}:{task.revision}:"
                            f"{routed['name']}"
                        ),
                    )
                    provider_switches.append(
                        {
                            "task_id": switched.id,
                            "from": task.provider,
                            "to": switched.provider,
                        }
                    )
            resumed = self.resilience.resume_suspended(
                limit=int(values["resume_batch_size"]),
                zone_availability=zone_availability,
                available_providers=available_providers,
            )
        else:
            resumed = []

        ready = self.tasks.list_ready(limit=1000)
        runnable = []
        blocked: list[dict[str, str]] = []
        for task in ready:
            if task.provider == "generic-command":
                if network_available(task.network_requirement, connectivity.state):
                    runnable.append(task)
                else:
                    blocked.append(
                        {"task_id": task.id, "reason": f"network:{connectivity.state}"}
                    )
                continue
            routed = (
                self.provider_pool.route_task(
                    task, zone_availability=zone_availability
                )
                if self.provider_pool else None
            )
            if routed is None:
                current = (
                    self.provider_pool.providers.get(task.provider)
                    if self.provider_pool else None
                )
                current_zone = str((current or {}).get("network_zone") or "overseas")
                suspension = (
                    TaskStatus.NETWORK_SUSPENDED.value
                    if network["global_offline"]
                    or not zone_availability.get(current_zone, False)
                    else TaskStatus.PROVIDER_SUSPENDED.value
                )
                if self.resilience:
                    self.resilience.suspend_task(
                        task.id,
                        kind=suspension,
                        reason=(
                            "global_network_unavailable"
                            if network["global_offline"]
                            else f"no_routable_provider:{current_zone}"
                        ),
                        incident_id=None,
                        idempotency_key=(
                            f"scheduler-suspend:{task.id}:{task.revision}:{suspension}"
                        ),
                    )
                blocked.append({"task_id": task.id, "reason": suspension})
                continue
            if routed["name"] != task.provider:
                old_provider = task.provider
                task = self.task_service.switch_provider(
                    task,
                    provider=str(routed["name"]),
                    reason="automatic_provider_fallback",
                    idempotency_key=(
                        f"provider-fallback:{task.id}:{task.revision}:"
                        f"{routed['name']}"
                    ),
                )
                provider_switches.append(
                    {
                        "task_id": task.id,
                        "from": old_provider,
                        "to": str(routed["name"]),
                    }
                )
            runnable.append(task)
        active = self.tasks.in_flight_count()
        available_slots = max(0, values["max_parallel_workers"] - active)
        selected = runnable[:available_slots]
        result: dict[str, Any] = {
            "status": "completed", "scanned": len(ready), "selected": len(selected),
            "active": active, "available_slots": available_slots,
            "completed": [],
            "deferred": blocked,
            "orchestration": orchestration,
            "model_tokens": 0, "health": health_result, "recovery": recovery_result,
            "host_jobs": host_jobs_result,
            "network": network,
            "suspended_active": suspended_active,
            "resumed": resumed,
            "provider_switches": provider_switches,
            "provider_incidents": provider_incidents,
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
