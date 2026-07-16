# Local runbook

## First start

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd web && pnpm install && pnpm run build && cd ..
.venv/bin/python -m plow_whip_web --data-dir runtime
```

Open `http://127.0.0.1:8742`. Register projects, save Convention and Settings, then create tasks. The built frontend is served by FastAPI.

After the Python environment is installed, the one-command source startup is `python scripts/start_local.py`. A self-contained wheel with the built Web UI is produced by `python scripts/build_release.py`; the wheel is written below `dist/`.

## Scheduler

Settings shows the detected OS adapter and exact target. Enable authorization, save, then install. Without that explicit authorization the endpoint is a dry denial. Manual zero-token tick:

```bash
.venv/bin/python -m plow_whip_web scheduler-tick --data-dir runtime
```

## Upgrade and migration

Create a backup from Health, stop the server, update the source, reinstall editable dependencies, rebuild the frontend and start. Migrations are ordered and idempotent. Check `/health` and the migration count.

## Backup, diagnostics and restore

Health creates integrity-checked SQLite backups and secret-free diagnostic ZIPs. Restore requires the exact backup filename and the literal confirmation `RESTORE`; a safety backup is made first.

## Uninstall

Stop the server, unload/delete the user scheduler definition shown in Settings, and archive or delete this repository and its `runtime/` directory. No system-wide service or shared database is installed.
