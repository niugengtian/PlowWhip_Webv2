# Sprint 11 Backend Correction Result

## Scope

- Backend mechanisms, migrations, and backend tests only.
- No `web/` changes, UI rework, deployment, reset, checkout, rollback, commit, or push.
- Existing dirty-worktree changes were preserved.

## Removed or merged mechanisms

- Replaced the old five-role assertion with an explicit catalog contract: six capability roles (`coordination`, `backend`, `frontend`, `ui`, `devops_sre`, `verification`) plus the retained `fullstack` and `web3` legacy binding aliases.
- Kept deterministic planning free of title/objective keyword routing. Legacy roles are selected only by an explicit structured plan.
- Removed generic `aborted` matching and immediate session rotation. Exact transient transport signatures defer with their session retained; internal tool aborts require no process exit status and rotate only after two consecutive no-progress results in the same session generation.
- Removed per-application-start recursive SQLite body scrubbing. Migration `0019_backend_correction.sql` performs the one-time idempotent legacy cleanup; runtime persistence keeps output refs, segment metadata, hashes, byte counts, offsets, and error classification without stdout/stderr/prompt bodies.
- Corrected session archive reads to use the real `archived_at` schema column.
- Unified attempt truth: estimated task creation mirrors `execution_budget.max_attempts` into `tasks.max_attempts`, migration `0018_p0_correction.sql` repairs older rows, and claim/retry/terminal decisions use the same authoritative value.
- Removed unconditional child fallback to `goal.provider`. Existing role Worker bindings win; every unbound work-item role requires an explicit provider decision. Each distinct provider is probed before the atomic goal/task transaction, so any failed probe leaves no goal or task rows.
- Journal threshold checks count only `events.current.jsonl`. Worker rotation first archives that hot generation, so archived generations cannot repeatedly rotate the same role Worker; multiple same-role children reuse the Worker/session until the bounded threshold is reached.
- Host Bridge manager recreation now reuses a process owned by the same bridge process while retaining the persisted PID/start-identity guard for real process restarts. Monitor threads exit cleanly if their temporary state has already been removed.

## Verification evidence

- Fresh migration: 19 migrations applied; final migration `0019_backend_correction.sql`.
- Second migration on the same fresh database: no migrations applied.
- Upgrade regression: legacy task attempt count repaired from 1 to 4 and legacy SQLite output/prompt bodies scrubbed while refs/segments/offsets remain.
- Related backend regression slice: 81 passed.
- Host Bridge manager-recreation race regression: 10 consecutive passes.
- `.venv/bin/python -m pytest -q`: 244 passed.
- `git diff --check`: passed with no output.

## Still not implemented

- Model-generated PM planning remains disabled (`model_pm_implemented=false`); only explicit structured plans and the deterministic sizing-based default are supported.
- Existing `fullstack`/`web3` bindings are retained as compatibility aliases; this slice does not automatically rewrite them to capability roles.
- No frontend editor for per-role provider decisions was added because this slice is backend-only. Callers must send `role_providers` explicitly for unbound work-item roles.
- Real external Codex/Cursor availability remains an environment/runtime fact. This slice verifies unique-provider probing and atomic no-write behavior, not external CLI availability.

SPRINT11_BACKEND_CORRECTION_COMPLETE
