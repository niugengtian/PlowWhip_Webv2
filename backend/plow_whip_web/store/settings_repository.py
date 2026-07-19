from __future__ import annotations

import json
from typing import Any

from plow_whip_web.domain.model import RevisionConflictError
from plow_whip_web.store.database import Database


DEFAULT_SETTINGS: dict[str, Any] = {
    "scheduler_interval_seconds": 30,
    "scheduler_lease_seconds": 90,
    "cron_enabled": True,
    "cron_expression": "*/1 * * * *",
    "cron_timezone": "Asia/Shanghai",
    "cron_misfire_policy": "catch_up_once",
    "max_parallel_workers": 4,
    "auto_dispatch": True,
    "task_default_token_budget": 50_000,
    "global_daily_token_budget": 500_000,
    "convention_refinement_token_budget": 10_000,
    "max_same_failure": 2,
    "max_no_progress": 3,
    "session_no_progress_rotation_threshold": 2,
    "context_max_bytes": 32_768,
    "checkpoint_max_bytes": 4096,
    "handoff_max_bytes": 4096,
    "observation_tail_lines": 20,
    "observation_max_bytes": 65_536,
    "rotation_max_bytes": 262_144,
}


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT revision, settings_json, updated_at FROM system_settings WHERE id = 1").fetchone()
            if row is None:
                return {"revision": 0, "values": dict(DEFAULT_SETTINGS), "updated_at": None}
            values = dict(DEFAULT_SETTINGS)
            values.update(
                {
                    key: value
                    for key, value in json.loads(row["settings_json"]).items()
                    if key in DEFAULT_SETTINGS
                }
            )
            return {"revision": row["revision"], "values": values, "updated_at": row["updated_at"]}
        finally:
            connection.close()

    def update(self, values: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT revision, settings_json FROM system_settings WHERE id = 1").fetchone()
            current_revision = row["revision"] if row else 0
            if current_revision != expected_revision:
                raise RevisionConflictError(
                    f"expected settings revision {expected_revision}, current revision {current_revision}"
                )
            merged = dict(DEFAULT_SETTINGS)
            if row:
                merged.update(
                    {
                        key: value
                        for key, value in json.loads(row["settings_json"]).items()
                        if key in DEFAULT_SETTINGS
                    }
                )
            merged.update({key: value for key, value in values.items() if key in DEFAULT_SETTINGS})
            next_revision = current_revision + 1
            payload = json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO system_settings(id, revision, settings_json, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET revision = excluded.revision,
                    settings_json = excluded.settings_json, updated_at = CURRENT_TIMESTAMP
                """,
                (next_revision, payload),
            )
        return self.get()
