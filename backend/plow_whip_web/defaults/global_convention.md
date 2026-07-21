# Task-scoped sessions and bounded continuity

- Physical provider session identity is project_id + role_id + task_id. A different project, role, or Task starts a fresh session; project + role retains only the logical Worker.
- Same-Task retries may resume. Broken or no-progress sessions are terminated, unbound, archived, and replaced with a new task session_generation.
- Replacement bootstrap uses global Convention, project brief, role prompt, immutable TaskSpec, current evidence, latest confirmed progress, and bounded structured checkpoint/handoff.
- Exclude old chat transcripts, full terminal output, complete stdout/stderr, raw tool logs, full DOM dumps, and unbounded event history. Use stable ids, revisions, artifact paths/hashes, offsets, focused errors, verification evidence, and the next action.
- Monitoring reads canonical Task/attempt/episode/Host Job/Worker/Provider/artifact/verification state first. Logs use a configurable bounded tail, normally 20 lines, and expand only by targeted bounded slices. Recurring observation reports deltas.
- Group and configure same-failure, no-progress, Context, checkpoint, handoff, observation, and rotation thresholds. Precedence is direct human Task+role convention > project > global. Show effective sources and reject or warn about conflicting budgets on submit. No fixed 8 KiB global invariant.
- Cached input is a subset of input. Preserve provider cumulative snapshots as raw evidence and aggregate only normalized physical-session deltas.
- Workspace state and deterministic verification are canonical; model claims, heartbeat, queued, wake accepted, or Host Job accepted do not prove completion.

# Major-change incident-ledger gate

- Each project owns one isolated structured ledger source. Human and model Markdown are generated views, never separate sources. Global Convention defines the schema and gate only; it must not merge project incident bodies.
- For PlowWhip Web the source manifest is `docs/engineering-ledger/manifest.toml`, with independent requirement/incident records below that directory. Before a major change run `scripts/engineering_ledger.py check`, read the generated `docs/ENGINEERING_MODEL_LEDGER.md`, then select the Task-scoped pack with `scripts/engineering_ledger.py context --domains <domain,...>`.
- Load full source records only for the selected ids. Persist the ledger revision, entry revisions, hashes and selection reasons in Task context. Do not ingest all project history.
- If source validation fails, generated views are stale, the ledger belongs to another project, or required records are missing, stop the major change and reconcile the project ledger first.
- Major changes include lifecycle/state machines, Butler/Provider/session, retry/recovery/watchdog, evidence/completion, Token accounting, migrations/deployment/security, and cross-layer primary UI flows.
- Record current branch, HEAD, dirty worktree, database schema, deployed revision, related ledger entries, affected state combinations, failure paths, acceptance, and recovery. Do not deploy or restart while a Host Job or model call is active.
- Prefer deleting, merging, or unifying conflicting mechanisms over adding special-case patches.
- After a fault or change, update only the structured source record, bump its entry revision and the manifest ledger revision, then render and check both views. Closed/superseded incidents move from `incidents/open/` to `incidents/archive/YYYY/`; retain only durable rules and regression pointers in active context.
- Separate local code, committed code, remote Git, deployed image, and running database.
- Never mark an incident closed from model claims or a code edit alone. Require proportional regression and live evidence; otherwise use an open or mitigated-unverified status.
