from __future__ import annotations

from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import ProviderUnavailableError, TaskRecord
from plow_whip_web.providers.generic_command import GenericCommandProvider
from plow_whip_web.providers.pool import ProviderPool
from plow_whip_web.runtime.budget import BudgetManager
from plow_whip_web.runtime.context import ContextCompiler
from plow_whip_web.runtime.journal import SessionJournal
from plow_whip_web.runtime.verification import VerificationEngine
from plow_whip_web.security import CommandPolicy
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
    ) -> None:
        self.repository = repository
        self.provider = provider or GenericCommandProvider()
        self.verifier = verifier or VerificationEngine()
        self.budget = budget
        self.context_compiler = context_compiler
        self.journal = journal
        self.command_policy = command_policy or CommandPolicy()
        self.provider_pool = provider_pool

    def drive(
        self,
        task_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> TaskRecord:
        pending = self.repository.get(task_id)
        if self.provider_pool:
            self.provider_pool.require_available(pending.provider)
        elif pending.provider != self.provider.name:
            raise ProviderUnavailableError(f"provider unavailable or not configured: {pending.provider}")
        if pending.provider == self.provider.name:
            self.command_policy.validate(Path(pending.project_path), pending.command)
        estimate = self.provider.estimate_tokens(pending.command) if pending.provider == self.provider.name else 0
        if self.budget:
            self.budget.ensure(pending, estimate)
        claim = self.repository.claim(
            task_id,
            expected_revision=expected_revision,
            idempotency_key=f"{idempotency_key}:claim",
        )
        if not claim.claimed:
            return claim.task
        assert claim.attempt_id is not None
        assert claim.run_id is not None
        if claim.task.quality_profile in {"balanced", "strict"}:
            self.repository.record_quality_run(
                task_id=task_id, attempt_id=claim.attempt_id, run_type="plan",
                result={"bounded": True, "objective": claim.task.objective, "model_tokens": 0},
            )
        context = self.context_compiler.compile(task_id) if self.context_compiler else None
        if self.journal:
            self.journal.append(claim.task.worker_id, {
                "event": "task.started", "task_id": task_id,
                "context_hash": context["content_hash"] if context else None,
            })
        project_path = Path(claim.task.project_path).resolve()
        execution = (
            self.provider_pool.execute_task(
                claim.task, prompt=context["content"] if context else claim.task.objective
            )
            if self.provider_pool else self.provider.execute(project_path, claim.task.command)
        )
        verifying = self.repository.mark_verifying(
            task_id,
            expected_revision=claim.task.revision,
            idempotency_key=f"{idempotency_key}:verify",
        )
        verification = self.verifier.verify(project_path, execution, claim.task.verification)
        if claim.task.quality_profile == "strict":
            independent = VerificationEngine().verify(project_path, execution, claim.task.verification)
            self.repository.record_quality_run(
                task_id=task_id, attempt_id=claim.attempt_id, run_type="independent_review",
                result={"evidence_hash": independent.evidence_hash, "passed": independent.passed, "model_tokens": 0},
            )
            if independent.evidence_hash != verification.evidence_hash:
                verification = type(verification)(
                    False, verification.checks, verification.evidence_hash,
                    "independent review disagreement",
                )
        verification_payload: dict[str, Any] = {
            "passed": verification.passed,
            "checks": verification.checks,
            "evidence_hash": verification.evidence_hash,
            "summary": verification.summary,
        }
        limits = self.budget.settings.get()["values"] if self.budget else {}
        completed = self.repository.finish(
            task_id,
            expected_revision=verifying.revision,
            attempt_id=claim.attempt_id,
            run_id=claim.run_id,
            execution=execution.as_dict(),
            verification=verification_payload,
            idempotency_key=f"{idempotency_key}:finish",
            max_same_failure=limits.get("max_same_failure", 3),
            max_no_progress=limits.get("max_no_progress", 3),
        )
        execution_payload = execution.as_dict()
        if self.budget:
            self.budget.record(completed, execution_payload, provider=completed.provider)
        if self.journal:
            self.journal.append(completed.worker_id, {
                "event": "task.finished", "task_id": task_id, "status": completed.status.value,
                "evidence_hash": completed.last_evidence_hash,
                "input_tokens": execution_payload["input_tokens"],
                "output_tokens": execution_payload["output_tokens"],
            })
        return completed
