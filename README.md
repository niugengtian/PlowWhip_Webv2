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
comparable alternatives and a bounded Task DAG. Confidence of at least 95%
selects the plan automatically only when no explicit authorization is needed;
otherwise the project Butler asks one question. Plan authorization is stored as
a message bound to the project, Task, spec revision, action, workspace scope and
15-minute expiry.

Runtime continuity has three deliberately small layers: a transient bounded Hot
Context Capsule, atomic Warm `current.json` handoffs with archives, and
append-only Cold Session segment manifests. Project numeric settings are
validated, queued as actions, applied only by `advance_project`, and frozen with
their source into newly-created TaskSessions. Visible project creation, restore,
workspace binding and archive also pass through the same action queue.

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
materialization. Durable HostJob tests also prove that Provider
and Checker waits release SQLite, different projects advance concurrently,
terminal failures fall back by generation, and v3 terminal jobs migrate to
schema v4 without loss. The code-Task regressions use a fake Host Bridge and
therefore spend no external Provider tokens.

## Deliberate V1 boundary

The application never runs a paid Provider periodically. A 0 Token Host Bridge
probe is deterministic; the bounded Codex minimal-Token probe requires an exact
human confirmation and records its ModelCallLedger usage. The application does
not control Docker, touch production, migrate old data, or copy the old
repository.

## Local Docker check

```bash
docker build -t plowwhip-web:v1-local .
docker run -d --name plowwhip-web-v1-8750 \
  -p 127.0.0.1:8750:8742 plowwhip-web:v1-local
```

The explicit non-loopback bind exists only inside the container; Docker exposes
it on the host loopback address above.
