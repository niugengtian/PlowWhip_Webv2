# Architecture

plow-whip Web v2 is a Docker-first local control plane with four explicit layers.

1. FastAPI and React expose Chinese product controls and evidence.
2. SQLite/WAL is the source of truth for projects, tasks, leases, workers, sessions, budgets, events and audit.
3. The deterministic runtime performs scheduling, recovery, context compilation, verification and fault classification without model calls.
4. Provider adapters are workers. All user-selectable project workers run through the restricted authenticated Host Bridge against the original host project directory. Generic Command remains an internal deterministic test adapter and is not exposed in the Web UI worker pool.

The image runs one embedded Cron engine beside the Web server. Its standard five-field schedule is stored in SQLite and managed from Settings. Each due slot takes one fenced global lease, reconciles expired work, probes connectivity and enabled Providers, selects a bounded batch, obtains worker/resource leases and dispatches. A duplicate container cannot dispatch concurrently because the database lease and fencing token are authoritative.

Project-role-provider sessions are reused until explicit rotation/rebind or project release. The internal binding id is separate from the CLI external session id. Provider switching is never implicit. Context is compiled from objective, one compact role template and global/project/task Convention instead of replaying a full chat.

Probe, wake, lease, recovery and scheduling are deterministic 0 Token actions. A model is invoked only after a ready task is leased to a model Provider, or when the operator explicitly requests Convention refinement. Refinement returns a suggestion and usage record; it never overwrites Convention automatically.

SQLite, WAL, logs and archives live in `/data`; `/projects` is a control-plane mount, not an artifact destination. Host and container paths are stored separately because a Docker named volume cannot be treated as a macOS CLI workspace. Project workers execute and deterministically verify against `projects.host_path` through authenticated structured Host Bridge endpoints. Reports, code, and other deliverables remain in the original host checkout.

Completion is impossible without deterministic verification. Balanced adds one bounded planning record. Strict adds exactly one independent deterministic review; there is no review recursion.

## Runtime resource gates

`max_parallel_workers` is a system-wide in-flight limit. Both Scheduler selection and the transactional task claim count `running`, `verifying`, `stopping`, and unconsumed Host Jobs. The Scheduler subtracts existing work before selecting a batch; the claim transaction is the final guard for manual drives and concurrent callers.

Every Host model task reserves its entire remaining task Token budget in the same SQLite transaction that creates its attempt and run. Active reservations and recorded usage both count against the global daily budget, so concurrent claims cannot allocate the same daily capacity. A zero task or global budget rejects the Host call before claim or dispatch. Completion reconciles the reservation to reported usage; cancellation, interruption, and stale-run recovery settle or release it.

This reservation is a scheduling/accounting hard gate, not a provider-side output cap. Codex, Cursor, and JSON Worker do not expose one common enforceable maximum-token argv contract, so a CLI can report more actual usage than was reserved. Convention refinement also has no task budget to reserve against and is not yet part of this ledger. Both are explicit product-policy boundaries, not completed budget guarantees.

## Context and evidence

Context truncation preserves Boundaries and the Completion rule in full. When the pack is too large, content is reduced deterministically from lower to higher priority: global Convention, continuation/role, objective, project Convention, then task Convention. If the configured limit cannot retain the protected safety tail and minimum task/project allocations, compilation fails instead of dispatching an unsafe pack.

File verification evidence includes the artifact SHA-256, byte size, and nanosecond modification time. These values are part of the evidence hash on both container and Host Bridge verification paths, so later artifact changes no longer match the recorded evidence. Existing verification semantics still allow an unchanged pre-existing file to pass; requiring creation or modification by the current run needs an explicit task-level provenance policy and is not inferred automatically.

## Execution continuity

Host CLI execution is a two-ledger protocol:

1. SQLite creates a stable Host Job id with task attempt, worker generation and fencing token.
2. Host Bridge writes `dispatching` before process creation, then persists PID, process identity and CLI session as soon as they exist.
3. The scheduler polls Host Job state and renews task/resource leases without invoking a model.
4. `completed` enters deterministic verification; `interrupted` releases the dead process lease and requeues with the retained CLI session; `cancelled` releases only after host confirmation.
5. Host Provider artifact checks run inside the same restricted Host Bridge project root. If the Bridge is unavailable after execution, the unconsumed Host Job retains its lease in `recovery_hold` and verification resumes without another model run.

Artifact discovery is bounded to the file paths already declared by task verification. The bridge returns only path metadata (existence, byte size, SHA-256, and modification time), while fixed Finder-reveal and Cursor-open actions revalidate the project root and reject traversal. The control plane never copies artifact contents into Docker or accepts arbitrary shell strings for artifact actions.

An unconsumed Host Job excludes its task from generic stale-lease recovery. This is the brain-split boundary: inability to prove that the old process is dead causes `recovery_hold`, never speculative duplicate dispatch. A Bridge restart can identify and cancel a live orphan process, but cannot reattach its stdout pipe; after that orphan exits, the task resumes from the persisted CLI session and compact context on a new attempt.
