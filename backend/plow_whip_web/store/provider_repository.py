from __future__ import annotations

import json
from typing import Any

from plow_whip_web.domain.model import NotFoundError, RevisionConflictError
from plow_whip_web.store.database import Database


class ProviderRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM provider_configs ORDER BY enabled DESC, name"
            ).fetchall()
            return [self._view(row) for row in rows]
        finally:
            connection.close()

    def get(self, name: str) -> dict[str, Any] | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM provider_configs WHERE name = ?", (name,)
            ).fetchone()
            return self._view(row) if row is not None else None
        finally:
            connection.close()

    def require(self, name: str) -> dict[str, Any]:
        provider = self.get(name)
        if provider is None:
            raise NotFoundError(f"provider not found: {name}")
        return provider

    def put(
        self,
        *,
        name: str,
        display_name: str,
        adapter: str,
        transport: str,
        executable: str | None,
        enabled: bool,
        credential_env: str | None,
        capabilities: list[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT revision FROM provider_configs WHERE name = ?", (name,)
            ).fetchone()
            if current is None:
                if expected_revision != 0:
                    raise RevisionConflictError("new provider must start at revision 0")
                connection.execute(
                    """
                    INSERT INTO provider_configs(
                        name, display_name, status, model_invoked, capabilities_json,
                        reason, adapter, transport, executable, enabled, credential_env,
                        config_json, revision
                    ) VALUES (?, ?, 'unknown', ?, ?, '等待探测', ?, ?, ?, ?, ?, '{}', 1)
                    """,
                    (
                        name, display_name, 0 if adapter == "generic-command" else 1,
                        _dump(capabilities), adapter, transport, executable,
                        int(enabled), credential_env,
                    ),
                )
            else:
                if int(current["revision"]) != expected_revision:
                    raise RevisionConflictError(
                        f"expected revision {expected_revision}, current revision {current['revision']}"
                    )
                status = "unknown" if enabled else "disabled"
                reason = "等待探测" if enabled else "已停用"
                connection.execute(
                    """
                    UPDATE provider_configs SET display_name = ?, adapter = ?, transport = ?,
                        executable = ?, enabled = ?, credential_env = ?, capabilities_json = ?,
                        model_invoked = ?, status = ?, reason = ?, revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE name = ?
                    """,
                    (
                        display_name, adapter, transport, executable, int(enabled),
                        credential_env, _dump(capabilities),
                        0 if adapter == "generic-command" else 1, status, reason, name,
                    ),
                )
        return self.require(name)

    def record_probe(
        self,
        name: str,
        *,
        available: bool,
        detail: str,
        readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT enabled, config_json FROM provider_configs WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"provider not found: {name}")
            status = "available" if available else ("unavailable" if row["enabled"] else "disabled")
            config = json.loads(row["config_json"] or "{}")
            if readiness is not None:
                config["readiness"] = readiness
            connection.execute(
                """
                UPDATE provider_configs SET status = ?, reason = ?,
                    config_json = ?, last_probed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
                """,
                (status, detail, _dump(config), name),
            )
        return self.require(name)

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        config = json.loads(row["config_json"] or "{}")
        readiness = config.get("readiness") if isinstance(config, dict) else None
        if not isinstance(readiness, dict):
            readiness = {
                "installed": row["status"] in {"available", "unavailable", "unknown"},
                "cli_probe": row["status"],
                "session_resume_ready": False,
                "recent_execution_health": "unknown",
            }
        return {
            "name": row["name"],
            "display_name": row["display_name"] or row["name"],
            "status": row["status"],
            "model_invoked": bool(row["model_invoked"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "reason": row["reason"],
            "adapter": row["adapter"],
            "transport": row["transport"],
            "executable": row["executable"],
            "enabled": bool(row["enabled"]),
            "credential_env": row["credential_env"],
            "revision": int(row["revision"]),
            "last_probed_at": row["last_probed_at"],
            "model": config.get("model") or (
                "deepseek-chat" if row["name"] == "simple-worker" else "provider-managed"
            ),
            "readiness": readiness,
        }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
