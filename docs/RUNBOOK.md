# Docker runbook

## First start

```bash
docker compose up --build -d
docker compose ps
```

Open `http://127.0.0.1:8742`. Register projects, save Convention and Settings, then create tasks. The built frontend is served by FastAPI.

Python, Node, SQLite runtime and the scheduler are image internals. Source-mode commands remain available to contributors, but they are not production prerequisites.

## Scheduler

Settings shows the embedded runner heartbeat, five-field Cron expression, timezone, next run and misfire policy. Saving is revision guarded. Manual zero-token tick:

```bash
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

`max_parallel_workers` includes work already running from prior ticks and manual drives. A Tick result reports `active` and `available_slots`; `selected: 0` with no error is expected when existing Host Jobs occupy every slot.

Host model tasks require positive task and global Token budgets. The control plane reserves the task's remaining budget before dispatch and exposes active reservation totals from `/api/usage`. Reported actual usage releases unused capacity after settlement. Treat this as an allocation gate, not a provider-side generation cutoff: stop or cancel a CLI job if its provider does not honor an external spending limit.

## Upgrade and migration

Create a backup from Health, build the new image, then run `docker compose up -d`. Migrations are ordered and idempotent. Check `/health` and the migration count. Never use `down -v` during an upgrade.

## Backup, diagnostics and restore

Health creates integrity-checked SQLite backups and secret-free diagnostic ZIPs. Restore requires the exact backup filename and the literal confirmation `RESTORE`; a safety backup is made first.

## Uninstall

Run `docker compose down` to remove the container while preserving data. Only run `docker compose down -v` after an explicit decision to destroy SQLite, archives and managed projects. No launchd, systemd or Task Scheduler entry is installed.
