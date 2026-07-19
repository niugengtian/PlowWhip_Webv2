# Docker runbook

## First start

```bash
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
```

This is the only supported Compose writer. It requires a clean worktree and matching Git revision, holds a local release lock, preserves named volumes and verifies HTTP health, WAL and a non-empty migration ledger. Read-only monitoring must not invoke Compose.

Open `http://127.0.0.1:8742`, register the project and submit a goal through Butler. Structured and natural-language submissions share `/api/butler/intakes`; `POST /api/goals` is compatibility syntax that still creates an intake. Large goals remain in clarification/confirmation until the owner confirms the proposal hash. Do not interpret queued, heartbeat, wake or Host Job acceptance as completion.

## Scheduler and execution safety

The embedded scheduler uses one fenced global lease. `max_parallel_workers` includes work already in flight. A Tick reports active work and available slots; zero selected work is normal when capacity is occupied.

```bash
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

Token settings and historical Task budget fields are estimates/telemetry only. They do not block dispatch, cancel work or change terminal status. `/api/usage` totals `input + output`; cached input is already included in input. Investigate runaway work with wall-clock, process, turn, same-failure and no-progress evidence—not a Token terminal gate.

`model_calls` contains model invocations only. Generic Command is deterministic execution and does not create a zero-Token ModelCall. For cumulative Provider snapshots, inspect both raw snapshots and normalized per-session deltas; individual counters may reset independently.

Physical Provider sessions are Task-scoped. Confirm `provider_sessions.project_id + role_id + task_id + session_generation` before resuming. Only same-Task retries may reuse the external session. A replacement must archive/unbind the previous generation and use bounded structured context.

Continuity defaults are in Settings; the normal observation tail is 20 lines. Project or Task Convention may override supported integers with exactly one single-line declaration such as `Continuity-Limits: {"checkpoint_max_bytes":4096,"observation_tail_lines":12}`. Inspect `GET /api/tasks/{id}/context` before dispatch to confirm effective values, sources and warnings. Oversized help checkpoint/reply context is rejected before persistence; Context that cannot retain boundaries and the completion rule is rejected rather than silently dispatched.

## Butler help and interruption

Worker help is persisted and discoverable with `GET /api/butler/help`; the Butler UI can reply, escalate to the owner and persist the owner resolution. A reply can return bounded context only to the same Task; cross-Task context is rejected. The outbox emits distinct help-requested, Butler-reply, owner-escalation and owner-resolution events. An intake interrupt is revisioned first, then non-terminal Goal Tasks receive cancellation commands. Verify Host Job reconciliation before calling the work stopped.

## Safe deletion

`DELETE /api/tasks/{id}` and `DELETE /api/goals/{id}` require `expected_revision`, `reason` and `Idempotency-Key`.

Before mutation, inspect `GET /api/aggregates/{task|goal}/{id}/control-plane`. It returns the canonical revision, explicit next action, Task-scoped Provider sessions, help chain, a bounded latest-20 transition lineage and deletion eligibility.

- HTTP 202 / `stopping` means active execution or an unconsumed Host Job still requires reconciliation.
- Repeat the same logical delete after cancellation/reconciliation; a replay while still stopping returns the same tombstone without advancing Goal revision.
- Goal deletion cancels undispatched children immediately and waits for active Host Jobs before cascading physical control-row deletion.
- Usage and audit are anonymized and retained.
- Artifact files are never removed by Task/Goal deletion.

## Upgrade and migration

```bash
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
python3 scripts/release_local.py verify --expected-sha "$SHA"
```

Migrations are ordered and idempotent. `0021_unified_domain_reducer.sql` migrates usage to `model_calls`, removes reservation storage and adds reducer/session/deletion state. `0022_butler_intake_help.sql` adds intake, questions, help and reply state. Check `/health` and `PRAGMA integrity_check`; never run `docker compose down -v` during upgrade.

## Host Bridge

Install the macOS LaunchAgent once:

```bash
.venv/bin/python scripts/release_local.py install-bridge-macos \
  --project-root /Users/you/work
```

The Bridge reads secrets only from the mode-600 `.env.local`, restricts project roots and adapters, and keeps Host Job process state outside the container. Container health does not prove container-to-Bridge reachability; verify the configured `host.docker.internal:8765` path when deploying.

## Backup and uninstall

Health creates integrity-checked SQLite backups and secret-free diagnostics. Restore requires the exact backup name and explicit confirmation. `docker compose down` preserves data; `docker compose down -v` destroys the named volume and must be separately authorized.
