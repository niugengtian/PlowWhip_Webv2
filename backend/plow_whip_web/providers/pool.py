from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import ProviderUnavailableError, TaskRecord
from plow_whip_web.providers.generic_command import ExecutionResult, GenericCommandProvider
from plow_whip_web.providers.host_bridge import HostBridgeClient
from plow_whip_web.runtime.verification import VerificationResult
from plow_whip_web.store.database import Database
from plow_whip_web.store.provider_repository import ProviderRepository
from plow_whip_web.store.task_repository import TaskRepository


class ProviderPool:
    def __init__(
        self,
        database: Database,
        providers: ProviderRepository,
        tasks: TaskRepository,
        bridge: HostBridgeClient,
        generic: GenericCommandProvider | None = None,
    ) -> None:
        self.database = database
        self.providers = providers
        self.tasks = tasks
        self.bridge = bridge
        self.generic = generic or GenericCommandProvider()

    def require_available(self, name: str) -> dict[str, Any]:
        provider = self.providers.require(name)
        if not provider["enabled"]:
            raise ProviderUnavailableError(f"provider 已停用: {name}")
        if provider["transport"] == "host-bridge" and not self.bridge.token:
            raise ProviderUnavailableError("Host Bridge 未配置，不能调用本机 CLI")
        return provider

    def probe(self, name: str) -> dict[str, Any]:
        provider = self.providers.require(name)
        if not provider["enabled"]:
            return self.providers.record_probe(name, available=False, detail="已停用")
        if provider["transport"] == "container":
            available = provider["adapter"] == "generic-command"
            detail = "容器内置执行器可用" if available else "容器适配器未安装"
        else:
            try:
                available, detail = self.bridge.probe(provider)
            except ProviderUnavailableError as error:
                available, detail = False, str(error)
        return self.providers.record_probe(name, available=available, detail=detail)

    def probe_all(self) -> list[dict[str, Any]]:
        names = [item["name"] for item in self.providers.list() if item["enabled"]]
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(names)))) as pool:
            return list(pool.map(self.probe, names))

    def execute_task(self, task: TaskRecord, *, prompt: str) -> ExecutionResult:
        provider = self.require_available(task.provider)
        if provider["adapter"] == "generic-command":
            return self.generic.execute(Path(task.project_path), task.command)
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id)
        try:
            result = self.bridge.execute(
                provider=provider,
                project_path=worker["host_path"],
                prompt=prompt,
                session_id=worker["external_session_id"],
                timeout_seconds=int(task.command.get("timeout_seconds", 600)),
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
        worker = self.tasks.worker_execution_context(task.worker_id)
        return self.bridge.start_job(
            job_id=job_id,
            provider=provider,
            project_path=worker["host_path"],
            prompt=prompt,
            session_id=worker["external_session_id"],
            timeout_seconds=int(task.command.get("timeout_seconds", 600)),
        )

    def poll_task_job(self, job_id: str) -> dict[str, object]:
        return self.bridge.job_status(job_id)

    def cancel_task_job(self, job_id: str) -> dict[str, object]:
        return self.bridge.cancel_job(job_id)

    def verify_host_task(
        self, task: TaskRecord, execution: ExecutionResult
    ) -> VerificationResult:
        if not task.worker_id:
            raise ProviderUnavailableError("CLI Worker 尚未绑定")
        worker = self.tasks.worker_execution_context(task.worker_id)
        return self.bridge.verify(
            project_path=worker["host_path"],
            execution=execution,
            verification=task.verification,
        )

    def inspect_artifacts(
        self, *, project_path: str, paths: list[str]
    ) -> list[dict[str, object]]:
        return self.bridge.inspect_artifacts(project_path=project_path, paths=paths)

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
    ) -> dict[str, Any]:
        provider = self.require_available(provider_name)
        if "refine_convention" not in provider["capabilities"]:
            raise ProviderUnavailableError(f"provider 不支持 Convention 精炼: {provider_name}")
        prompt = (
            "你是 Convention 编辑器。只输出精炼后的 Convention 正文，不要解释，不要代码围栏。\n\n"
            f"精炼要求：{instruction}\n\n原始 Convention：\n{content}"
        )
        result = self.bridge.execute(
            provider=provider, project_path=project_path, prompt=prompt,
            session_id=None, timeout_seconds=180,
        )
        status = "completed" if result.returncode == 0 and result.stdout.strip() else "failed"
        with self.database.transaction(immediate=True) as connection:
            refinement_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO convention_refinements(
                    id, scope, scope_id, provider, source_revision,
                    input_tokens, output_tokens, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    refinement_id, scope, scope_id, provider_name, source_revision,
                    result.input_tokens, result.output_tokens, status,
                ),
            )
        if status != "completed":
            raise ProviderUnavailableError(result.stderr or "Convention 精炼未返回结果")
        suggestion = _last_text(result.stdout)
        return {
            "id": refinement_id,
            "scope": scope,
            "scope_id": scope_id,
            "source_revision": source_revision,
            "provider": provider_name,
            "suggestion": suggestion,
            "input_tokens": result.input_tokens,
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
