from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import ProviderUnavailableError, TaskRecord
from plow_whip_web.providers.generic_command import ExecutionResult, GenericCommandProvider
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.verification import VerificationResult
from plow_whip_web.runtime.evidence import snapshot_environment
from plow_whip_web.store.database import Database
from plow_whip_web.store.provider_repository import ProviderRepository
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.task_repository import (
    TaskRepository,
    task_hard_deadline_seconds,
)
from plow_whip_web.runtime.token_ledger import TokenLedger
from plow_whip_web.runtime.model_call_ledger import ModelCallLedger


class ProviderPool:
    def __init__(
        self,
        database: Database,
        providers: ProviderRepository,
        tasks: TaskRepository,
        bridge: HostBridgeClient,
        generic: GenericCommandProvider | None = None,
        token_ledger: TokenLedger | None = None,
        model_calls: ModelCallLedger | None = None,
        settings: SettingsRepository | None = None,
    ) -> None:
        self.database = database
        self.providers = providers
        self.tasks = tasks
        self.bridge = bridge
        self.generic = generic or GenericCommandProvider()
        self.token_ledger = token_ledger
        self.model_calls = model_calls
        self.settings = settings

    def _limits(self) -> dict[str, Any]:
        return self.settings.get()["values"] if self.settings else {}

    @staticmethod
    def _canonical_name(name: str) -> str:
        # Existing TaskSpecs remain replayable after the provider rename.
        return "deepseek" if name == "simple-worker" else name

    def require_available(self, name: str) -> dict[str, Any]:
        canonical = self._canonical_name(name)
        provider = self.providers.require(canonical)
        if not provider["enabled"]:
            raise ProviderUnavailableError(f"provider 已停用: {canonical}")
        if provider.get("circuit_state") == "open":
            raise ProviderUnavailableError(
                f"provider 熔断中: {canonical}; next_probe_at={provider.get('next_probe_at')}"
            )
        if provider["transport"] == "host-bridge" and not self.bridge.token:
            raise ProviderUnavailableError("Host Bridge 未配置，不能调用本机 CLI")
        return provider

    def require_ready(self, name: str) -> dict[str, Any]:
        canonical = self._canonical_name(name)
        provider = self.probe(canonical)
        readiness = provider.get("readiness") or {}
        if provider["status"] != "available":
            raise ProviderUnavailableError(
                f"provider 未通过就绪探测: {canonical}: {provider['reason'] or '不可用'}"
            )
        # Host-bridge providers must be CLI-installed; session resume readiness is
        # reported separately and does not block first-bind dispatch.
        if provider["transport"] == "host-bridge" and not readiness.get("installed", True):
            raise ProviderUnavailableError(f"provider CLI 未安装: {canonical}")
        return provider

    def probe(self, name: str) -> dict[str, Any]:
        name = self._canonical_name(name)
        provider = self.providers.require(name)
        if not provider["enabled"]:
            return self.providers.record_probe(
                name,
                available=False,
                detail="已停用",
                readiness={
                    "installed": False,
                    "cli_probe": "disabled",
                    "session_resume_ready": False,
                    "recent_execution_health": "unknown",
                },
            )
        if not self.providers.probe_allowed(name):
            current = self.providers.require(name)
            return {
                **current,
                "reason": current.get("reason") or "Provider 熔断冷却中",
            }
        limits = self._limits()
        if provider["transport"] == "container":
            available = provider["adapter"] == "generic-command"
            detail = "容器内置执行器可用" if available else "容器适配器未安装"
            readiness = {
                "installed": available,
                "cli_probe": "available" if available else "unavailable",
                "session_resume_ready": True,
                "recent_execution_health": "healthy" if available else "unknown",
            }
        else:
            try:
                available, detail = self.bridge.probe(provider)
            except ProviderUnavailableError as error:
                available, detail = False, str(error)
            health = self._recent_execution_health(name)
            resume_ready = available and health != "tooling_broken"
            readiness = {
                "installed": available,
                "cli_probe": "available" if available else "unavailable",
                "session_resume_ready": resume_ready,
                "recent_execution_health": health,
            }
            # Health is telemetry. ExecutionEpisode owns bounded recovery and
            # replacement, so historical tool aborts must not create a second
            # dispatch gate here.
        return self.providers.record_probe(
            name,
            available=available,
            detail=detail,
            readiness=readiness,
            failure_threshold=int(limits.get("provider_failure_threshold", 3)),
            recovery_successes=int(limits.get("provider_recovery_successes", 1)),
            open_seconds=int(limits.get("provider_open_seconds", 60)),
            failure_class=None if available else "provider_probe_failed",
        )

    def _recent_execution_health(self, provider_name: str) -> str:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT status, last_error, result_json FROM host_jobs
                WHERE provider = ?
                  AND status IN (
                      'completed', 'failed', 'cancelled', 'interrupted',
                      'rejected', 'fault_finalized', 'reconciliation_timeout'
                  )
                ORDER BY updated_at DESC LIMIT 5
                """,
                (provider_name,),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            return "unknown"
        abort_hits = 0
        for row in rows:
            blob = f"{row['last_error'] or ''}\n{row['result_json'] or ''}".lower()
            if any(marker in blob for marker in (
                "no_progress", "tool_aborted", "internal_tool_no_progress",
            )):
                abort_hits += 1
        if abort_hits >= 2:
            return "tooling_broken"
        if abort_hits == 1:
            return "degraded"
        latest = rows[0]
        try:
            result = json.loads(latest["result_json"] or "{}")
        except json.JSONDecodeError:
            result = {}
        if latest["status"] == "completed" and result.get("returncode") == 0:
            return "healthy"
        return "unhealthy"

    def probe_all(
        self, *, unavailable_zones: set[str] | None = None
    ) -> list[dict[str, Any]]:
        unavailable_zones = unavailable_zones or set()
        configured = [
            item for item in self.providers.list()
            if item["enabled"]
        ]
        skipped = [
            {
                **item,
                "status": "unavailable",
                "reason": f"network_zone_unavailable:{item['network_zone']}",
                "probe_skipped": True,
            }
            for item in configured
            if item["network_zone"] in unavailable_zones
        ]
        names = [
            item["name"] for item in configured
            if item["network_zone"] not in unavailable_zones
        ]
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(names)))) as pool:
            return [*list(pool.map(self.probe, names)), *skipped]

    def route_task(
        self,
        task: TaskRecord,
        *,
        zone_availability: dict[str, bool],
    ) -> dict[str, Any] | None:
        if task.provider == "generic-command":
            provider = self.providers.require("generic-command")
            return provider if provider["enabled"] else None
        order = list(task.provider_order or [])
        if task.provider_policy == "pinned" or not task.fallback_enabled:
            candidates = [task.provider]
        elif task.provider_policy == "preferred":
            candidates = [task.provider, *order]
        else:
            candidates = [*order]
            if task.provider not in candidates:
                candidates.insert(0, task.provider)
        for name in dict.fromkeys(candidates):
            provider = self.providers.get(name)
            if provider is None or not provider["enabled"]:
                continue
            if provider["circuit_state"] != "closed":
                continue
            zone = str(provider["network_zone"])
            if zone != "local" and not zone_availability.get(zone, False):
                continue
            if provider["status"] != "available":
                continue
            if "new_session" not in provider["capabilities"]:
                continue
            return provider
        return None

    def execute_task(self, task: TaskRecord, *, prompt: str) -> ExecutionResult:
        provider = self.require_available(task.provider)
        if provider["adapter"] == "generic-command":
            return self.generic.execute(Path(task.project_path), task.command)
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id, task_id=task.id)
        try:
            result = self.bridge.execute(
                provider=provider,
                project_path=worker["host_path"],
                prompt=prompt,
                session_id=worker["external_session_id"],
                timeout_seconds=task_hard_deadline_seconds(task),
            )
        except ProviderUnavailableError as error:
            result = ExecutionResult(
                returncode=126, stdout="", stderr=str(error), duration_ms=0,
                failure_class="provider_unavailable",
            )
        self.tasks.record_worker_result(
            task.worker_id,
            external_session_id=result.external_session_id,
            error=result.stderr[:1000] if result.returncode else None,
            task_id=task.id,
        )
        return result

    def uses_host_job(self, provider_name: str) -> bool:
        return self.require_available(provider_name)["transport"] == "host-bridge"

    def start_task_job(
        self, task: TaskRecord, *, job_id: str, prompt: str
    ) -> dict[str, object]:
        provider = self.require_available(task.provider)
        if provider["transport"] != "host-bridge":
            raise ProviderUnavailableError(f"provider 不使用 Host Job: {task.provider}")
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id, task_id=task.id)
        return self.bridge.start_job(
            job_id=job_id,
            provider=provider,
            project_path=worker["host_path"],
            prompt=prompt,
            session_id=worker["external_session_id"],
            timeout_seconds=task_hard_deadline_seconds(task),
        )

    def poll_task_job(self, job_id: str) -> dict[str, object]:
        return self.bridge.job_status(job_id)

    def cancel_task_job(self, job_id: str) -> dict[str, object]:
        return self.bridge.cancel_job(job_id)

    def read_task_job_output(
        self,
        job_id: str,
        *,
        stdout_offset: int,
        stderr_offset: int,
        limit: int,
        tail_lines: int = 20,
    ) -> dict[str, object]:
        return self.bridge.job_output(
            job_id,
            stdout_offset=stdout_offset,
            stderr_offset=stderr_offset,
            limit=limit,
            tail_lines=tail_lines,
        )

    def verify_host_task(
        self, task: TaskRecord, execution: ExecutionResult
    ) -> VerificationResult:
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id, task_id=task.id)
        return self.bridge.verify(
            project_path=worker["host_path"],
            execution=execution,
            verification=task.verification,
            acceptance=list(task.spec.get("acceptance") or []),
            require_structured_verdict=(
                task.work_item_kind == "verification"
            ),
        )

    def inspect_artifacts(
        self, *, project_path: str, paths: list[str]
    ) -> list[dict[str, object]]:
        return self.bridge.inspect_artifacts(project_path=project_path, paths=paths)

    def snapshot_task_evidence(
        self, task: TaskRecord, *, paths: list[str]
    ) -> dict[str, Any]:
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id, task_id=task.id)
        snapshot = getattr(self.bridge, "snapshot_evidence", None)
        if snapshot is None:
            return snapshot_environment(Path(task.project_path), paths)
        return snapshot(project_path=worker["host_path"], paths=paths)

    def open_artifact(
        self, *, project_path: str, relative_path: str, action: str
    ) -> dict[str, object]:
        return self.bridge.open_artifact(
            project_path=project_path, relative_path=relative_path, action=action
        )

    def refine_convention(
        self,
        *,
        scope: str,
        scope_id: str,
        content: str,
        source_revision: int,
        provider_name: str,
        project_path: str,
        instruction: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            duplicate = connection.execute(
                """
                SELECT * FROM convention_refinements
                WHERE idempotency_key = ? AND status = 'completed'
                """,
                (idempotency_key,),
            ).fetchone()
        finally:
            connection.close()
        if duplicate is not None:
            return {
                "id": duplicate["id"],
                "scope": duplicate["scope"],
                "scope_id": duplicate["scope_id"],
                "source_revision": duplicate["source_revision"],
                "provider": duplicate["provider"],
                "suggestion": duplicate["suggestion"],
                "input_tokens": duplicate["input_tokens"],
                "cached_input_tokens": 0,
                "output_tokens": duplicate["output_tokens"],
                "applied": False,
            }
        provider = self.require_available(provider_name)
        if "refine_convention" not in provider["capabilities"]:
            raise ProviderUnavailableError(f"provider 不支持 Convention 精炼: {provider_name}")
        prompt = (
            "你是 Convention 编辑器。只输出精炼后的 Convention 正文，不要解释，不要代码围栏。\n\n"
            f"精炼要求：{instruction}\n\n原始 Convention：\n{content}"
        )
        task = self.tasks.get(scope_id) if scope == "task" else None
        project_id = task.project_id if task else scope_id if scope == "project" else None
        receipt = self.model_calls.prepare(
            idempotency_key=f"{idempotency_key}:model-call",
            call_kind="convention_refinement",
            provider=provider_name,
            model=str(provider.get("model") or provider_name),
            task=task,
            project_id=project_id,
        ) if self.model_calls else None
        if receipt:
            self.model_calls.dispatched(receipt["call_id"])
        try:
            result = self.bridge.execute(
                provider=provider, project_path=project_path, prompt=prompt,
                session_id=None, timeout_seconds=180,
            )
        except Exception as error:
            if receipt:
                self.model_calls.settle(
                    receipt["call_id"], failed=True, error_class=type(error).__name__
                )
            raise
        status = "completed" if result.returncode == 0 and result.stdout.strip() else "failed"
        suggestion = _last_text(result.stdout) if status == "completed" else None
        if receipt:
            self.model_calls.settle(
                receipt["call_id"], result.as_dict(),
                failed=status != "completed",
                error_class=result.failure_class,
                session_id=result.external_session_id,
            )
        with self.database.transaction(immediate=True) as connection:
            refinement_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO convention_refinements(
                    id, scope, scope_id, provider, source_revision,
                    input_tokens, output_tokens, status, project_id, call_id,
                    idempotency_key, suggestion, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    refinement_id, scope, scope_id, provider_name, source_revision,
                    result.input_tokens, result.output_tokens, status, project_id,
                    receipt["call_id"] if receipt else None, idempotency_key,
                    suggestion, result.stderr[:1000] if status != "completed" else None,
                ),
            )
            if self.token_ledger:
                TokenLedger.record_in_transaction(
                    connection,
                    call_id=receipt["call_id"] if receipt else f"convention-refinement:{refinement_id}",
                    call_kind="convention_refinement",
                    execution=result.as_dict(),
                    task=task,
                    project_id=project_id,
                    provider=provider_name,
                )
        if status != "completed":
            raise ProviderUnavailableError(result.stderr or "Convention 精炼未返回结果")
        return {
            "id": refinement_id,
            "scope": scope,
            "scope_id": scope_id,
            "source_revision": source_revision,
            "provider": provider_name,
            "suggestion": str(suggestion),
            "input_tokens": result.input_tokens,
            "cached_input_tokens": result.cached_input_tokens,
            "output_tokens": result.output_tokens,
            "applied": False,
        }


def _last_text(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                import json
                event = json.loads(line)
                value = _find_output_text(event)
                if value:
                    return value
            except (ValueError, TypeError):
                pass
        else:
            return line
    return output.strip()


def _find_output_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("text", "content", "message", "result", "output_text"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            found = _find_output_text(item)
            if found:
                return found
        for key, item in value.items():
            if key not in {"text", "content", "message", "result", "output_text"}:
                found = _find_output_text(item)
                if found:
                    return found
    elif isinstance(value, list):
        for item in reversed(value):
            found = _find_output_text(item)
            if found:
                return found
    return None
