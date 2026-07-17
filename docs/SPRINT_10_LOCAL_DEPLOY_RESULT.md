# Sprint 10 local deployment result

Date: 2026-07-17 21:09 CST
Task: `c29fec9a-60a6-4e14-8884-81a8509b5c4a`
Worker: `b6f9eda2-4334-4ef7-a161-b3185a1d632d` (`devops_sre`)
Target: `http://127.0.0.1:8742/`
Result: **PASS**

## Scope and deployment path

`README.md`, `docs/RUNBOOK.md`, and `compose.yaml` identify the supported local deployment command as:

```bash
docker compose up --build -d
```

The existing Compose service, image name, loopback port, and named volumes were reused. No implementation file was edited, no commit or push was made, and no reset, rollback, database clear, volume rebuild, `down -v`, or Host Bridge termination was performed. The only project file added by this deployment is this report.

## Commands executed

Read-only discovery and pre-deploy inventory:

```bash
sed -n '1,260p' README.md
sed -n '1,320p' docs/RUNBOOK.md
sed -n '1,300p' compose.yaml
git status --short --branch
docker compose ps --all --format json
docker image inspect plow-whip-web-v2:local --format '{{json .Id}} {{json .RepoTags}} {{json .Created}}'
docker volume inspect plow-whip-web-v2-data plow-whip-web-v2-projects
docker inspect plow-whip-web-v2-control-plane-1 --format '{{json .Mounts}} {{json .State}} {{json .RestartCount}} {{json .Config.Image}}'
curl -fsS --max-time 8 http://127.0.0.1:8742/health
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

The database inventory was read from the mounted runtime with Python `sqlite3` in URI read-only mode:

```bash
docker compose exec -T control-plane python - <<'PY'
import json, sqlite3
path='/data/plow-whip-web.sqlite3'
con=sqlite3.connect(f'file:{path}?mode=ro', uri=True)
con.row_factory=sqlite3.Row
m=[r[0] for r in con.execute('select version from schema_migrations order by version')]
tables=[r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
counts={t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
print(json.dumps({'database_path':path,'journal_mode':con.execute('pragma journal_mode').fetchone()[0],'integrity_check':con.execute('pragma integrity_check').fetchone()[0],'schema_migrations':m,'table_counts':counts}, indent=2))
PY
```

The repository-provided online backup API was used before deployment:

```bash
curl -fsS --max-time 30 -X POST http://127.0.0.1:8742/api/maintenance/backup
```

The first normal BuildKit attempt stopped before project build steps because this restricted Codex session could not update `~/.docker/buildx/activity/.tmp-*`:

```text
failed to update builder last activity time: ... operation not permitted
```

The existing healthy container remained running. The same documented Compose path was then run with Docker's classic builder. The current container token was inherited without printing or persisting it:

```bash
set -eu
current_container=plow-whip-web-v2-control-plane-1
bridge_token=$(docker inspect "$current_container" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^PLOW_WHIP_BRIDGE_TOKEN=//p')
test -n "$bridge_token"
DOCKER_BUILDKIT=0 PLOW_WHIP_BRIDGE_TOKEN="$bridge_token" docker compose up --build -d
```

Post-deploy and restart verification used:

```bash
docker compose ps --all --format json
curl -fsS --max-time 8 http://127.0.0.1:8742/health
curl -fsS --max-time 15 -X POST http://127.0.0.1:8742/api/providers/codex/probe
docker compose restart control-plane
docker inspect plow-whip-web-v2-control-plane-1 --format '{{json .State}} {{json .RestartCount}} {{json .Image}}'
docker compose logs --no-color --since 10m control-plane
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

## Pre-deploy state and backup

| Item | Evidence before deployment |
|---|---|
| Container | `df1d22204000`, running about 7 hours, `healthy`, restart count `0` |
| Image | `sha256:eb4046cd08967d029a61f850d37128a1c9673cbc2c6223b8595c7627f2acb197` |
| Database | `/data/plow-whip-web.sqlite3`, WAL, `PRAGMA integrity_check=ok` |
| Data volume | `plow-whip-web-v2-data:/data` |
| Project volume | `plow-whip-web-v2-projects:/projects` |
| Migrations | 13, through `0013_simple_worker.sql` |
| Preserved rows | tasks 38, workers 4, provider configs 5, projects 1, system settings 1 |
| Host Bridge | PID `51418`, listening on TCP `*:8765` |

Backup result:

```json
{
  "filename": "plow-whip-20260717T130437061358Z.sqlite3",
  "path": "/data/backups/plow-whip-20260717T130437061358Z.sqlite3",
  "bytes": 5660672,
  "sha256": "efa6a256c8b42a6bedb668a52f4a7c8f41c381e058f15bc122779f63770fe245",
  "integrity": "ok"
}
```

The backup was independently reopened read-only and contained 13 migrations, 38 tasks, 4 workers, and 5 provider configs. It remains inside the named volume and is not in Git.

## Image, container, health, and migrations

| Item | Final evidence |
|---|---|
| Image tag | `plow-whip-web-v2:local` |
| Image ID | `sha256:deab5b1d24cfc76060d91002335007884f844295ae516b01293dd7f877650136` |
| Image created | `2026-07-17T13:06:44.60713434Z` |
| Container ID | `3f34335921c3e20c7a2c25f23742dbe7e8796c4e250aef605b2c61f29324dede` |
| Port | `127.0.0.1:8742 -> 8742/tcp` |
| Mounts | Original `plow-whip-web-v2-data:/data` and `plow-whip-web-v2-projects:/projects` |
| Health | `running`, `healthy`, failing streak `0`, OOM killed `false`, restarting `false`, dead `false` |
| Restart observation | Explicit restart completed; after another full 30-second healthcheck interval, PID/StartedAt were stable and restart count was `0` |

Host curl after deployment and again after restart returned HTTP 200:

```json
{"status":"ok","version":"0.1.0","database":{"status":"ok","journal_mode":"wal","migration_count":16}}
```

`schema_migrations` is ordered through:

```text
0001_initial.sql
0002_tasks.sql
0003_workforce.sql
0004_scheduler.sql
0005_context_usage.sql
0006_resilience.sql
0007_release_security.sql
0008_embedded_cron.sql
0009_worker_provider_pool.sql
0010_cli_capabilities.sql
0011_host_jobs.sql
0012_token_usage_idempotency.sql
0013_simple_worker.sql
0014_token_reservations.sql
0015_model_call_accounting.sql
0016_task_sizing_budget.sql
```

Final database checks remained `journal_mode=wal` and `integrity_check=ok`.

## Task estimate API

The following requests were posted to `POST /api/tasks/estimate`; all four planning gates were true unless noted.

XS input:

```json
{"layers_touched":1,"components_touched":1,"estimated_files_changed":1,"has_migration":false,"has_deploy":false,"verification_commands_count":1,"estimated_verification_seconds":60,"external_dependencies_count":0,"risk_level":"low","independent_review_required":false,"gate_artifact":true,"gate_boundary":true,"gate_verification":true,"gate_dependency":true}
```

Result: `size_class=XS`, `model_invoked=false`, total hard cap `37500`, soft/hard deadline `120/300`, max attempts `2`.

M input:

```json
{"layers_touched":1,"components_touched":3,"estimated_files_changed":4,"has_migration":false,"has_deploy":false,"verification_commands_count":1,"estimated_verification_seconds":180,"external_dependencies_count":0,"risk_level":"medium","independent_review_required":false,"gate_artifact":true,"gate_boundary":true,"gate_verification":true,"gate_dependency":true}
```

Result: `size_class=M`, `model_invoked=false`, input p90 `112500`, output p90 `37500`, total hard cap `225000`, reserved tokens `150000`, soft/hard deadline `480/1200`, max turns `40`, max attempts `3`, verification timeout `300`. The M hard cap, hard deadline, and attempts are not the old fixed `50000/600/2` values.

With the same M input and `independent_review_required=true`, the API returned:

```json
{
  "status": "needs_planning",
  "missing_gates": ["independent_review_orchestration"],
  "size_class": null,
  "estimated_input_tokens": null,
  "estimated_output_tokens": null,
  "soft_deadline_seconds": null,
  "hard_deadline_seconds": null,
  "max_turns": null,
  "max_attempts": null,
  "verification_timeout_seconds": null,
  "progress_extension_seconds": null,
  "total_token_hard_cap": null,
  "reserved_tokens": null,
  "model_invoked": false
}
```

## Existing data preservation and readability

API reads after migration returned one project, 38 tasks, four nested workers, five providers, and Settings revision 7. The active task and Worker remained linked:

```text
task c29fec9a-60a6-4e14-8884-81a8509b5c4a status=running worker_id=b6f9eda2-4334-4ef7-a161-b3185a1d632d
worker b6f9eda2-4334-4ef7-a161-b3185a1d632d role=devops_sre provider=codex status=busy active_task_id=c29fec9a-60a6-4e14-8884-81a8509b5c4a
```

Provider reads returned Codex and Cursor available, Generic Command available, optional simple-worker unavailable, and Claude disabled. Settings retained all 15 existing keys. Final direct database counts matched the pre-deploy counts exactly for tasks, workers, provider configs, projects, and system settings.

## Static frontend evidence

The production HTML was fetched over HTTP, its referenced JavaScript asset was resolved from the HTML, and `/assets/index-DeVcbXdX.js` was downloaded from the deployed service. Bundle size was 306295 bytes.

| Text | Bundle occurrences |
|---|---:|
| `验证机制` | 1 |
| `0 Token` | 5 |
| `高级预判` | 1 |
| `执行 0 Token 预判` | 1 |
| `快速` | 0 |
| `均衡` | 0 |
| `严格` | 0 |

This is repeatable static/HTTP evidence that the new verification and 0 Token task-preflight semantics are deployed and the old three creation-form quality options are absent. Per task boundary, final browser interaction remains for the control session.

## Host Bridge and logs

The independent Host Bridge process was not killed or restarted. Before and after deployment, and after the control-plane restart, `lsof` reported the same PID `51418` listening on TCP 8765. `POST /api/providers/codex/probe` succeeded after deployment and again after restart through the authenticated control-plane-to-Bridge path:

```json
{"name":"codex","status":"available","reason":"codex-cli 0.145.0-alpha.18","transport":"host-bridge","last_probed_at":"2026-07-17 13:08:33"}
```

Recent container logs show normal startup, HTTP 200 responses, an orderly shutdown caused by the explicit restart, and successful startup afterward. No traceback, migration error, OOM, repeated unexpected exit, or crash loop was observed.

## Remaining risks

- Final interactive browser acceptance was intentionally not performed in this DevOps task; the control session owns it.
- The optional `simple-worker` remains unavailable because its optional provider credentials are not configured; this did not affect Codex Bridge deployment or the required checks.
- This restricted session could not use Buildx because its activity directory was not writable. Deployment succeeded with the same Compose definition and Docker classic builder; a future Buildx-only automation environment must provide a writable Docker config/activity path.

SPRINT10_LOCAL_DEPLOY_COMPLETE
