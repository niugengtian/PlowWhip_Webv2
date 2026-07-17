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

## Goal orchestration

The primary product path is goal submission, not manual role picking.

1. `POST /api/goals` is the sole split entry. A deterministic 0 Token PM planner creates a coordination parent plus an ordered linear chain of implementation/verification work items (1-7). There is no general DAG in this release.
2. Each `project + role` reuses one stable Worker session. Task slices do not open a new session by default. The pre-dispatch guard records the task reserve, prior same-generation cached carry-in, hard cap, and an explainable decision. With only turn-level attribution, the projected relationship is `unknown`; cached/context pressure is telemetry and does not rotate the Provider session. A task whose settled `input + output` actually exceeds its hard cap rotates once in the settlement transaction. Consecutive no-progress/tool aborts retain the existing bounded FaultPolicy rotation, while explicit operator rotate/rebind remains available. Rotation trigger keys make repeated Scheduler ticks generation-idempotent. Provider capacity does not rotate, and the local Journal byte threshold rotates the file generation only.
3. Cross-role handoff is structured metadata only: evidence hash and artifact paths. Full model history is never copied between roles. SQLite stores goals/tasks/leases/budgets/session ids and file path/hash/offset metadata; stdout/stderr and journals remain file-backed and rotate by `rotation_max_bytes`.
4. Child work items reuse the existing 0 Token sizing → dynamic token/deadline/attempt/lease path and Provider readiness probes. The Scheduler advances ready children, feeds Evidence Delta into the same-role session on repairable failure, and completes the parent only after every implementation child and the independent verification child succeed.

Manual `POST /api/tasks` remains a diagnostic escape hatch.

## Runtime resource gates

`max_parallel_workers` is a system-wide in-flight limit. Both Scheduler selection and the transactional task claim count `running`, `verifying`, `stopping`, and unconsumed Host Jobs. The Scheduler subtracts existing work before selecting a batch; the claim transaction is the final guard for manual drives and concurrent callers.

Every Host model task reserves its sized task allocation in the same SQLite transaction that creates its attempt and run. Active reservations and recorded usage both count against the global daily budget, so concurrent claims cannot allocate the same daily capacity. A zero task or global budget rejects the Host call before claim or dispatch. Completion reconciles the reservation to reported usage; cancellation, interruption, and stale-run recovery settle or release it.

Provider usage is stored as `input_tokens`, `cached_input_tokens`, `uncached_input_tokens = input - cached`, and `output_tokens`. Cached input is a subset of input, so accounting total and the task hard-cap comparison are always `input + output`; cached input is never added again. `uncached_input` means cache miss only—it does not prove new work, value, or waste. Each call records task, Worker, session generation, rotation reason, `attribution_granularity=turn`, and `value_classification=unknown`. Worker rows retain the same last-usage and guard metadata. SQLite stores only these numbers, session ids, failure classes, and output refs/hashes/bytes/offsets—not prompt, stdout, stderr, or key bodies.

The hard cap is a settlement hard gate, not a provider-side mid-turn cutoff. Codex, Cursor, and JSON Worker do not expose one common reliable streaming-usage cancellation contract, so a CLI can report more actual usage than was reserved after the turn ends. The pre-dispatch guard reports prior structured usage, cached carry-in, the estimated new-work reserve, and the hard cap, but does not add them into a projected rotation rule: with turn-level attribution it cannot prove which cached context will be sent again or whether it is valuable. Its relation is therefore `unknown` and it conservatively reuses the session. Reliable content compression is not implemented in this slice; carry-forward is generated only by the existing file Journal mechanism. Convention refinement also has no task budget to reserve against and is not yet part of this ledger.

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

Host Bridge classifies explicit capacity/rate-limit responses as `provider_capacity`. FaultPolicy retains the same external session and performs bounded defer; repeated capacity with no progress reaches the shared fault threshold and becomes human-visible instead of retrying forever. A single capacity response never rotates the session. Host Job updates are generation-fenced so replaying an old job cannot restore its external session id after rotation.

Artifact discovery is bounded to the file paths already declared by task verification. The bridge returns only path metadata (existence, byte size, SHA-256, and modification time), while fixed Finder-reveal and Cursor-open actions revalidate the project root and reject traversal. The control plane never copies artifact contents into Docker or accepts arbitrary shell strings for artifact actions.

An unconsumed Host Job excludes its task from generic stale-lease recovery. This is the brain-split boundary: inability to prove that the old process is dead causes `recovery_hold`, never speculative duplicate dispatch. A Bridge restart can identify and cancel a live orphan process, but cannot reattach its stdout pipe; after that orphan exits, the task resumes from the persisted CLI session and compact context on a new attempt.
