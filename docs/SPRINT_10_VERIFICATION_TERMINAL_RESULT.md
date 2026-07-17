# Sprint 10 Verification Terminal Result

- Date: 2026-07-17
- Role: Backend Worker
- Task: `e71d4cad-178f-4aeb-a016-82bcbf5c4b2e`
- Worker: `6b4eeb52-ffc1-441a-9a20-029504252f78`
- Scope: ordinary verification-failure terminal semantics only
- Verdict: **PASS**

## Baseline reproduced

The two QA failures from `docs/SPRINT_10_PREDEPLOY_VERIFICATION_RESULT.md`
were replayed before implementation:

| Test | Baseline result |
| --- | --- |
| `test_repeated_identical_failure_stops_at_guard_threshold` | FAIL: the third drive attempted to claim a `needs_human` task |
| `test_verification_failure_cannot_complete` | FAIL: legacy deterministic command returned `ready` instead of `terminal_failed` |

## Implemented contract

- A normal verification failure retries only while attempts remain and the
  repeated-fingerprint guard permits another result.
- With runtime `max_same_failure=2`, the first failure plus two permitted
  identical repeats produces `ready`, `ready`, then `terminal_failed`.
- A normal failure that cannot retry becomes `terminal_failed` and emits
  `task.terminal_failed`; it does not create a human gate.
- Budget-policy failures remain `needs_human`. A valid overrun evidence record
  can still complete a verified result.
- Tasks with an explicit deterministic `argv` command default to one attempt.
  Tasks without `argv`, which can produce a changed model result, default to
  three attempts. An explicit `max_attempts` value always wins. This decision
  does not branch on a Provider name.
- Replaying the same finish idempotency key does not add task tokens, token
  usage rows, or task events.

## Verification evidence

| Command | Exit code | Result |
| --- | ---: | --- |
| `.venv/bin/python -m pytest tests/test_resilience.py::test_repeated_identical_failure_stops_at_guard_threshold tests/test_tasks_api.py::test_verification_failure_cannot_complete tests/test_tasks_api.py::test_attempt_defaults_follow_execution_capability_and_explicit_override -q` | 0 | 3 passed |
| `.venv/bin/python -m pytest tests/test_resilience.py tests/test_tasks_api.py tests/test_budget_policy.py -q` | 0 | 152 passed: resilience 111, tasks API 7, budget policy 34 |
| `git diff --check` | 0 | PASS |

The specified three-file suite covers the B3 no-evidence over-cap
`needs_human` path, valid overrun-evidence completion, ordinary verification
failure, and idempotent finish accounting/event behavior.

## Changed files

- `backend/plow_whip_web/store/task_repository.py`
- `backend/plow_whip_web/api/schemas.py`
- `backend/plow_whip_web/api/app.py`
- `tests/test_tasks_api.py`
- `tests/test_budget_policy.py`
- `docs/SPRINT_10_VERIFICATION_TERMINAL_RESULT.md`

Pre-existing dirty changes were retained. No migration, quality-profile,
fault-policy, task-service, provider, host, sizing, web, settings, deployment,
commit, push, reset, rollback, or cleanup action was performed.

This result is limited to the requested verification-terminal slice and does
not override the remaining items in the independent pre-deployment report.

SPRINT10_VERIFICATION_TERMINAL_COMPLETE
