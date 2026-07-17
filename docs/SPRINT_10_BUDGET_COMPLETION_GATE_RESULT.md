# Sprint 10 Budget Completion Gate Result

## Scope

- Worker slice: Backend Repository completion gate only.
- Modified implementation: `backend/plow_whip_web/store/task_repository.py`.
- Modified tests: `tests/test_budget_policy.py`.
- Host, TaskService, API schemas, domain, runtime, migrations, settings, and web were not changed by this slice.
- Existing dirty worktree changes were preserved; no reset, rollback, commit, or push was performed.

## Dispatch sizing

- Zero-token estimate: `layers=1`, `components=2`, `files=3`, `risk=high`.
- All four dispatch gates were present.
- M budget facts: `reserved_tokens=150000`, `soft_deadline_seconds=480`, `hard_deadline_seconds=1200`, `total_token_hard_cap=225000`.

## Result

`TaskRepository.finish` is the single atomic completion decision for verification and total actual Token usage.

- Actual task Token usage is the prior settled task total plus the current execution's `input_tokens + output_tokens`.
- Estimated tasks use `execution_budget.total_token_hard_cap`; `legacy_fallback` tasks use `task.token_budget`.
- An over-cap result cannot complete or schedule another model retry without valid overrun evidence.
- Missing or invalid overrun evidence moves the task, run, and attempt to `needs_human`, settles actual Token usage, releases locks, and records stable `reason=token_hard_cap_exceeded` in `last_error`, the task event, and the outbox.
- Valid overrun evidence requires passed verification, exact actual Token/cap/evidence-hash facts, a non-empty reason, and `prohibit_new_model_run=true`. It is persisted atomically in `tasks.budget_overrun_evidence_json` and summarized in `task.completed`.
- Evidence supplied without an overrun is not accepted and moves the task to `needs_human` with `reason=invalid_budget_overrun_evidence`.
- Finish replay returns through the existing idempotency event before Token settlement, so task Token totals and `token_usage` are not duplicated.
- Ordinary under-cap verification and retry behavior remains unchanged.

## Verification evidence

- `.venv/bin/python -m pytest tests/test_budget_policy.py -q`: `34 passed`.
- `git diff --check`: passed with no output, including this result document.

The focused tests cover under-cap completion, over-cap `needs_human` without retry, valid evidence completion and persistence, forged actual/cap/hash, `prohibit_new_model_run=false`, empty reason, failed verification with evidence, legacy cap selection, finish idempotency, stable event/outbox reason, and evidence supplied without an overrun.
