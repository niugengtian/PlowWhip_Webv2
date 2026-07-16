# Sprint 3 acceptance evidence

Date: 2026-07-17

## Delivered

- One global database-backed scheduler for every project, role, worker and ready task.
- High-availability scheduler lease with monotonically increasing fencing token.
- Deterministic zero-model-token scan, probe, selection and dispatch control path.
- Native scheduler capability plans for macOS launchd, Linux systemd user timers and Windows Task Scheduler.
- Explicit authorization boundary before any user-level operating-system scheduler installation.
- Versioned Settings API and page for interval, lease, parallelism, automatic dispatch, budgets, circuit thresholds, context limits and rotation limits.
- Headless `scheduler-tick` CLI entry point for the operating system timer.

## Verification

- 17 backend tests passed.
- One tick scanned and completed tasks from two projects while reporting `model_tokens: 0`.
- A second scheduler owner was rejected while the first global lease was live.
- Invalid settings and stale revisions were rejected before persistence.
- Scheduler installation without explicit persisted authorization returned `authorization_required` and performed no write.
- Real CLI smoke test returned a completed empty tick with fencing token 1 and zero model tokens.
- Frontend Vitest, TypeScript, ESLint and production build passed.

The operating-system scheduler is intentionally not installed by repository tests. Installation is an explicit local admin action from Settings, because it writes outside the project directory.
