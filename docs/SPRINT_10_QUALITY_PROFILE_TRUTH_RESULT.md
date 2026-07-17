# Sprint 10 Quality Profile Truth Result

## Result

PASS for this backend-only slice. This result is not a deployment approval.

`TaskService` no longer creates synthetic `plan` runs, performs a second
`strict` verification, records `independent_review`, or applies a disagreement
branch. New API tasks and persisted legacy `fast` / `balanced` / `strict` rows
all use one deterministic verification path. Their `task_runs` contain only the
real `execute` run required by the existing state machine.

The `quality_profile` API and database field remain readable for compatibility.
The schema marks the input as deprecated and normalizes every accepted API value
to `deterministic`; legacy database values are deliberately ignored by runtime
branching rather than rewritten by a migration.

## Contract evidence

- `tests/test_release_security.py` proves each legacy API input is persisted and
  returned as `deterministic`, and that an existing row containing the same
  legacy value still executes successfully.
- The same parameterized contract counts one verification call per task and
  asserts exactly one `execute` run with no synthetic `model_tokens` payload.
- `tests/test_host_job_continuity.py` proves a persisted `strict` Host task calls
  Host verification once per reconciliation attempt. A temporary bridge failure
  causes one retry, not a hidden second review.
- The obsolete release test whose promise depended on fake `strict` independent
  review was removed. Existing permission, network, secret-redaction, scheduler,
  workforce-parallelism, and Host path boundary tests remain in the suite.

## Verification

- `.venv/bin/python -m pytest tests/test_release_security.py tests/test_host_job_continuity.py tests/test_budget_policy.py -q`
  - final result: 66 passed, 0 failed.
- `.venv/bin/python -m pytest -q`
  - final result: 235 passed, 0 failed.
- One earlier targeted run observed a transient Host process restart race in
  `test_bridge_restart_identifies_orphan_without_duplicate_and_can_cancel`.
  The isolated reproduction passed, followed by the clean final targeted and
  full-suite runs above. No Host implementation was changed for that occurrence.
- `git diff --check`
  - final result: passed.

## Follow-up blockers

- `web/src/App.tsx` still defaults to and displays the legacy
  `fast` / `balanced` / `strict` quality choices. The UI therefore still implies
  quality tiers that the backend intentionally does not provide.
- `independent_review_required` remains in the frontend preflight contract and
  deterministic sizing inputs. It changes an estimate but does not create a
  genuinely independent reviewer.
- A real independent review has not been designed or implemented in this slice.
- No frontend convergence or deployment was performed. These unresolved product
  semantics block any claim that this change is deployable.

SPRINT10_QUALITY_PROFILE_TRUTH_COMPLETE
