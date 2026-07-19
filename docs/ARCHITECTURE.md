# Architecture

plow-whip Web v2 is a Docker-first local control plane. FastAPI and React expose controls; SQLite/WAL owns canonical state; deterministic services schedule, recover and verify; Provider adapters execute work in the original host project directory.

## Canonical aggregates

All goal-shaped external input enters `ButlerIntake`, whether the source is structured JSON or natural language. The public compatibility `POST /api/goals` route also creates a structured intake before a Goal exists. Manual `POST /api/tasks` remains a diagnostic command, not a second goal workflow.

The deterministic size floor cannot be downgraded by a model assessment. A large intake has at most one unanswered question, cannot dispatch below 95% confidence, and still requires owner confirmation of the exact proposal hash. Small and medium intakes may select an available Provider and create an ordered Goal/Task plan. Goal creation is idempotent; the following intake transition to `dispatched` and all initial `worker.wake_requested` events commit together. A wake event proves only that dispatch was requested.

Task and Goal state/evidence changes use optimistic revisions and the shared `aggregate_transitions` write protocol, including stale-lease recovery. Each transition records actor, reason, previous/new state and previous/new evidence hashes. Authorized evidence rewrites are CAS-protected and append lineage; reusing an idempotency key for different evidence or command metadata is rejected. Model prose, queued state, heartbeat and wake acceptance cannot create deterministic completion evidence.

## Worker and Provider session identity

`project + role` identifies the logical Worker and responsibility boundary. A physical Provider session is a separate `provider_sessions` aggregate keyed by `project + role + task + session_generation`.

- A different Task never resumes another Task's external session.
- A retry of the same Task may resume its current generation.
- The legacy Worker external-session column remains empty; Host Job resume, API projection and usage attribution read only `provider_sessions`.
- Termination/rotation archives the old generation before a replacement is bound.
- Replacement Context is compiled from stable conventions, immutable task state, bounded checkpoint/handoff and current evidence. Full chats and unbounded logs are not copied.

The file journal rotates independently from the Provider session. Context, checkpoint, handoff, observation, rotation and failure thresholds are one continuity policy. A one-line `Continuity-Limits: {"handoff_max_bytes":6144}` declaration in Task Convention overrides Project Convention, which overrides global Settings. The compiled Context API returns every effective value, its source and conflict warnings; mandatory-boundary conflicts fail compilation. Bounded monitoring defaults to 20 lines, reads structured Task/attempt/Host Job/session/artifact state first and only then a focused log tail.

## Usage is observe-only

`model_calls` is the only stored usage ledger. The legacy `token_usage` name is a read-only compatibility view and `token_reservations` no longer exists. Every settled model call, including a real call that reports zero Token, retains direct Goal/Task/attempt/Worker/Host Job/Provider attribution, physical session/generation, stable anonymous Goal/Task hashes, raw usage and normalized values. Deterministic Generic Command executions do not create fake ModelCall rows. Non-Task model calls such as Convention refinement are project-attributed one-shot calls and are recorded in the same ledger.

Provider cumulative snapshots are linked in settlement order within one physical session. Input, cached-input and output counters are normalized independently: a monotonic counter contributes its delta and a reset counter contributes its current value. Normalized cached input is bounded by normalized input, so total usage is always `input + output`.

Token values never reject admission, cancel a run, rotate a session or change a verified terminal result. Historical token-budget and sizing fields remain as compatibility estimates for old clients. Execution safety is enforced by concurrency, lease/fencing, wall-clock deadlines, same-failure/no-progress handling, Provider readiness and deterministic verification.

## Execution and evidence

Host CLI execution has one control truth with two ownership domains: SQLite owns Task/attempt/Host Job reconciliation, while Host Bridge owns only process identity, PID and the external session snapshot. A Host completion cannot write Task evidence or terminal state directly; only the versioned reducer path after deterministic verification can. An unconsumed Host Job excludes its Task from speculative stale recovery. If the old process cannot be proven dead, the control plane holds instead of duplicate-dispatching.

Completion requires deterministic verification. File evidence includes path metadata and content hash where supported. Provider output is bounded, redacted and file-backed; SQLite stores references and small structured facts instead of full stdout/stderr or prompts.

## Help, interruption and deletion

Worker help requests, Butler/owner replies, bounded same-Task context and owner escalation are revisioned records. The outbox distinguishes help requested, Butler reply, owner escalation and owner resolution. Cross-Task help context is rejected and only one open owner escalation is allowed per Task.

The aggregate control-plane read model composes—not rewrites—canonical Task/Goal state, the explicit next action, Task-scoped sessions, help chain, deletion eligibility and the latest bounded 20 reducer transitions. The Web Task/Goal detail and Butler inbox consume this read model; it is not another state machine.

Task and Goal deletion is a command:

1. CAS and idempotency accept the request.
2. Active Task/Host Job state moves to stopping and cancellation is requested; undispatched Goal children become cancelled in the same transaction.
3. Only reconciled control rows are deleted in one transaction.
4. Goal deletion cascades through its Tasks and runtime control rows.
5. Usage and audit lineage are retained with hashed aggregate identity.
6. Artifact references are retained in the tombstone and artifact files are never deleted.

Repeated deletion returns the same tombstone and does not keep advancing Goal revision while Host reconciliation is pending. Goal-only and Task-attributed ModelCalls are anonymized. SQLite transactions, unique keys and foreign keys provide the concurrency boundary; no parallel deletion workflow exists.
