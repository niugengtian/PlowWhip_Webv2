from __future__ import annotations

import json
from typing import Any

from plow_whip_web.domain.model import DomainError, RevisionConflictError
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
    "max_same_failure": 2,
    "max_no_progress": 3,
    "context_max_bytes": 32_768,
    "rotation_max_bytes": 262_144,
    "checkpoint_max_bytes": 4_096,
    "handoff_max_bytes": 2_048,
    "observation_tail_lines": 20,
    "observation_max_bytes": 8_192,
    "episode_wall_limit_seconds": 4_800,
    "checkpoint_interval_seconds": 120,
    "no_progress_seconds": 300,
    "max_host_processes": 2,
    "progress_extension_seconds": 120,
    "provider_failure_threshold": 3,
    "provider_recovery_successes": 1,
    "provider_open_seconds": 60,
    "network_failure_threshold": 2,
    "network_recovery_successes": 3,
    "resume_batch_size": 2,
    "alert_debounce_seconds": 30,
    "default_provider_policy": "auto",
    "default_provider_order": ["codex", "cursor", "deepseek", "kimi"],
    "default_butler_provider": "codex",
}

CONTINUITY_SETTING_KEYS = {
    "max_same_failure",
    "max_no_progress",
    "context_max_bytes",
    "rotation_max_bytes",
    "checkpoint_max_bytes",
    "handoff_max_bytes",
    "observation_tail_lines",
    "observation_max_bytes",
    "episode_wall_limit_seconds",
    "checkpoint_interval_seconds",
    "no_progress_seconds",
    "max_host_processes",
    "progress_extension_seconds",
    "provider_failure_threshold",
    "provider_recovery_successes",
    "provider_open_seconds",
    "network_failure_threshold",
    "network_recovery_successes",
    "resume_batch_size",
    "alert_debounce_seconds",
}


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT revision, settings_json, updated_at FROM system_settings WHERE id = 1").fetchone()
            if row is None:
                return {
                    "revision": 0,
                    "values": dict(DEFAULT_SETTINGS),
                    "sources": {key: "global_default" for key in DEFAULT_SETTINGS},
                    "warnings": _setting_warnings(DEFAULT_SETTINGS),
                    "updated_at": None,
                }
            values = dict(DEFAULT_SETTINGS)
            values.update(
                {
                    key: value
                    for key, value in json.loads(row["settings_json"]).items()
                    if key in DEFAULT_SETTINGS
                }
            )
            return {
                "revision": row["revision"],
                "values": values,
                "sources": {key: "global" for key in values},
                "warnings": _setting_warnings(values),
                "updated_at": row["updated_at"],
            }
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
            _validate_settings(merged)
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

    def effective(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        role_id: str | None = None,
    ) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            values, sources, revisions = resolve_effective_settings(
                connection,
                project_id=project_id,
                task_id=task_id,
                role_id=role_id,
            )
            return {
                "revision": revisions.get("global", 0),
                "override_revisions": revisions,
                "values": values,
                "sources": sources,
                "warnings": _setting_warnings(values),
                "updated_at": None,
            }
        finally:
            connection.close()

    def get_override(self, *, scope: str, scope_id: str) -> dict[str, Any]:
        _validate_override_scope(scope)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM runtime_setting_overrides
                WHERE scope = ? AND scope_id = ?
                """,
                (scope, scope_id),
            ).fetchone()
            if row is None:
                return {
                    "scope": scope,
                    "scope_id": scope_id,
                    "revision": 0,
                    "values": {},
                    "updated_at": None,
                }
            item = dict(row)
            item["values"] = json.loads(item.pop("values_json"))
            return item
        finally:
            connection.close()

    def update_override(
        self,
        *,
        scope: str,
        scope_id: str,
        values: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        _validate_override_scope(scope)
        unknown = set(values) - CONTINUITY_SETTING_KEYS
        if unknown:
            raise DomainError(
                "override only accepts continuity settings: "
                + ", ".join(sorted(unknown))
            )
        with self.database.transaction(immediate=True) as connection:
            target = _override_target(connection, scope=scope, scope_id=scope_id)
            row = connection.execute(
                """
                SELECT revision, values_json FROM runtime_setting_overrides
                WHERE scope = ? AND scope_id = ?
                """,
                (scope, scope_id),
            ).fetchone()
            revision = int(row["revision"]) if row else 0
            if revision != expected_revision:
                raise RevisionConflictError(
                    f"expected override revision {expected_revision}, current revision {revision}"
                )
            merged_override = json.loads(row["values_json"]) if row else {}
            merged_override.update(values)
            base, _, _ = resolve_effective_settings(
                connection,
                project_id=target["project_id"],
                task_id=None if scope == "project" else target["task_id"],
                role_id=None if scope == "project" else target["role_id"],
                exclude_scope=scope,
            )
            effective = {**base, **merged_override}
            _validate_settings(effective)
            next_revision = revision + 1
            connection.execute(
                """
                INSERT INTO runtime_setting_overrides(
                    scope, scope_id, project_id, task_id, role_id,
                    revision, values_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, scope_id) DO UPDATE SET
                    revision = excluded.revision,
                    values_json = excluded.values_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    scope,
                    scope_id,
                    target["project_id"],
                    target.get("task_id"),
                    target.get("role_id"),
                    next_revision,
                    json.dumps(
                        merged_override,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return {
            **self.get_override(scope=scope, scope_id=scope_id),
            "effective": self.effective(
                project_id=target["project_id"],
                task_id=target.get("task_id"),
                role_id=target.get("role_id"),
            ),
        }


def resolve_effective_settings(
    connection: Any,
    *,
    project_id: str | None,
    task_id: str | None,
    role_id: str | None,
    exclude_scope: str | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, int]]:
    values = dict(DEFAULT_SETTINGS)
    sources = {key: "global_default" for key in values}
    revisions: dict[str, int] = {"global": 0}
    global_row = connection.execute(
        "SELECT revision, settings_json FROM system_settings WHERE id = 1"
    ).fetchone()
    if global_row:
        revisions["global"] = int(global_row["revision"])
        for key, value in json.loads(global_row["settings_json"]).items():
            if key in DEFAULT_SETTINGS:
                values[key] = value
                sources[key] = "global"
    if task_id:
        task = connection.execute(
            "SELECT project_id, role_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise DomainError(f"task not found: {task_id}")
        project_id = task["project_id"]
        if role_id is not None and role_id != task["role_id"]:
            raise DomainError("task-role settings role does not match the task")
        role_id = task["role_id"]
    scopes = []
    if project_id and exclude_scope != "project":
        scopes.append(("project", project_id, "project"))
    if task_id and role_id and exclude_scope != "task_role":
        scopes.append(("task_role", task_id, "task_role"))
    for scope, scope_id, source in scopes:
        row = connection.execute(
            """
            SELECT revision, values_json FROM runtime_setting_overrides
            WHERE scope = ? AND scope_id = ?
            """,
            (scope, scope_id),
        ).fetchone()
        if not row:
            continue
        revisions[source] = int(row["revision"])
        for key, value in json.loads(row["values_json"]).items():
            if key in CONTINUITY_SETTING_KEYS:
                values[key] = value
                sources[key] = source
    _validate_settings(values)
    return values, sources, revisions


def _override_target(
    connection: Any, *, scope: str, scope_id: str
) -> dict[str, Any]:
    if scope == "project":
        row = connection.execute(
            "SELECT id project_id FROM projects WHERE id = ?", (scope_id,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id task_id, project_id, role_id FROM tasks WHERE id = ?",
            (scope_id,),
        ).fetchone()
    if row is None:
        raise DomainError(f"{scope} settings target not found: {scope_id}")
    return dict(row)


def _validate_override_scope(scope: str) -> None:
    if scope not in {"project", "task_role"}:
        raise DomainError("settings override scope must be project or task_role")


def _validate_settings(values: dict[str, Any]) -> None:
    checkpoint = int(values["checkpoint_max_bytes"])
    handoff = int(values["handoff_max_bytes"])
    context = int(values["context_max_bytes"])
    if checkpoint < 512 or handoff < 256:
        raise DomainError("checkpoint/handoff limits are too small for structured evidence")
    if checkpoint + handoff + 2048 > context:
        raise DomainError(
            "checkpoint + handoff + 2048-byte mandatory context reserve exceeds context_max_bytes"
        )
    if int(values["observation_tail_lines"]) < 1:
        raise DomainError("observation_tail_lines must be positive")
    if int(values["observation_max_bytes"]) < 1024:
        raise DomainError("observation_max_bytes must be at least 1024")
    if int(values["episode_wall_limit_seconds"]) < 60:
        raise DomainError("episode_wall_limit_seconds must be at least 60")
    if int(values["checkpoint_interval_seconds"]) >= int(
        values["episode_wall_limit_seconds"]
    ):
        raise DomainError(
            "checkpoint_interval_seconds must be below episode_wall_limit_seconds"
        )
    if int(values["no_progress_seconds"]) < int(values["checkpoint_interval_seconds"]):
        raise DomainError(
            "no_progress_seconds must be at least checkpoint_interval_seconds"
        )
    if int(values["progress_extension_seconds"]) > int(
        values["episode_wall_limit_seconds"]
    ):
        raise DomainError(
            "progress_extension_seconds cannot exceed episode_wall_limit_seconds"
        )
    if values["default_provider_policy"] not in {"auto", "preferred", "pinned"}:
        raise DomainError("default_provider_policy must be auto, preferred, or pinned")
    order = values["default_provider_order"]
    if (
        not isinstance(order, list)
        or not order
        or any(not isinstance(item, str) or not item for item in order)
        or len(order) != len(set(order))
    ):
        raise DomainError("default_provider_order must be a non-empty unique string list")


def _setting_warnings(values: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    context_payload = int(values["checkpoint_max_bytes"]) + int(
        values["handoff_max_bytes"]
    )
    if context_payload * 2 > int(values["context_max_bytes"]):
        warnings.append("checkpoint 与 handoff 已占用超过一半的 Context 上限")
    if int(values["observation_max_bytes"]) >= int(values["rotation_max_bytes"]):
        warnings.append("单次观察字节上限不应达到或超过文件轮转阈值")
    if int(values["no_progress_seconds"]) * 2 > int(
        values["episode_wall_limit_seconds"]
    ):
        warnings.append("无进展窗口超过 Episode wall limit 的一半，故障收敛可能偏慢")
    return warnings
