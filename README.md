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

Revision 2 exposes seven focused navigation entries: global Butler, project
Butler, projects, Tasks, Token, read-only Monitor, and settings/library.
Human requirements and discovered product issues are tracked in the
[product ledger](docs/PRODUCT_LEDGER.zh-CN.md).

The smallest deterministic instruction is:

```text
写入 result.txt: 闭环完成
```

It follows this path:

```text
POST /api/messages
→ SQLite WAL
→ in-process Cronner
→ advance_project (one action)
→ execute / verify / bounded repair
→ Done or NeedsDecision
```

Only `POST /api/messages` and `POST /api/actions` mutate owner intent. Monitor
and all GET routes are read-only.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

The suite covers WAL and fencing, idempotent intake, four-state convergence,
Evidence, automatic repair, versioned DAGs, cancellation and generation
rotation, TaskSession ownership, bounded handoffs, token normalization,
Token dashboards, recoverable project archive, restart recovery, read-only
Monitor, settings/library snapshots, UI/API safety, and fail-closed external
Providers.

## Deliberate V1 boundary

The application does not call paid Providers, control Docker, touch production,
migrate old data, or copy the old repository. External Provider candidates are
represented as unavailable facts; only the local deterministic adapter runs.

## Local Docker check

```bash
docker build -t plowwhip-web:v1-local .
docker run -d --name plowwhip-web-v1-8750 \
  -p 127.0.0.1:8750:8742 plowwhip-web:v1-local
```

The explicit non-loopback bind exists only inside the container; Docker exposes
it on the host loopback address above.
