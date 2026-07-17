# Sprint 10 Hard Budget Terminal Result

## Scope

- Backend-only repository completion policy.
- Implementation: `backend/plow_whip_web/store/task_repository.py`.
- Regression coverage: `tests/test_budget_policy.py`.
- No frontend, deployment, commit, push, reset, rollback, or unrelated dirty-worktree changes.

## Removed mechanism

The old completion exception treated a matching `budget_overrun_evidence` payload as permission to turn an over-cap, verification-passed finish request into `completed`. That evidence-validation and completion-bypass branch was deleted. There is no replacement approval path, configuration switch, provider branch, or second budget decider.

## Terminal contract

`TaskRepository.finish()` remains the single atomic completion decision. It compares total measured usage (`previous tokens_used + current input_tokens + current output_tokens`) with the persisted hard cap. Estimated tasks use `execution_budget.total_token_hard_cap`; legacy fallback tasks retain `task.token_budget` as their persisted cap.

- Usage equal to the cap is allowed and preserves normal completion behavior.
- Usage greater than the cap always settles as task `terminal_failed`, attempt/run `failed`, and stable `reason=budget_exceeded`.
- An over-cap result cannot become `completed` or `needs_human`, and cannot be retried or resumed for more model work.
- Missing, well-formed, and malformed overrun evidence all produce the same terminal result.
- Supplied overrun evidence is persisted only as audit/calibration data and never changes the terminal decision.
- The transaction retains measured usage, verification/artifact evidence, run result data, and releases the worker/lock.
- Replaying the same finish idempotency key returns the terminal record without adding Token usage, events, attempts, or runs, and cannot flip the terminal state.
- Below-cap behavior is unchanged, including rejection of evidence when no overrun occurred.

## Verification

- `.venv/bin/python -m pytest tests/test_budget_policy.py -q`: 30 passed.
- `.venv/bin/python -m pytest -q`: 231 passed.
- `git diff --check`: passed with no output.
