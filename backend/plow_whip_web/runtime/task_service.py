from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import (
    HostBridgeRejectedError,
    InvalidTransitionError,
    ProviderUnavailableError,
    TaskRecord,
    TaskStatus,
)
from plow_whip_web.providers.generic_command import ExecutionResult, GenericCommandProvider
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.store.task_repository import (
    task_host_reserved_tokens,
    task_lease_seconds,
    task_sizing_status,
)
from plow_whip_web.runtime.budget import BudgetManager
from plow_whip_web.runtime.context import ContextCompiler
from plow_whip_web.runtime.fault_policy import FaultPolicy
from plow_whip_web.runtime.journal import SessionJournal
from plow_whip_web.runtime.verification import VerificationEngine, VerificationResult
from plow_whip_web.security import CommandPolicy
from plow_whip_web.store.host_job_repository import HostJobRepository
from plow_whip_web.store.task_repository import TaskRepository


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        provider: GenericCommandProvider | None = None,
        verifier: VerificationEngine | None = None,
        budget: BudgetManager | None = None,
        context_compiler: ContextCompiler | None = None,
        journal: SessionJournal | None = None,
        command_policy: CommandPolicy | None = None,
        provider_pool: ProviderPool | None = None,
        host_jobs: HostJobRepository | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or GenericCommandProvider()
        self.verifier = verifier or VerificationEngine()
        self.budget = budget
        self.context_compiler = context_compiler
        self.journal = journal
        self.command_policy = command_policy or CommandPolicy()
        self.provider_pool = provider_pool
        self.host_jobs = host_jobs

    def drive(
        self, task_id: str, *, expected_revision: int, idempotency_key: str
    ) -> TaskRecord:
        pending = self.repository.get(task_id)
        provider_config = (
            self.provider_pool.require_available(pending.provider)
            if self.provider_pool else None
        )
        if not self.provider_pool and pending.provider != self.provider.name:
            raise ProviderUnavailableError(
                f"provider unavailable or not configured: {pending.provider}"
            )
        if pending.provider == self.provider.name:
            self.command_policy.validate(Path(pending.project_path), pending.command)
        host_model = bool(
            provider_config and provider_config["transport"] == "host-bridge"
        )
        estimate = (
            self.provider.estimate_tokens(pending.command)
            if pending.provider == self.provider.name else 0
        )
        reserved_tokens = 0
        if self.budget:
            self.budget.ensure(pending, estimate)
            if host_model:
                if task_sizing_status(pending) == "estimated":
                    remaining = pending.token_budget - pending.tokens_used
                    reserved_tokens = task_host_reserved_tokens(
                        pending, remaining=remaining,
                    )
                else:
                    reserved_tokens = self.budget.host_reservation(pending)
        claim = self.repository.claim(
            task_id, expected_revision=expected_revision,
            idempotency_key=f"{idempotency_key}:claim",
            reserved_tokens=reserved_tokens,
        )
        if not claim.claimed:
            return claim.task
        assert claim.attempt_id is not None
        assert claim.run_id is not None
        context = self.context_compiler.compile(task_id) if self.context_compiler else None
        if self.journal:
            self.journal.append(claim.task.worker_id, {
                "event": "task.started", "task_id": task_id,
                "context_hash": context["content_hash"] if context else None,
            })
        prompt = context["content"] if context else claim.task.objective
        if (
            provider_config
            and provider_config["transport"] == "host-bridge"
            and self.host_jobs
            and self.provider_pool
        ):
            job = self.host_jobs.prepare(
                task_id=task_id, attempt_id=claim.attempt_id,
                run_id=claim.run_id, provider=claim.task.provider,
            )
            try:
                snapshot = self.provider_pool.start_task_job(
                    claim.task, job_id=job["job_id"], prompt=prompt
                )
                self.host_jobs.record(job["job_id"], snapshot)
            except HostBridgeRejectedError as error:
                result = self._handle_host_fault(
                    claim.task,
                    job,
                    {
                        "job_id": job["job_id"],
                        "status": "rejected",
                        "returncode": 126,
                        "last_error": str(error),
                        "input_tokens": 0,
                        "output_tokens": 0,
                    },
                )
                assert result is not None
                return result
            except ProviderUnavailableError as error:
                # Dispatch outcome is unknown. Retain lease and reconcile by stable job_id.
                self.host_jobs.hold(job["job_id"], str(error))
                self.host_jobs.renew(job["job_id"])
            return self.repository.get(task_id)

        project_path = Path(claim.task.project_path).resolve()
        execution = (
            self.provider_pool.execute_task(claim.task, prompt=prompt)
            if self.provider_pool else self.provider.execute(project_path, claim.task.command)
        )
        return self._finish_execution(
            claim.task, claim.attempt_id, claim.run_id, execution,
            idempotency_prefix=idempotency_key,
        )

    def reconcile_host_jobs(self) -> dict[str, Any]:
        if not self.host_jobs or not self.provider_pool:
            return {"checked": 0, "active": 0, "settled": [], "model_invoked": False}
        jobs = self.host_jobs.active()
        active = 0
        settled: list[dict[str, str]] = []
        for job in jobs:
            job_id = job["job_id"]
            task_id = job["task_id"]
            try:
                snapshot = self.provider_pool.poll_task_job(job_id)
                self.host_jobs.record(job_id, snapshot)
            except ProviderUnavailableError as error:
                self.host_jobs.hold(job_id, str(error))
                self.host_jobs.renew(job_id)
                active += 1
                continue
            status = str(snapshot.get("status") or "unknown")
            task = self.repository.get(task_id)
            if status in {"dispatching", "running", "orphan_running", "cancelling"}:
                self.host_jobs.renew(job_id, seconds=task_lease_seconds(task))
                active += 1
                continue
            if status == "completed":
                if task.status is TaskStatus.STOPPING:
                    result = self.repository.finalize_running_cancel(task_id, job_id=job_id)
                    self._settle_host_reservation(task, job, snapshot)
                else:
                    result = self._handle_host_fault(task, job, snapshot)
                    if result is not None:
                        settled.append({"task_id": task_id, "status": result.status.value})
                        continue
                    if task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                        execution = self.provider_pool.bridge.result(snapshot)
                        try:
                            verification = self.provider_pool.verify_host_task(task, execution)
                        except ProviderUnavailableError as error:
                            self.host_jobs.hold(job_id, str(error))
                            self.host_jobs.renew(job_id)
                            active += 1
                            continue
                        result = self._finish_execution(
                            task, job["attempt_id"], job["run_id"], execution,
                            idempotency_prefix=f"host-job:{job_id}",
                            verification=verification,
                        )
                    else:
                        result = task
                        if self.budget and task.status in {
                            TaskStatus.COMPLETED, TaskStatus.TERMINAL_FAILED,
                        }:
                            self.budget.record(
                                task, self.provider_pool.bridge.result(snapshot).as_dict(),
                                provider=task.provider, run_id=job["run_id"],
                            )
                self.host_jobs.consume(job_id)
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            if status == "cancelled":
                if task.status is TaskStatus.STOPPING:
                    result = self.repository.finalize_running_cancel(task_id, job_id=job_id)
                    self._settle_host_reservation(task, job, snapshot)
                    self.host_jobs.consume(job_id)
                else:
                    result = self._handle_host_fault(task, job, snapshot)
                    assert result is not None
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            if status == "interrupted":
                result = self._handle_host_fault(task, job, snapshot)
                assert result is not None
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            self.host_jobs.hold(job_id, f"unknown host job status: {status}")
            self.host_jobs.renew(job_id)
            active += 1
        return {
            "checked": len(jobs), "active": active, "settled": settled,
            "model_invoked": False,
        }

    def _handle_host_fault(
        self, task: TaskRecord, job: dict[str, Any], snapshot: dict[str, Any]
    ) -> TaskRecord | None:
        decision = FaultPolicy.from_host_snapshot(snapshot)
        if decision.action == "verify":
            return None
        execution = {
            "returncode": int(snapshot.get("returncode") or 0),
            "stderr": str(snapshot.get("stderr") or snapshot.get("last_error") or ""),
            "failure_class": decision.failure_class,
            "input_tokens": int(snapshot.get("input_tokens") or 0),
            "output_tokens": int(snapshot.get("output_tokens") or 0),
        }
        return self.repository.finalize_host_fault(
            task.id,
            job_id=job["job_id"],
            attempt_id=job["attempt_id"],
            run_id=job["run_id"],
            action=decision.action,
            failure_class=decision.failure_class,
            reason=decision.reason,
            execution=execution,
            external_session_id=(
                snapshot.get("session_id")
                or snapshot.get("external_session_id")
                or job.get("external_session_id")
            ),
        )

    def _settle_host_reservation(
        self, task: TaskRecord, job: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        if not self.budget or not self.provider_pool:
            return
        execution = self.provider_pool.bridge.result(snapshot).as_dict()
        if int(execution["input_tokens"]) + int(execution["output_tokens"]) > 0:
            self.budget.record(
                task, execution, provider=task.provider, run_id=job["run_id"],
                add_to_task=True,
            )
        else:
            self.budget.release(job["run_id"])

    def control(
        self, task_id: str, *, action: str, reason: str,
        expected_revision: int, idempotency_key: str,
    ) -> TaskRecord:
        task = self.repository.get(task_id)
        if action != "cancel" or task.status not in {
            TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.STOPPING,
        }:
            return self.repository.control(
                task_id, action=action, reason=reason,
                expected_revision=expected_revision, idempotency_key=idempotency_key,
            )
        if not self.host_jobs or not self.provider_pool:
            raise InvalidTransitionError("running provider does not support safe cancellation")
        matching = [
            job for job in self.host_jobs.active()
            if job["task_id"] == task_id and not job["consumed_at"]
        ]
        if not matching:
            raise InvalidTransitionError("active Host Job not found")
        job = matching[-1]
        if task.status is not TaskStatus.STOPPING:
            task = self.repository.request_running_cancel(
                task_id, reason=reason, expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        try:
            snapshot = self.provider_pool.cancel_task_job(job["job_id"])
            self.host_jobs.record(job["job_id"], snapshot)
        except ProviderUnavailableError as error:
            self.host_jobs.hold(job["job_id"], str(error))
            self.host_jobs.renew(job["job_id"])
        return self.repository.get(task_id)

    def _finish_execution(
        self, task: TaskRecord, attempt_id: str, run_id: str,
        execution: ExecutionResult, *, idempotency_prefix: str,
        verification: VerificationResult | None = None,
    ) -> TaskRecord:
        project_path = Path(task.project_path).resolve()
        verifying = self.repository.mark_verifying(
            task.id, expected_revision=task.revision,
            idempotency_key=f"{idempotency_prefix}:verify",
        )
        # quality_profile is deprecated compatibility data. Legacy rows and new
        # tasks intentionally share this single deterministic verification path.
        verification = verification or self.verifier.verify(
            project_path, execution, task.verification
        )
        verification_payload: dict[str, Any] = {
            "passed": verification.passed, "checks": verification.checks,
            "evidence_hash": verification.evidence_hash,
            "failure_fingerprint": _failure_fingerprint(
                execution, verification.checks
            ),
            "summary": verification.summary,
        }
        limits = self.budget.settings.get()["values"] if self.budget else {}
        completed = self.repository.finish(
            task.id, expected_revision=verifying.revision,
            attempt_id=attempt_id, run_id=run_id,
            execution=execution.as_dict(), verification=verification_payload,
            idempotency_key=f"{idempotency_prefix}:finish",
            max_same_failure=limits.get("max_same_failure", 3),
        )
        execution_payload = execution.as_dict()
        if self.budget:
            self.budget.record(
                completed, execution_payload, provider=completed.provider, run_id=run_id
            )
        if self.journal:
            self.journal.append(completed.worker_id, {
                "event": "task.finished", "task_id": task.id,
                "status": completed.status.value,
                "evidence_hash": completed.last_evidence_hash,
                "input_tokens": execution_payload["input_tokens"],
                "output_tokens": execution_payload["output_tokens"],
            })
        return completed


def _failure_fingerprint(
    execution: ExecutionResult, checks: list[dict[str, Any]]
) -> str:
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key != "modified_at_ns"
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    canonical = json.dumps(
        {
            "execution": {
                "returncode": execution.returncode,
                "failure_class": execution.failure_class,
            },
            "checks": stable(checks),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
