from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from plow_whip_web.domain.model import HostBridgeRejectedError, ProviderUnavailableError
from plow_whip_web.providers.generic_command import ExecutionResult
from plow_whip_web.store.task_repository import MAX_HARD_DEADLINE_SECONDS
from plow_whip_web.runtime.verification import VerificationResult

_HTTP_TIMEOUT_BUFFER_SECONDS = 20


@dataclass(frozen=True, slots=True)
class HostBridgeClient:
    base_url: str
    token: str | None
    timeout_seconds: int = MAX_HARD_DEADLINE_SECONDS + _HTTP_TIMEOUT_BUFFER_SECONDS

    def probe(self, provider: dict[str, object]) -> tuple[bool, str]:
        payload = self._post("/v1/probe", {
            "adapter": provider["adapter"],
            "executable": provider.get("executable"),
        }, timeout=20)
        return bool(payload.get("available")), str(payload.get("detail", "无探测详情"))

    def execute(
        self,
        *,
        provider: dict[str, object],
        project_path: str,
        prompt: str,
        session_id: str | None,
        timeout_seconds: int,
    ) -> ExecutionResult:
        payload = self._post("/v1/execute", {
            "adapter": provider["adapter"],
            "executable": provider.get("executable"),
            "project_path": project_path,
            "prompt": prompt,
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
        }, timeout=min(
            self.timeout_seconds,
            max(timeout_seconds + _HTTP_TIMEOUT_BUFFER_SECONDS, 10),
        ))
        return ExecutionResult(
            returncode=int(payload.get("returncode", 1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
            duration_ms=int(payload.get("duration_ms", 0)),
            failure_class=payload.get("failure_class"),
            input_tokens=int(payload.get("input_tokens", 0)),
            cached_input_tokens=int(payload.get("cached_input_tokens", 0)),
            output_tokens=int(payload.get("output_tokens", 0)),
            external_session_id=payload.get("session_id"),
            attribution_granularity="turn",
            value_classification="unknown",
        )

    def start_job(
        self, *, job_id: str, provider: dict[str, object], project_path: str,
        prompt: str, session_id: str | None, timeout_seconds: int,
    ) -> dict[str, object]:
        return self._post("/v1/jobs/start", {
            "job_id": job_id,
            "adapter": provider["adapter"],
            "executable": provider.get("executable"),
            "project_path": project_path,
            "prompt": prompt,
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
        }, timeout=20)

    def job_status(self, job_id: str) -> dict[str, object]:
        return self._post("/v1/jobs/status", {"job_id": job_id}, timeout=10)

    def job_output(
        self,
        job_id: str,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
        limit: int = 32_768,
    ) -> dict[str, object]:
        return self._post("/v1/jobs/output", {
            "job_id": job_id,
            "stdout_offset": max(0, stdout_offset),
            "stderr_offset": max(0, stderr_offset),
            "limit": min(max(limit, 1024), 65_536),
        }, timeout=10)

    def cancel_job(self, job_id: str) -> dict[str, object]:
        return self._post("/v1/jobs/cancel", {"job_id": job_id}, timeout=10)

    def verify(
        self, *, project_path: str, execution: ExecutionResult,
        verification: list[dict[str, object]],
    ) -> VerificationResult:
        payload = self._post("/v1/verify", {
            "project_path": project_path,
            "execution": {
                "returncode": execution.returncode,
                "duration_ms": execution.duration_ms,
                "failure_class": execution.failure_class,
                "input_tokens": execution.input_tokens,
                "cached_input_tokens": execution.cached_input_tokens,
                "output_tokens": execution.output_tokens,
                "external_session_id": execution.external_session_id,
            },
            "verification": verification,
        }, timeout=20)
        checks = payload.get("checks")
        if not isinstance(checks, list):
            raise ProviderUnavailableError("Host Bridge 返回了无效的验证结果")
        return VerificationResult(
            passed=bool(payload.get("passed")),
            checks=checks,
            evidence_hash=str(payload.get("evidence_hash") or ""),
            summary=str(payload.get("summary") or ""),
        )

    def inspect_artifacts(
        self, *, project_path: str, paths: list[str]
    ) -> list[dict[str, object]]:
        payload = self._post("/v1/artifacts/inspect", {
            "project_path": project_path,
            "paths": paths,
        }, timeout=30)
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ProviderUnavailableError("Host Bridge 返回了无效的产物索引")
        return artifacts

    def snapshot_evidence(
        self, *, project_path: str, paths: list[str]
    ) -> dict[str, object]:
        payload = self._post("/v1/evidence/snapshot", {
            "project_path": project_path,
            "paths": paths,
        }, timeout=30)
        if not isinstance(payload.get("artifacts"), list):
            raise ProviderUnavailableError("Host Bridge 返回了无效的证据快照")
        return payload

    def open_artifact(
        self, *, project_path: str, relative_path: str, action: str
    ) -> dict[str, object]:
        return self._post("/v1/artifacts/open", {
            "project_path": project_path,
            "relative_path": relative_path,
            "action": action,
        }, timeout=10)

    @staticmethod
    def result(snapshot: dict[str, object]) -> ExecutionResult:
        return ExecutionResult(
            returncode=int(snapshot.get("returncode") or 0),
            stdout=str(snapshot.get("stdout") or ""),
            stderr=str(snapshot.get("stderr") or ""),
            duration_ms=int(snapshot.get("duration_ms") or 0),
            failure_class=snapshot.get("failure_class"),
            input_tokens=int(snapshot.get("input_tokens") or 0),
            cached_input_tokens=int(snapshot.get("cached_input_tokens") or 0),
            output_tokens=int(snapshot.get("output_tokens") or 0),
            external_session_id=snapshot.get("session_id"),
            attribution_granularity="turn",
            value_classification="unknown",
        )

    def _post(self, path: str, payload: dict[str, object], *, timeout: int) -> dict[str, object]:
        if not self.token:
            raise ProviderUnavailableError("Host Bridge 未配置：缺少 PLOW_WHIP_BRIDGE_TOKEN")
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            if path == "/v1/jobs/start" and 400 <= error.code < 500:
                raise HostBridgeRejectedError(
                    f"Host Bridge 拒绝派发: HTTP {error.code} {detail}"
                ) from error
            raise ProviderUnavailableError(f"Host Bridge 拒绝请求: HTTP {error.code} {detail}") from error
        except (URLError, TimeoutError) as error:
            raise ProviderUnavailableError(f"Host Bridge 不可达: {error}") from error
