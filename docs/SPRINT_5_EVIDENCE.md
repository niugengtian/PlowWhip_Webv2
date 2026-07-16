# Sprint 5 acceptance evidence

Date: 2026-07-17

## Delivered

- Explicit pause, resume, cancel and needs-human transitions with revision guards and idempotency.
- Durable outbox plus SSE event stream for human-required notifications.
- Zero-model-token domestic and overseas probes with four states: online, domestic-only, overseas-only and offline.
- Per-task network requirements (`none`, `any`, `domestic`, `overseas`); flight mode continues local work and defers only incompatible network work.
- Sleep/resume detection from scheduler gaps without replaying missed ticks or launching a catch-up storm.
- Crash reconciliation for stale running/verifying tasks, leases, resource locks, attempts, runs and busy workers.
- Database-lock handling returns one safe skipped tick instead of retry recursion.
- Bounded retry with exponential eligibility delay, maximum attempts, identical-failure fingerprint and no-progress counters.
- Task leases are longer than the command timeout, preventing a live process from losing its fence before its configured deadline.
- Deterministic fault policy with no model call.
- Human control, network requirement, connectivity, loop counters and needs-you alerts in the web UI.

## Special-case policy

- Domestic down / overseas available: only overseas or network-agnostic tasks run.
- Overseas down / domestic available: only domestic or network-agnostic tasks run.
- Both down / flight mode: local tasks continue; network tasks remain ready and consume zero Token.
- Machine sleep: the persistent OS timer fires after resume; one bounded tick runs, and no historical tick backlog is replayed.
- SQLite busy: the scheduler exits as `skipped_database_busy`; the next OS tick tries once again.
- Process crash: only expired or missing task leases are reclaimed; fencing and resource locks prevent concurrent owners.

## Verification

- 134 backend tests passed with 86% coverage.
- 100 parametrized fault-injection cases produced only bounded actions: defer, retry with backoff, needs-human or terminal failure.
- Three identical no-progress attempts produced `ready`, `ready`, then `terminal_failed`; there was no fourth execution.
- Flight-mode acceptance completed the local task and left the overseas task ready.
- Stale running-task recovery was effective once and idempotent on the second reconciliation.
- Pause/resume/needs-human/cancel, outbox acknowledgement and SSE delivery passed.
- Frontend Vitest, TypeScript, ESLint and production build passed.
