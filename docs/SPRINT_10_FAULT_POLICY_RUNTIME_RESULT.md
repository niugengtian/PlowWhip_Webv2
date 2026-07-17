# Sprint 10 FaultPolicy Runtime Result

## Scope

This slice connects the existing `FaultPolicy` to real Host job termination. It does not change schemas, migrations, provider repositories or pools, Host Bridge code, sizing, settings, UI, deployment, or provider availability presentation.

## Implemented mechanism

- `FaultPolicy.from_host_snapshot()` derives one stable decision from `failure_class`, `returncode`, and terminal `stderr` / `last_error` evidence. It never invokes a model (`model_invoked = False`).
- Known transport signatures (`socket hang up`, `ECONNRESET`, TLS handshake failure, websocket EOF, and temporary bridge unavailability) become `transient_transport` with stable reason `transient_provider_transport` and action `defer`.
- `provider_auth` and `permission_denied` become `needs_human`; they do not enter another model attempt.
- `timeout` and confirmed external interruption retain the confirmed external session and return the same task to `ready` for continuation.
- Ordinary `command_failed` remains on the deterministic verification/evidence path. Transport phrases are matched as terminal error lines so incidental command output mentioning such text is not treated as infrastructure failure.
- Running/heartbeat/log-growth snapshots remain active work; they are not accepted as success or fault progress.
- The implementation is provider-neutral and contains no Cursor-specific runtime branch.

## Single termination path

- `TaskService._handle_host_fault()` is the only Host fault entry. Definitive prelaunch rejection and terminal Host reconciliation use it; unknown dispatch outcomes remain held for reconciliation.
- `TaskRepository.finalize_host_fault()` is the only Host fault state decision/termination transaction.
- The old `resume_after_external_interruption()` and `reject_prelaunch()` state-decision methods were removed because they had no external callers.
- One immediate SQLite transaction now closes the task state, attempt, run, worker, task lease, resource lock, token reservation/usage, Host job consumption, task event, and needs-human outbox event where applicable.
- The Host job id is the idempotency boundary. Repeated reconciliation does not add Token usage or events, does not decrement the attempt again, and re-consumes the same Host job without changing the task revision.
- A deferred or resumed Host infrastructure fault rolls back the current `attempts_used`. Historical attempt row numbering remains monotonic, while a continuation increments the actual attempt budget from the rolled-back value.

## Verified behavior

- The reported terminal snapshot `failure_class=command_failed`, `returncode=1`, `stderr="Error: [aborted] socket hang up"`, and zero Token returns the existing task to `ready` with `attempts_used=0`, releases the reservation, retains the confirmed external session, schedules backoff, and can continue the same task/session without creating a replacement task.
- Non-zero transient transport usage settles exactly once with exact input/output totals.
- Provider authentication and permission failures end in `needs_human` without verification or model retry.
- Timeout continuation preserves the existing checkpoint/session behavior.
- Ordinary command failure invokes verification and consumes a real attempt.
- Forced repeated reconciliation leaves revision, attempt usage, Token usage, and event count unchanged; worker/run/attempt/reservation/locks are already closed.

## Verification evidence

- Required command: `.venv/bin/python -m pytest tests/test_fault_policy_runtime.py tests/test_host_job_continuity.py -q`
  - One earlier combined run observed the existing Host Bridge restart race once: `test_bridge_restart_identifies_orphan_without_duplicate_and_can_cancel` saw `interrupted` instead of `orphan_running`, with background-thread temporary-directory cleanup warnings. No Host Bridge code or assertion was changed.
  - Immediate unchanged rerun: `33 passed`.
  - Runtime C2 continuation unchanged run: `33 passed` in 2.9 seconds.
- `git diff --check`: passed with no output before this document was created; it is rerun after document creation below.

## Deferred work

Provider execution-health accounting and provider cooldown are not implemented in this slice. The Provider page can therefore still report probe availability independently of recent execution transport failures. That requires a later provider-health slice and is intentionally not approximated here.

No commit, push, deployment, schema change, or forbidden-file change was performed.

SPRINT10_FAULT_POLICY_RUNTIME_COMPLETE
