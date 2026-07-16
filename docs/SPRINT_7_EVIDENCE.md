# Sprint 7 Docker runtime and embedded Crontab evidence

Date: 2026-07-17

## Delivered

- Multi-stage Docker image builds the React UI and installs the FastAPI application.
- One unprivileged application process runs the Web server and embedded zero-Token Cron engine.
- Docker named volume `plow-whip-web-v2-data` persists SQLite, WAL, logs, archives and backups at `/data`.
- Docker named volume `plow-whip-web-v2-projects` persists managed repositories at `/projects`.
- Settings manages one global five-field Cron expression, timezone, enabled state and misfire policy.
- Host launchd, systemd and Task Scheduler installation code and API were removed.
- `restart: unless-stopped` restores the runtime after Docker restarts; a missed schedule is coalesced into one catch-up Tick.
- The global database lease/fencing token prevents concurrent schedulers, while persisted `last_cron_slot` prevents a restart from repeating the same minute.
- The UI exposes runner heartbeat, next run, last Tick, fencing token, `/data` location and the 0-Token control-path assertion.

## Automated gates

- Backend: 152 tests passed with 89% branch-aware coverage.
- Embedded Cron module: 93% coverage, including standard numeric fields, ranges, lists, steps, Sunday `0/7`, DOM/DOW semantics, validation, next run, persistent slot deduplication, heartbeat, stop, error and catch-up.
- Frontend: Vitest, TypeScript, ESLint and Vite production build passed.
- Docker Compose configuration validation passed.
- Multi-stage Docker image `plow-whip-web-v2:local` built successfully.

## Live container evidence

- Container became healthy on `127.0.0.1:8742`.
- `/health` reported SQLite WAL and eight applied migrations.
- `/api/scheduler/status` reported `embedded-cron`, `managed_by: docker`, active heartbeat, `model_invoked: false` and `model_tokens: 0` in the completed Tick.
- Runtime process identity was `uid=999(plowwhip)`, not root.
- Docker inspection showed exactly two writable named-volume mounts: `/data` and `/projects`.
- After a container restart, Settings revision 2 and the default Cron plan remained persisted.
- Restarting inside the same minute did not increment the fencing token or repeat the prior Tick because `last_cron_slot` was durable.

## Browser acceptance

- The live Docker-served Today page rendered the approved product priority and `Control Token: 0`.
- Settings rendered Container Crontab as running with backend, schedule, next run, fencing, heartbeat and SQLite path.
- Changing Cron to `*/5 * * * *` with `skip` persisted as revision 1; restoring `*/1 * * * *` with `catch_up_once` persisted as revision 2.
- Browser console reported zero warnings and zero errors.

## Exit result

`pass`
