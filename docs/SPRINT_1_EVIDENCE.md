# Sprint 1 Evidence

## Delivered vertical slice

- Web/API Task creation with required idempotency key.
- Revision/CAS protected state transitions.
- SQLite Task, Attempt, Run and immutable Event records.
- Generic command provider using argv with `shell=False`, timeout and bounded output.
- Deterministic verification for exit code, file existence and file content.
- Absorbing terminal states and explicit 409 errors for stale revision or invalid transition.
- Task list, Task Detail, Drive action, Event timeline, Evidence hash and Token display.

## Automated proof

- Backend: 8 tests passed, 86% initial branch coverage.
- Duplicate create returns the original Task.
- Duplicate drive with the same idempotency key executes the command once.
- A completed Task rejects a new drive action.
- A verification failure ends as `terminal_failed`, never `completed`.
- Missing project directory is rejected before a Run exists.
- Frontend test, TypeScript check, ESLint and production build pass.

## Real HTTP E2E

`scripts/e2e_sprint1.py` created a Task through HTTP, executed a real subprocess, wrote an Artifact, verified it and reached:

```json
{
  "status": "completed",
  "revision": 3,
  "events": 4,
  "tokens_used": 0,
  "evidence_hash": "6c0224fb93528c5260e7ab402ebe104f2920027d20be4be893178951dbc3f46b"
}
```

## Sprint exit decision

`pass` — one Task can complete unattended only after deterministic verification. The successful Generic Command path consumed zero model Token.
