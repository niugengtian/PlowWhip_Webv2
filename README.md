# PlowWhip Web V1

Zero-dependency local implementation of the frozen
[minimal redesign baseline](docs/MINIMAL_REDESIGN_BASELINE_V1.zh-CN.md).

## Run

Python 3.9 or newer is sufficient.

```bash
python3 -m plowwhip serve
```

Open `http://127.0.0.1:8742`. The server intentionally rejects non-loopback
binds. SQLite and runtime files default to `data/`.

Revision 5 exposes seven focused navigation entries: global Butler, project
Butler, projects, Tasks, Token, read-only Monitor, and settings/library.
The project scope selector refreshes the current page in place; entering a
project is an explicit action. Tasks use one project workbench with Goal
navigation, four public-state lanes, and a shared detail inspector.
Human requirements and discovered product issues are tracked in the
[product ledger](docs/PRODUCT_LEDGER.zh-CN.md).

The smallest deterministic instruction is:

```text
写入 result.txt: 闭环完成
```

For a code Task, create or bind the project to an absolute host workspace path
on the Projects page, then submit an ordinary natural-language development
instruction. The control plane creates separate Fullstack and independent
Checker TaskSessions, records ModelCallLedger usage, compares Host Bridge
workspace snapshots, dispatches a durable HostJob, and requires structured
read-only Checker Evidence for every frozen acceptance before Done. A terminal
Provider failure advances to the next frozen candidate with a new Session
Generation; an ambiguous dispatch is never blindly replayed.

An explicit GitHub SSH publish instruction is normalized into a deterministic
`git_publish` Task rather than sent to a model Worker. Its one-file host script
requires a clean tree and an unexpired remote/branch authorization, rejects
tracked secret-like files and credential patterns, pushes the frozen HEAD, then
compares `git ls-remote` with the local commit. It records Evidence but no model
Token. A non-fast-forward result records both local and observed remote SHA and
stops at `NeedsDecision`. The Task page then offers exactly two scoped recovery
actions: publish to a different branch without rewriting history, or enter the
full observed remote SHA to authorize `force-with-lease`. Either choice creates
a new TaskSpec revision, 15-minute authorization and Session generation; a moved
remote safely fails the lease. A Host Bridge rejection before job acceptance is
terminal; a genuinely unknown accepted job remains `NeedsDecision`, where the
Task page exposes the exact HostJob confirmation needed for safe recovery.

It follows this path:

```text
POST /api/messages
→ SQLite WAL
→ in-process Cronner
→ advance_project (one action)
→ classify / optional Planner / execute / verify / bounded repair
→ Done or NeedsDecision
```

Large instructions use a read-only Planner that must return at least two
comparable alternatives, per-Task acceptance contracts, scheduling declarations
and a bounded Task DAG. Confidence of at least 95%
selects the plan automatically only when no explicit authorization is needed;
otherwise the project Butler asks one question. Plan authorization is stored as
a message bound to the project, Task, spec revision, action, workspace scope and
15-minute expiry.

Runtime continuity has three deliberately small layers: a transient bounded Hot
Context Capsule, atomic Warm `current.json` handoffs with archives, and
append-only Cold Session segment manifests. Native compact policy/events and
non-native generation rotation use the same frozen thresholds. Project numeric
settings, Provider order and Project rules are validated, queued as actions,
applied only by `advance_project`, and frozen with their source into newly-created
TaskSessions. Human-visible Unicode project names are separate from safe internal
IDs. Creation, restore, workspace/name binding and archive pass through the same
action queue; an unchanged create request returns without adding history.

Executor, Planner and Checker calls use stable HostJob IDs with
start/status/output reconciliation outside SQLite write transactions. A restart
polls the existing job instead of blindly replaying it. Explicitly read-only
analysis Tasks use a read-only Host Bridge job and may finish from independent
Evidence without a workspace delta.

The repository includes the restricted host-side Bridge used by those calls.
It binds only to `127.0.0.1`, accepts a fixed adapter set instead of arbitrary
commands, restricts workspaces to explicit roots, persists job identity before
starting a process, and returns bounded redacted output:

```bash
install -m 600 /dev/null /absolute/private/plowwhip-bridge.env
# Edit that private file and add:
# PLOW_WHIP_BRIDGE_TOKEN=replace-with-a-random-value-at-least-24-characters
python3 -m plowwhip.host_bridge \
  --port 8765 \
  --env-file /absolute/private/plowwhip-bridge.env \
  --state-dir /absolute/private/plowwhip-host-jobs \
  --project-root /absolute/allowed-workspace
```

Use that exact command as a macOS `launchd` or Linux `systemd` service when a
persistent host installation is approved. Service startup and `/v1/probe` do
not invoke a model. Keep the environment file outside the repository; it may
contain only the Bridge token and supported Provider variables and must remain
mode `0600`. Job state never stores prompts, argv, or credentials.
The same active-process restart/cancel contract is tested on macOS and in a
minimal Linux container. Cursor read-only Planner/Checker work uses CLI plan
mode without `--force`; write Tasks enable `--force` only inside the configured
workspace sandbox. Adapter executables resolve from the host service `PATH`
instead of a macOS-only path; Cursor also accepts the `cursor-agent` binary
name used by its standalone installer. A first Cursor job creates one chat
session before invoking `--resume`; later retries of the same Task reuse that
physical session.

The global Butler accepts a selected Project or an `@项目名称` prefix. Safe
internal IDs remain accepted for existing API clients. An
exact search such as `找 result.txt 任务` routes to a unique Project without
creating a Task. Project conversation files are bounded projections; global
conversation files contain only transfer references, while SQLite remains the
single canonical history.

Only `POST /api/messages` and `POST /api/actions` mutate owner intent. Monitor
and all GET routes are read-only.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The suite covers WAL and fencing, idempotent intake, four-state convergence,
Evidence, automatic repair, versioned DAGs, cancellation and generation
rotation, TaskSession ownership, bounded Hot/Warm/Cold continuity, token
normalization, Token dashboards, recoverable project archive, restart recovery,
read-only Monitor, Provider Probe Tasks, queued project settings, UI/API safety,
and fail-closed external Providers. Planner tests cover high-confidence
selection, one Butler question, scoped authorization and serial DAG
materialization. Durable HostJob tests also prove that Executor, Planner and
Checker waits release SQLite, different projects advance concurrently, all three
model roles recover across restart and fall back by generation, and terminal
jobs migrate to schema v6 without loss. A real loopback HTTP test also runs the
restricted Host Bridge against a local fake executable to prove authentication,
root/executable guards, durable idempotent start, bounded output, cancellation,
restart reconciliation, and zero-secret state. The suite also covers deadline stopping,
compact/rotation, global routing, Project rules/templates, consistent SQLite
backup, candidate isolation and the single-Cronner lock. The code-Task
regressions use a fake Host Bridge and therefore spend no external Provider
tokens. Git publishing is tested against a local bare repository, including
authorization scope, tracked-secret rejection, diverged history, stale leases,
the two recovery actions and exact remote SHA evidence.

## Deliberate V1 boundary

The application never runs a paid Provider periodically. A 0 Token Host Bridge
probe is deterministic; the bounded Codex minimal-Token probe requires an exact
human confirmation and records its ModelCallLedger usage. The application does
not control Docker, touch production, migrate old data, or copy the old
repository.

“CLI available” on Monitor means the zero-Token version probe succeeded. It
does not mean a model was invoked: Token remains zero until a real Provider
HostJob reports usage into ModelCallLedger.

## Candidate gate

Create a consistent database copy without copying a live `.db` away from its
WAL:

```bash
python3 -m plowwhip --db data/plowwhip.db --data-root data \
  backup /absolute/candidate-data/plowwhip.db
```

`candidate-preflight` accepts production and candidate JSON manifests containing
exactly `code_root`, `data_root`, `db_path`, `compose_project`, `port`,
`host_bridge_namespace`, and `cronner_enabled`. It rejects shared isolation
fields, requires the candidate database to live in its own data root, and
requires the candidate Cronner to be disabled:

```bash
python3 -m plowwhip candidate-preflight production.json candidate.json
```

The result never authorizes cutover. Production switching, old-data reconcile
and rollback remain separate owner-approval actions.

Before an approved rollback, verify that the candidate Cronner is disabled, its
shared scheduler lock is released, SQLite passes `quick_check`, and no active
project lease remains:

```bash
python3 -m plowwhip rollback-preflight candidate.json
```

This command is a gate only; it does not switch traffic or mutate Task state.

## Local Docker check

```bash
docker build -t plowwhip-web:v1-local .
docker run -d --name plowwhip-web-v1-8750 \
  -p 127.0.0.1:8750:8742 plowwhip-web:v1-local
```

The explicit non-loopback bind exists only inside the container; Docker exposes
it on the host loopback address above.
