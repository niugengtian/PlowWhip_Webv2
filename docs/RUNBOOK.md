# Docker runbook

## Mandatory pre-change ledger

Before changing lifecycle, state-machine, Butler/Provider/session, retry/recovery,
evidence/completion, Token accounting, migration, deployment, security, or a
cross-layer primary UI flow:

```bash
.venv/bin/python scripts/engineering_ledger.py check
.venv/bin/python scripts/engineering_ledger.py context --domains lifecycle,evidence
```

Read the generated compact
[`ENGINEERING_MODEL_LEDGER.md`](ENGINEERING_MODEL_LEDGER.md), then only the selected
records under [`engineering-ledger/`](engineering-ledger/). The structured directory
is the only source of truth; the compact model view and
[`ENGINEERING_REQUIREMENTS_AND_INCIDENT_LEDGER.md`](ENGINEERING_REQUIREMENTS_AND_INCIDENT_LEDGER.md)
are generated and must not be edited directly. After a change, update the relevant
source record, bump its entry revision plus `manifest.toml` ledger revision, run
`render`, then `check`. A code edit or passing unit test alone does not close an
incident.

## First start

```bash
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
```

This is the only supported local release writer. It holds a process lock, requires
a clean worktree and exact local/remote branch SHA, passes that SHA into the image
revision label, preserves named volumes, and rejects a non-unique or unhealthy
control-plane. Read-only monitors must never run Compose.

Open `http://127.0.0.1:8742`. Register projects, save Convention and Settings, then enter through **全局管家** or **与项目管家对话**. The Global Butler shows canonical status and takes the operator directly to the isolated Project Butler. The project chat persists every turn, can be resumed after closing the page, asks for one missing field at a time, and presents the 95% structural-completeness proposal for human confirmation before creating the role DAG. Manual single-task create remains available under “诊断任务” for debugging only. The built frontend is served by FastAPI.

Python, Node, SQLite runtime and the scheduler are image internals. Source-mode commands remain available to contributors, but they are not production prerequisites.

## Goal flow

1. Start a project-scoped intake (`POST /api/projects/{project_id}/butler/conversations`), or route through `POST /api/butlers/global/route`.
2. Reply through the conversation `/messages` endpoint; the server owns the one active `expected_field`. At 95% structural completeness, review objective, boundaries, acceptance, sizing, Provider choices, and the proposal hash. To change the proposal, send another message with the selected proposal field before confirming.
3. Confirm the current proposal as a human. Confirmation probes every selected default/role Provider before writing the Goal; a stale revision or hash is rejected.
4. Inspect the returned Goal plan: semantic role, Provider, ordinal, depends_on, sizing/execution_policy. Independent L/XL work items are all ready; only declared dependencies wait.
5. Let Cron/Tick auto-dispatch ready children, or run a manual zero-token tick.
6. A goal completes only when every implementation item has a passing immutable
   `EvidenceManifest`; a writable verification Worker is not treated as independent proof.

The Worker is the logical `project + role` owner. A physical Provider session is scoped to `project + role + Task`; a new Task starts without the previous Task's session, while retries inside the same Task may resume. Replacement archives and increments the Task session generation after bounded no-progress/tool-abort recovery. The local Journal byte threshold rotates only `events.current.jsonl`; it does not silently reuse or discard a Task session. Provider capacity and Token usage alone never rotate a session.

## Scheduler

Settings shows the embedded runner heartbeat, five-field Cron expression, timezone, next run and misfire policy. Saving is revision guarded. Manual zero-token tick:

```bash
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

`max_parallel_workers` includes work already running from prior ticks and manual drives. A Tick result reports `active` and `available_slots`; `selected: 0` with no error is expected when existing Host Jobs occupy every slot.

The Global Butler overview reads normalized SQLite state only. Supplying
`workspace_root` filters already-registered project paths; it does not scan arbitrary
directories, project contents, DOM, logs, or chat history.

Token usage is accounting only. `/api/usage` preserves Provider cumulative snapshots as raw evidence and aggregates normalized physical-session deltas; migrated records without a physical session id are labeled `legacy_inferred_delta`. `cached_input_tokens` is already included in `input_tokens`, so total is `input + output`, never `input + cached + output`. No Token total can block dispatch, change a Task/Goal status, rotate a Worker, or create a human gate.

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
`0021_remove_token_budget.sql` removes the obsolete reservation table while preserving
`token_usage` and historical Task rows. `0028_butler_intake.sql` adds isolated Butler
conversations, one-question state, proposal hashes, and human confirmation linkage.
Check `/health` and the migration count. Never
use `down -v` during an upgrade.

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
