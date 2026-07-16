# Architecture

plow-whip Web v2 is a local-first control plane with four explicit layers.

1. FastAPI and React expose product controls and evidence.
2. SQLite/WAL is the source of truth for projects, tasks, leases, workers, sessions, budgets, events and audit.
3. The deterministic runtime performs scheduling, recovery, context compilation, verification and fault classification without model calls.
4. Provider adapters are workers. The built-in generic command provider is available; model CLI adapters remain unavailable until explicitly configured.

The only recurring operating-system job calls `scheduler-tick`. That tick takes one fenced global lease, reconciles expired work, probes domestic and overseas connectivity, selects a bounded fair batch, obtains worker and resource leases, and dispatches. Project-role sessions are reused until rotation or project release. Context is compiled from objective, one role template and global/project/task Convention instead of full chat replay.

Completion is impossible without deterministic verification. Balanced adds one bounded planning record. Strict adds exactly one independent deterministic review; there is no review recursion.
