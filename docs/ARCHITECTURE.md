# Architecture

plow-whip Web v2 is a Docker-first local control plane with four explicit layers.

1. FastAPI and React expose Chinese product controls and evidence.
2. SQLite/WAL is the source of truth for projects, tasks, leases, workers, sessions, budgets, events and audit.
3. The deterministic runtime performs scheduling, recovery, context compilation, verification and fault classification without model calls.
4. Provider adapters are workers. Generic Command runs in the container. macOS Codex CLI, Cursor CLI and simple-worker run through a restricted authenticated Host Bridge and register into the same worker pool.

The image runs one embedded Cron engine beside the Web server. Its standard five-field schedule is stored in SQLite and managed from Settings. Each due slot takes one fenced global lease, reconciles expired work, probes connectivity and enabled Providers, selects a bounded batch, obtains worker/resource leases and dispatches. A duplicate container cannot dispatch concurrently because the database lease and fencing token are authoritative.

Project-role-provider sessions are reused until explicit rotation/rebind or project release. The internal binding id is separate from the CLI external session id. Provider switching is never implicit. Context is compiled from objective, one compact role template and global/project/task Convention instead of replaying a full chat.

Probe, wake, lease, recovery and scheduling are deterministic 0 Token actions. A model is invoked only after a ready task is leased to a model Provider, or when the operator explicitly requests Convention refinement. Refinement returns a suggestion and usage record; it never overwrites Convention automatically.

SQLite, WAL, logs and archives live in `/data`; managed repositories live in `/projects`. Host and container paths are stored separately because a Docker named volume cannot be treated as a macOS CLI workspace. Container workers use `projects.path`; Host Bridge workers use `projects.host_path`. Both must refer to the same logical checkout through an explicit mount or operator-managed sync.

Completion is impossible without deterministic verification. Balanced adds one bounded planning record. Strict adds exactly one independent deterministic review; there is no review recursion.
