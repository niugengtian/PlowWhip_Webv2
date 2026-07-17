# Sprint 9 — Execution continuity

## Outcome

- Host CLI work is asynchronous and survives control-plane request/container restarts through stable Host Job ids.
- PID, process identity and external CLI session are persisted before normal task completion.
- One zero-Token scheduler reconciles all active Host Jobs and renews task/resource leases.
- An unconfirmed host process is isolated in `recovery_hold`; stale recovery cannot duplicate it.
- Confirmed external interruption requeues the task, preserves the CLI session and does not spend the task attempt budget.
- The next compact Context Pack carries one continuation cue, and token usage is idempotent per execution run.
- Running cancellation transitions through `stopping` and releases the worker only after host confirmation.

## State path

`ready → running/dispatching → running|orphan_running|recovery_hold → verifying → completed`

Exceptional paths:

- confirmed dead process: `running → interrupted → ready`
- operator cancellation: `running → stopping → cancelled`
- uncertain dispatch/process identity: `running → recovery_hold`

## Verification

- Host Job tests cover idempotent start, early PID/session persistence, bounded sanitized state, Bridge restart/orphan detection and cancellation.
- Container tests cover active-job recovery exclusion, completion reconciliation, attempt-neutral interruption resume and safe running cancellation.
- Full backend, frontend typecheck, lint, component tests and production build remain release gates.

## Explicit boundary

Bridge restart does not reconstruct a lost stdout pipe. A surviving orphan is held or cancelled; after it is confirmed dead, the next attempt resumes the same CLI session with a newly compiled compact context. This favors no duplicate execution over pretending that arbitrary process I/O can be reattached.
