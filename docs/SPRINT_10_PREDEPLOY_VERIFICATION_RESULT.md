# Sprint 10 Pre-deploy Verification Result

- Date: 2026-07-17
- Role: independent QA / verification-only
- Task: `84c104f6-55fb-4f87-b10a-07c174ea3b58`
- Worker: `7670722c-ebf3-49c2-85d4-effe30ad647f`
- Verdict: **CHANGES_REQUIRED**

The current worktree is not ready for pre-deployment acceptance. The backend
full-suite gate has 6 failures, including cross-slice regressions in the
existing migration, quality-profile, retry, and terminal-state contracts. No
implementation, repair, formatting, reset, cleanup, commit, push, deployment,
or Docker refresh was performed.

## Worktree baseline

The required preflight inspection was performed before the gates:

| Command | Exit code | Evidence |
| --- | ---: | --- |
| `git status --short` | 0 | 19 modified tracked files and 16 pre-existing untracked files; this result document did not yet exist |
| `git diff --name-only` | 0 | 19 modified tracked files |
| `git diff --cached --name-only` | 0 | Empty; no staged files |

All pre-existing dirty changes were retained. The post-test status was
identical to this baseline, so the test commands did not create an unexpected
repository file.

## Commands and gate results

Commands were run in the requested order.

| Gate | Command | Exit code | Result |
| --- | --- | ---: | --- |
| 1 | `git diff --check` | 0 | PASS; no whitespace errors |
| 2 | `.venv/bin/python -m pytest -q` | 1 | **CHANGES_REQUIRED**; 235 tests collected, 229 passed, 6 failed |
| Count evidence | `.venv/bin/python -m pytest --collect-only` | 0 | `235 tests collected in 0.17s` |
| 3 | `cd web && pnpm test && pnpm run typecheck && pnpm run lint && pnpm run build` | 0 | PASS; every command in the `&&` chain completed |
| 4 | `git diff --check` | 0 | PASS; no whitespace errors after all test/build commands |
| 5 | `git status --short` | 0 | PASS for repository hygiene; status matched the pre-test baseline before this QA document was written |

Frontend detail:

- `pnpm test`: exit 0; 1 test file passed, 10 tests passed.
- `pnpm run typecheck`: exit 0.
- `pnpm run lint`: exit 0.
- `pnpm run build`: exit 0; Vite transformed 4,558 modules and emitted the production bundle.

## PASS

The following direct coverage passed inside the same full-suite/frontend runs:

- Deterministic task sizing, estimate/create persistence, idempotency, manual
  override validation, and token-hard-cap policy tests.
- ExecutionBudget deadline, lease, host reservation, oversubscription, and
  recovery coverage.
- Host output redacted file rotation, restart continuity, deterministic
  carry-forward, and bounded SQLite tail/index coverage.
- FaultPolicy Host classification and its single TaskService/repository
  handling path for transient, authentication, permission, cancellation, and
  ordinary command failures.
- Task preflight UI behavior, including dynamic budget facts, missing-gate
  blocking, invalidation on input change, exact sizing inputs on create, and
  estimate-failure reset.
- Both `git diff --check` gates and all frontend test/typecheck/lint/build
  gates.

These are direct local test results only. They do not override the failed
backend full-suite gate or establish end-to-end combination acceptance.

## CHANGES_REQUIRED

### 1. Migration contract is not synchronized

Responsibility slice: sizing/model-call accounting persistence migrations.

- `tests/test_app.py::test_health_reports_wal_and_migration`
  - Expected `migration_count == 14`.
  - Actual value: `16`.
- `tests/test_database.py::test_migrations_are_idempotent`
  - Expected the migration list to end at `0014_token_reservations.sql`.
  - Actual list additionally contains
    `0015_model_call_accounting.sql` and
    `0016_task_sizing_budget.sql`.

This is minimal evidence that the two new migrations were added without
updating the existing migration/health acceptance contract.

### 2. Quality-profile run shapes regress through task creation

Responsibility slice: sizing/create API schema composition.

- `tests/test_release_security.py::test_quality_profiles_have_bounded_run_shapes[balanced-expected1]`
  - Expected `{"execute", "plan"}`.
  - Actual: `{"execute"}`.
- `tests/test_release_security.py::test_quality_profiles_have_bounded_run_shapes[strict-expected2]`
  - Expected `{"execute", "plan", "independent_review"}`.
  - Actual: `{"execute"}`.

Minimal cause evidence: `TaskCreate.normalize_quality_profile` currently
normalizes every accepted profile to `deterministic`, so balanced and strict
requests do not reach the existing plan/independent-review branches.

### 3. Repeated verification failure no longer satisfies the terminal guard contract

Responsibility slice: budget single-completion adjudication and the overlapping
`TaskRepository.finish` state transition.

- `tests/test_resilience.py::test_repeated_identical_failure_stops_at_guard_threshold`
  - The third `drive` did not reach the expected terminal result.
  - It raised `InvalidTransitionError: task is not ready: needs_human`.
  - Existing contract: statuses
    `["ready", "ready", "terminal_failed"]` and final
    `task.terminal_failed` event.

Minimal cause evidence: an exhausted/repeated verification failure is now
transitioned to `needs_human`; the existing third-drive and terminal-event
contract still requires `terminal_failed`.

### 4. Default verification failure retries instead of terminating

Responsibility slices: sizing/create API default-attempt change plus the
overlapping budget completion transition.

- `tests/test_tasks_api.py::test_verification_failure_cannot_complete`
  - Expected status: `terminal_failed`.
  - Actual status: `ready`.

Minimal cause evidence: the API task default changed from one attempt to three,
so the failing verification is scheduled for another attempt instead of
preserving the existing one-attempt terminal contract.

## Unverified items

- Online Docker is still using an old image; it was not refreshed or accepted.
- Real deployment migration was not executed.
- Browser E2E was not executed.
- Cursor execution-health was not verified.
- Session rotate-to-fresh is still not implemented.

SPRINT10_PREDEPLOY_VERIFICATION_BLOCKED
