from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import (
    EvidenceBaselineMissingError,
    HostBridgeRejectedError,
    InvalidTransitionError,
    ProviderUnavailableError,
    TaskRecord,
    TaskStatus,
)
from plow_whip_web.providers.generic_command import ExecutionResult, GenericCommandProvider
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.store.task_repository import (
    task_lease_seconds,
)
from plow_whip_web.runtime.context import ContextCompiler
from plow_whip_web.runtime.evidence import (
    build_evidence_manifest,
    snapshot_environment,
)
from plow_whip_web.runtime.fault_policy import FaultPolicy
from plow_whip_web.runtime.journal import SessionJournal
from plow_whip_web.runtime.verification import VerificationEngine, VerificationResult
from plow_whip_web.security import CommandPolicy
from plow_whip_web.store.host_job_repository import HostJobRepository
from plow_whip_web.store.project_repository import ProjectRepository
from plow_whip_web.store.task_repository import TaskRepository
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.runtime.token_ledger import TokenLedger
from plow_whip_web.runtime.model_call_ledger import ModelCallLedger


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        provider: GenericCommandProvider | None = None,
        verifier: VerificationEngine | None = None,
        settings: SettingsRepository | None = None,
        token_ledger: TokenLedger | None = None,
        context_compiler: ContextCompiler | None = None,
        journal: SessionJournal | None = None,
        command_policy: CommandPolicy | None = None,
        provider_pool: ProviderPool | None = None,
        host_jobs: HostJobRepository | None = None,
        projects: ProjectRepository | None = None,
        model_calls: ModelCallLedger | None = None,
        role_instances: Any | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider or GenericCommandProvider()
        self.verifier = verifier or VerificationEngine()
        self.settings = settings
        self.token_ledger = token_ledger
        self.context_compiler = context_compiler
        self.journal = journal
        self.command_policy = command_policy or CommandPolicy()
        self.provider_pool = provider_pool
        self.host_jobs = host_jobs
        self.projects = projects
        self.model_calls = model_calls
        self.role_instances = role_instances

    def drive(
        self, task_id: str, *, expected_revision: int, idempotency_key: str
    ) -> TaskRecord:
        pending = self.repository.get(task_id)
        if pending.work_item_kind == "coordination":
            raise InvalidTransitionError("coordination parent is advanced by orchestration, not driven")
        provider_config = (
            self.provider_pool.require_ready(pending.provider)
            if self.provider_pool else None
        )
        if not self.provider_pool and pending.provider != self.provider.name:
            raise ProviderUnavailableError(
                f"provider unavailable or not configured: {pending.provider}"
            )
        if self.role_instances is not None:
            from plow_whip_web.runtime.rule_library import provider_invokes_model

            model_invoked = provider_invokes_model(
                provider=pending.provider,
                provider_config=provider_config,
            )
            if model_invoked:
                self.role_instances.ensure_for_task(pending, model_invoked=True)
            self.role_instances.require_dispatchable(
                task_id=pending.id,
                provider=pending.provider,
                command=pending.command,
                model_invoked=model_invoked,
                expected_task_spec_revision=pending.spec_revision,
            )
        if pending.provider == self.provider.name:
            self.command_policy.validate(Path(pending.project_path), pending.command)
        self._rotate_local_journal(pending)
        claim = self.repository.claim(
            task_id, expected_revision=expected_revision,
            idempotency_key=f"{idempotency_key}:claim",
        )
        if not claim.claimed:
            return claim.task
        assert claim.attempt_id is not None
        assert claim.run_id is not None
        call = self._prepare_executor_call(claim.task, claim.run_id)
        baseline = self._evidence_snapshot(claim.task)
        self.repository.record_evidence_baseline(
            task_id=claim.task.id,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            spec_revision=claim.task.spec_revision,
            baseline=baseline,
        )
        context = self.context_compiler.compile(task_id) if self.context_compiler else None
        if self.journal:
            self.journal.append(claim.task.worker_id, {
                "event": "task.started", "task_id": task_id,
                "context_hash": context["content_hash"] if context else None,
                "spec_revision": claim.task.spec_revision,
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
                if str(snapshot.get("job_id") or "") != job["job_id"]:
                    raise HostBridgeRejectedError("Host Bridge returned a different job identity")
                self.host_jobs.record(job["job_id"], snapshot)
                if call:
                    self.model_calls.dispatched(
                        call["call_id"], host_job_id=job["job_id"],
                        session_id=(
                            snapshot.get("session_id")
                            or snapshot.get("external_session_id")
                        ),
                    )
                self._observe_episode(job, snapshot)
            except HostBridgeRejectedError as error:
                self.host_jobs.dispatch_rejected(job["job_id"], str(error))
                if call:
                    self.model_calls.settle(
                        call["call_id"], failed=True,
                        error_class="dispatch_rejected",
                    )
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
                if call:
                    self.model_calls.unknown(
                        call["call_id"], error_class="dispatch_outcome_unknown"
                    )
            return self.repository.get(task_id)

        project_path = Path(claim.task.project_path).resolve()
        if call:
            self.model_calls.dispatched(call["call_id"])
        try:
            execution = (
                self.provider_pool.execute_task(claim.task, prompt=prompt)
                if self.provider_pool else self.provider.execute(project_path, claim.task.command)
            )
        except Exception as error:
            if call:
                self.model_calls.settle(
                    call["call_id"], failed=True, error_class=type(error).__name__
                )
            raise
        if call:
            self.model_calls.settle(
                call["call_id"], execution.as_dict(),
                failed=execution.returncode != 0,
                error_class=execution.failure_class,
                session_id=execution.external_session_id,
            )
        return self._finish_execution(
            claim.task, claim.attempt_id, claim.run_id, execution,
            idempotency_prefix=idempotency_key,
        )

    def reconcile_host_jobs(self) -> dict[str, Any]:
        if not self.host_jobs or not self.provider_pool:
            return {
                "checked": 0, "active": 0, "settled": [],
                "burn_rate_alerts": [], "model_invoked": False,
            }
        jobs = self.host_jobs.active()
        active = 0
        settled: list[dict[str, str]] = []
        burn_rate_alerts: list[dict[str, Any]] = []
        for job in jobs:
            job_id = job["job_id"]
            task_id = job["task_id"]
            task = self.repository.get(task_id)
            if self.model_calls and job.get("run_id"):
                self._prepare_executor_call(task, job["run_id"])
            try:
                snapshot = self.provider_pool.poll_task_job(job_id)
                self.host_jobs.record(job_id, snapshot)
                if (
                    self.model_calls
                    and job.get("run_id")
                    and str(snapshot.get("status") or "unknown")
                    not in {"unknown", "recovery_hold"}
                ):
                    self.model_calls.dispatched(
                        job["run_id"], host_job_id=job_id,
                        session_id=(
                            snapshot.get("session_id")
                            or snapshot.get("external_session_id")
                        ),
                    )
            except ProviderUnavailableError as error:
                self.host_jobs.hold(job_id, str(error))
                self.host_jobs.renew(job_id)
                if self.model_calls and job.get("run_id"):
                    self.model_calls.unknown(
                        job["run_id"], error_class="dispatch_outcome_unknown"
                    )
                if self.host_jobs.reconciliation_expired(job_id):
                    result = self._finalize_reconciliation_timeout(task, job)
                    settled.append({"task_id": task_id, "status": result.status.value})
                    continue
                watch = self._observe_episode(
                    job,
                    {"status": "recovery_hold"},
                )
                if watch["alert_raised"]:
                    burn_rate_alerts.append(watch)
                active += 1
                continue
            status = str(snapshot.get("status") or "unknown")
            if (
                status in {"unknown", "recovery_hold"}
                and self.host_jobs.reconciliation_expired(job_id)
            ):
                result = self._finalize_reconciliation_timeout(task, job)
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            decision = (
                FaultPolicy.from_host_snapshot(snapshot)
                if status not in {
                    "dispatching", "running", "orphan_running", "cancelling",
                }
                else None
            )
            fault_class = (
                "no_progress"
                if status in {"running", "orphan_running"}
                and FaultPolicy.is_no_progress(snapshot)
                else (
                    decision.failure_class
                    if decision is not None and decision.action != "verify"
                    else None
                )
            )
            watch = self._observe_episode(
                job, snapshot, fault_class=fault_class,
            )
            if watch["alert_raised"]:
                burn_rate_alerts.append(watch)
            if status in {"dispatching", "running", "orphan_running", "cancelling"}:
                if watch["bounded"]:
                    try:
                        cancelled = self.provider_pool.cancel_task_job(job_id)
                        snapshot = {
                            **snapshot,
                            **cancelled,
                            "failure_class": fault_class or "watchdog_boundary",
                        }
                        self.host_jobs.record(job_id, snapshot)
                    except ProviderUnavailableError as error:
                        self.host_jobs.hold(job_id, str(error))
                        self.host_jobs.renew(job_id)
                        active += 1
                        continue
                    result = self._finalize_episode_boundary(
                        task, job, snapshot, watch,
                        failure_class=fault_class or "watchdog_boundary",
                    )
                    settled.append({"task_id": task_id, "status": result.status.value})
                    continue
                self.host_jobs.renew(job_id, seconds=task_lease_seconds(task))
                active += 1
                continue
            if status == "completed":
                self._settle_executor_call(
                    job, snapshot, failed=int(snapshot.get("returncode") or 0) != 0
                )
                if task.status is TaskStatus.STOPPING:
                    result = self.repository.finalize_running_cancel(task_id, job_id=job_id)
                    self._record_host_usage(task, job, snapshot)
                    self.host_jobs.complete_episode(job_id)
                else:
                    result = self._handle_host_fault(
                        task, job, snapshot, decision=decision, watch=watch,
                    )
                    if result is not None:
                        settled.append({"task_id": task_id, "status": result.status.value})
                        continue
                    if task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                        execution = self.provider_pool.bridge.result(snapshot)
                        verify_call = self._prepare_verifier_call(task, job["run_id"])
                        if verify_call:
                            self.model_calls.dispatched(verify_call["call_id"])
                        try:
                            verification = self.provider_pool.verify_host_task(task, execution)
                        except ProviderUnavailableError as error:
                            if verify_call:
                                self.model_calls.unknown(
                                    verify_call["call_id"],
                                    error_class="verification_transport_unknown",
                                )
                            transport_watch = self._observe_episode(
                                job,
                                {
                                    **snapshot,
                                    "failure_class": "transient_transport",
                                    "error_summary": str(error),
                                },
                                fault_class="transient_transport",
                            )
                            if transport_watch["bounded"]:
                                if verify_call:
                                    self.model_calls.settle(
                                        verify_call["call_id"], failed=True,
                                        error_class="verification_transport_timeout",
                                    )
                                result = self._finalize_episode_boundary(
                                    task,
                                    job,
                                    snapshot,
                                    transport_watch,
                                    failure_class="transient_transport",
                                )
                                settled.append({
                                    "task_id": task_id,
                                    "status": result.status.value,
                                })
                                continue
                            self.host_jobs.hold(job_id, str(error))
                            self.host_jobs.renew(job_id)
                            active += 1
                            continue
                        if verify_call:
                            self.model_calls.settle(verify_call["call_id"])
                        try:
                            result = self._finish_execution(
                                task, job["attempt_id"], job["run_id"], execution,
                                idempotency_prefix=f"host-job:{job_id}",
                                verification=verification,
                            )
                            self.host_jobs.complete_episode(job_id)
                        except EvidenceBaselineMissingError:
                            checkpoint = self.host_jobs.terminate_episode(
                                job_id,
                                reason="evidence_baseline_missing_requires_fresh_run",
                                snapshot={
                                    **snapshot,
                                    "failure_class": "evidence_baseline_missing",
                                },
                            )
                            result = self._finalize_host_fault(
                                task,
                                job,
                                snapshot,
                                action="resume",
                                failure_class="evidence_baseline_missing",
                                reason="evidence_baseline_missing_requires_fresh_run",
                                episode=checkpoint,
                            )
                    else:
                        result = task
                self.host_jobs.consume(job_id)
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            if status == "cancelled":
                self._settle_executor_call(job, snapshot, failed=True)
                if task.status is TaskStatus.STOPPING:
                    result = self.repository.finalize_running_cancel(task_id, job_id=job_id)
                    self._record_host_usage(task, job, snapshot)
                    self.host_jobs.complete_episode(job_id)
                    self.host_jobs.consume(job_id)
                else:
                    result = self._handle_host_fault(
                        task, job, snapshot, decision=decision, watch=watch,
                    )
                    assert result is not None
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            if status == "interrupted":
                self._settle_executor_call(job, snapshot, failed=True)
                result = self._handle_host_fault(
                    task, job, snapshot, decision=decision, watch=watch,
                )
                assert result is not None
                settled.append({"task_id": task_id, "status": result.status.value})
                continue
            self.host_jobs.hold(job_id, f"unknown host job status: {status}")
            self.host_jobs.renew(job_id)
            if self.model_calls and job.get("run_id"):
                self.model_calls.unknown(
                    job["run_id"], error_class="dispatch_outcome_unknown"
                )
            active += 1
        return {
            "checked": len(jobs), "active": active, "settled": settled,
            "burn_rate_alerts": burn_rate_alerts, "model_invoked": False,
        }

    def _finalize_reconciliation_timeout(
        self, task: TaskRecord, job: dict[str, Any]
    ) -> TaskRecord:
        assert self.host_jobs is not None
        snapshot = {
            "job_id": job["job_id"],
            "status": "reconciliation_timeout",
            "failure_class": "dispatch_reconciliation_timeout",
            "error_summary": "dispatch reconciliation deadline exceeded",
            "input_tokens": 0,
            "output_tokens": 0,
        }
        checkpoint = self.host_jobs.terminate_episode(
            job["job_id"],
            reason="dispatch_reconciliation_deadline_exceeded",
            snapshot=snapshot,
            force_circuit=True,
        )
        self._settle_executor_call(job, snapshot, failed=True)
        return self._finalize_host_fault(
            task,
            job,
            snapshot,
            action="needs_human",
            failure_class="dispatch_reconciliation_timeout",
            reason="dispatch_reconciliation_deadline_exceeded",
            episode=checkpoint,
        )

    def _handle_host_fault(
        self, task: TaskRecord, job: dict[str, Any], snapshot: dict[str, Any],
        *,
        decision: Any | None = None,
        watch: dict[str, Any] | None = None,
    ) -> TaskRecord | None:
        decision = decision or FaultPolicy.from_host_snapshot(snapshot)
        if decision.action == "verify":
            return None
        watch = watch or self._observe_episode(
            job, snapshot, fault_class=decision.failure_class,
        )
        if decision.action == "needs_human":
            checkpoint = self.host_jobs.terminate_episode(
                job["job_id"],
                reason=decision.reason,
                snapshot={**snapshot, "failure_class": decision.failure_class},
                force_circuit=True,
            )
            return self._finalize_host_fault(
                task, job, snapshot,
                action="needs_human",
                failure_class=decision.failure_class,
                reason=decision.reason,
                episode=checkpoint,
            )
        if watch["bounded"]:
            return self._finalize_episode_boundary(
                task, job, snapshot, watch,
                failure_class=decision.failure_class,
            )
        return self._finalize_host_fault(
            task, job, snapshot,
            action=decision.action,
            failure_class=decision.failure_class,
            reason=decision.reason,
            episode=watch,
        )

    def _finalize_episode_boundary(
        self,
        task: TaskRecord,
        job: dict[str, Any],
        snapshot: dict[str, Any],
        watch: dict[str, Any],
        *,
        failure_class: str,
    ) -> TaskRecord:
        assert self.host_jobs is not None
        checkpoint = self.host_jobs.terminate_episode(
            job["job_id"],
            reason=str(watch["reason"]),
            snapshot={**snapshot, "failure_class": failure_class},
        )
        recovery_action = str(checkpoint["recovery_action"])
        action = (
            "needs_human"
            if recovery_action == "circuit_open"
            else (
                "defer"
                if failure_class in {"provider_capacity", "transient_transport"}
                else "resume"
            )
        )
        reason = f"execution_episode_{recovery_action}:{watch['reason']}"
        result = self._finalize_host_fault(
            task, job, snapshot,
            action=action,
            failure_class=failure_class,
            reason=reason,
            episode=checkpoint,
            rotate_worker_reason=(
                "execution_episode_replacement"
                if recovery_action == "replacement" else None
            ),
        )
        return result

    def _finalize_host_fault(
        self,
        task: TaskRecord,
        job: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        action: str,
        failure_class: str,
        reason: str,
        episode: dict[str, Any],
        rotate_worker_reason: str | None = None,
    ) -> TaskRecord:
        self._settle_executor_call(job, snapshot, failed=True)
        execution = {
            "returncode": int(snapshot.get("returncode") or 0),
            "failure_class": failure_class,
            "input_tokens": int(snapshot.get("input_tokens") or 0),
            "cached_input_tokens": int(snapshot.get("cached_input_tokens") or 0),
            "output_tokens": int(snapshot.get("output_tokens") or 0),
            "attribution_granularity": "turn",
            "value_classification": "unknown",
            "output_ref": snapshot.get("output_ref"),
            "output_bytes": snapshot.get("output_bytes"),
        }
        result = self.repository.finalize_host_fault(
            task.id,
            job_id=job["job_id"],
            attempt_id=job["attempt_id"],
            run_id=job["run_id"],
            action=action,
            failure_class=failure_class,
            reason=reason,
            execution=execution,
            external_session_id=(
                snapshot.get("session_id")
                or snapshot.get("external_session_id")
                or job.get("external_session_id")
            ),
            episode=episode,
            rotate_worker_reason=rotate_worker_reason,
        )
        return result

    def _observe_episode(
        self,
        job: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        fault_class: str | None = None,
    ) -> dict[str, Any]:
        assert self.host_jobs is not None
        limits = (
            self.settings.effective(task_id=str(job["task_id"]))["values"]
            if self.settings else {}
        )
        return self.host_jobs.observe_episode(
            job["job_id"],
            snapshot,
            fault_class=fault_class,
            same_fault_limit=int(limits.get("max_same_failure", 2)),
            zero_progress_limit=int(limits.get("max_no_progress", 3)),
        )

    def _record_host_usage(
        self, task: TaskRecord, job: dict[str, Any], snapshot: dict[str, Any]
    ) -> None:
        if not self.provider_pool:
            return
        execution = self.provider_pool.bridge.result(snapshot).as_dict()
        self._settle_executor_call(
            job, snapshot, failed=int(snapshot.get("returncode") or 0) != 0
        )
        if (
            self.token_ledger
            and int(execution["input_tokens"]) + int(execution["output_tokens"]) > 0
        ):
            self.token_ledger.record(
                execution,
                call_id=job["run_id"],
                task=task,
                provider=task.provider,
                run_id=job["run_id"],
                add_to_task=True,
            )

    def _prepare_executor_call(
        self, task: TaskRecord, run_id: str
    ) -> dict[str, Any] | None:
        if not self.model_calls:
            return None
        context = (
            self.repository.worker_execution_context(task.worker_id, task_id=task.id)
            if task.worker_id else {}
        )
        provider = (
            self.provider_pool.providers.require(task.provider)
            if self.provider_pool else {}
        )
        return self.model_calls.prepare(
            idempotency_key=f"task-run:{run_id}",
            call_id=run_id,
            call_kind="executor",
            provider=task.provider,
            model=str(provider.get("model") or task.provider),
            task=task,
            session_id=context.get("external_session_id"),
            session_generation=context.get("session_generation"),
        )

    def _prepare_verifier_call(
        self, task: TaskRecord, run_id: str
    ) -> dict[str, Any] | None:
        if not self.model_calls:
            return None
        return self.model_calls.prepare(
            idempotency_key=f"task-run:{run_id}:verifier",
            call_kind="verifier",
            provider=task.provider,
            model="deterministic-verifier",
            task=task,
        )

    def _settle_executor_call(
        self, job: dict[str, Any], snapshot: dict[str, Any], *, failed: bool
    ) -> None:
        if not self.model_calls or not job.get("run_id"):
            return
        execution = self.provider_pool.bridge.result(snapshot).as_dict() if self.provider_pool else {}
        self.model_calls.settle(
            job["run_id"], execution, failed=failed,
            error_class=(
                str(snapshot.get("failure_class"))
                if snapshot.get("failure_class") else None
            ),
            session_id=(
                snapshot.get("session_id")
                or snapshot.get("external_session_id")
            ),
        )

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
        if verification is None:
            verify_call = self._prepare_verifier_call(task, run_id)
            if verify_call:
                self.model_calls.dispatched(verify_call["call_id"])
            try:
                verification = self.verifier.verify(
                    project_path, execution, task.verification
                )
            except Exception as error:
                if verify_call:
                    self.model_calls.settle(
                        verify_call["call_id"], failed=True,
                        error_class=type(error).__name__,
                    )
                raise
            if verify_call:
                self.model_calls.settle(verify_call["call_id"])
        baseline = self.repository.evidence_baseline(run_id)
        manifest = build_evidence_manifest(
            task=task,
            attempt_id=attempt_id,
            run_id=run_id,
            call_id=run_id,
            task_revision=verifying.revision,
            baseline=baseline,
            after=self._evidence_snapshot(task),
            execution=execution,
            verification=verification,
        )
        limits = (
            self.settings.effective(
                project_id=task.project_id,
                task_id=task.id,
                role_id=task.role_id,
            )["values"]
            if self.settings else {}
        )
        completed = self.repository.finish(
            task.id, expected_revision=verifying.revision,
            attempt_id=attempt_id, run_id=run_id,
            execution=execution.as_dict(), evidence_manifest=manifest,
            idempotency_key=f"{idempotency_prefix}:finish",
            max_same_failure=limits.get("max_same_failure", 3),
        )
        execution_payload = execution.as_dict()
        if self.journal:
            self.journal.append(completed.worker_id, {
                "event": "task.finished", "task_id": task.id,
                "status": completed.status.value,
                "evidence_hash": completed.last_evidence_hash,
                "input_tokens": execution_payload["input_tokens"],
                "cached_input_tokens": execution_payload["cached_input_tokens"],
                "output_tokens": execution_payload["output_tokens"],
            })
        return completed

    def _evidence_snapshot(self, task: TaskRecord) -> dict[str, Any]:
        paths = [str(path) for path in task.spec["artifacts"]]
        transport = "container"
        worker_context: dict[str, Any] = {}
        if self.provider_pool:
            provider = self.provider_pool.require_available(task.provider)
            transport = str(provider["transport"])
            if provider["transport"] == "host-bridge":
                if task.worker_id:
                    worker_context = self.repository.worker_execution_context(
                        task.worker_id, task_id=task.id
                    )
                snapshot = self.provider_pool.snapshot_task_evidence(task, paths=paths)
            else:
                snapshot = snapshot_environment(Path(task.project_path), paths)
        else:
            snapshot = snapshot_environment(Path(task.project_path), paths)
        snapshot["environment"] = {
            "transport": transport,
            "session_generation": worker_context.get("session_generation"),
            "external_session_id": worker_context.get("external_session_id"),
        }
        return snapshot

    def _rotate_local_journal(self, task: TaskRecord) -> None:
        if not self.projects or not task.project_id or not task.role_id:
            return
        connection = self.repository.database.connect()
        try:
            worker = connection.execute(
                """
                SELECT id, status, session_generation, last_cached_input_tokens,
                       last_context_pressure_tokens, last_context_pressure_reason,
                       last_context_session_generation
                FROM workers
                WHERE project_id = ? AND role_id = ? AND released_at IS NULL
                """,
                (task.project_id, task.role_id),
            ).fetchone()
        finally:
            connection.close()
        if worker is None or worker["status"] != "idle":
            return
        settings_repository = self.journal.settings if self.journal else self.settings
        if settings_repository is None:
            return
        settings = settings_repository.effective(
            project_id=task.project_id,
            task_id=task.id,
            role_id=task.role_id,
        )["values"]
        maximum = int(settings["rotation_max_bytes"])
        persisted = self.journal.current_bytes(worker["id"]) if self.journal else 0
        if persisted >= maximum and self.journal:
            # File Journal rotation is independent from Provider session rotation.
            self.journal.rotate_current(worker["id"])


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
