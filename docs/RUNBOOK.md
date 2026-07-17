# Docker runbook

## First start

```bash
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
```

This is the only supported local release writer. It holds a process lock, requires
a clean worktree and exact local/remote branch SHA, passes that SHA into the image
revision label, preserves named volumes, and rejects a non-unique or unhealthy
control-plane. Read-only monitors must never run Compose.

Open `http://127.0.0.1:8742`. Register projects, save Convention and Settings, then **submit a goal**. The control plane PM-splits it into ordered role work items and the scheduler advances them. Manual single-task create remains available under “诊断任务” for debugging only. The built frontend is served by FastAPI.

Python, Node, SQLite runtime and the scheduler are image internals. Source-mode commands remain available to contributors, but they are not production prerequisites.

## Goal flow

1. Ensure the selected Provider probes ready (`/api/providers/{name}/probe`).
2. Submit a goal with sizing gates and verification paths (`POST /api/goals`).
3. Inspect the returned plan: role, ordinal, depends_on, sizing/execution_budget.
4. Let Cron/Tick auto-dispatch ready children, or run a manual zero-token tick.
5. Parent goal completes only when all implementation items and the independent verification item are completed.

Provider session rotation reasons visible in Worker status are limited to provable policy events: explicit operator rotate/rebind, settled `budget_exceeded`, and bounded consecutive no-progress/tool-abort recovery. The local Journal byte threshold rotates only `events.current.jsonl`; it does not discard the Provider session. Provider capacity does not rotate a session. Same-role work continues to reuse its external session even when cached context is large, unless one of those explicit policies fires.

## Scheduler

Settings shows the embedded runner heartbeat, five-field Cron expression, timezone, next run and misfire policy. Saving is revision guarded. Manual zero-token tick:

```bash
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

`max_parallel_workers` includes work already running from prior ticks and manual drives. A Tick result reports `active` and `available_slots`; `selected: 0` with no error is expected when existing Host Jobs occupy every slot.

Host model tasks require positive task and global Token budgets. The control plane reserves the sized allocation before dispatch and exposes active reservation totals from `/api/usage`. Reported actual usage releases unused capacity after settlement. `cached_input_tokens` is already included in `input_tokens`; total is `input + output`, never `input + cached + output`.

Treat the task hard cap as a turn-end settlement gate, not a provider-side generation cutoff. Before dispatch, the guard reports estimated new-work reserve, prior same-generation cached carry-in, and hard cap separately. Current Provider usage has only turn-level attribution, so their projected relationship is `unknown`: pressure and cached values are telemetry/alerts, not proof of repeated work or permission to rotate. The guard therefore conservatively reuses the external session. If settled `input + output` exceeds the cap, the task becomes `terminal_failed` with `budget_exceeded` and its Worker session is archived/rotated once; an idempotency trigger prevents repeated ticks from incrementing the generation again. There is no mid-turn cancellation claim until a Provider supplies reliable streaming usage with deterministic tests. Content-level value attribution, proof of repeated context, and reliable compression are not implemented.

`provider_capacity` means the Provider explicitly reported capacity, rate-limit, HTTP 429, or overload. FaultPolicy defers it with backoff while retaining the external session; repeated no-progress reaches `max_no_progress`. Do not manually rotate a session for one capacity response.

## Upgrade and migration

Create a backup from Health, push the intended clean commit, then run:

```bash
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
python3 scripts/release_local.py verify --expected-sha "$SHA"
```

Do not run a second `docker compose up`, build, restart, or down while this transaction
is active. Migrations are ordered and idempotent. Migration
`0020_provider_context_pressure.sql` adds zero-valued legacy usage/pressure defaults
and no body backfill; it preserves 0017/0018/0019 behavior. Check `/health` and the
migration count. Never use `down -v` during an upgrade.

## macOS Host Bridge

The Bridge remains a host process. Install its persistent user LaunchAgent once:

```bash
.venv/bin/python scripts/release_local.py install-bridge-macos \
  --project-root /Users/you/work
```

The installer refuses replacement while a Host Job is active, keeps one listener on
8765, uses the repository venv by absolute path, and persists an explicit PATH that
contains Codex and simple-worker. It reads secrets only from the mode-600
`.env.local`; no secret value is copied into the plist. All Host Providers share this
one Bridge. Generic Command remains container-local.

## Backup, diagnostics and restore

Health creates integrity-checked SQLite backups and secret-free diagnostic ZIPs. Restore requires the exact backup filename and the literal confirmation `RESTORE`; a safety backup is made first.

## Uninstall

Run `docker compose down` to remove the container while preserving data. Only run
`docker compose down -v` after an explicit decision to destroy SQLite, archives and
managed projects. On macOS, remove the Host Bridge LaunchAgent separately only when
the host worker pool is intentionally being uninstalled.
