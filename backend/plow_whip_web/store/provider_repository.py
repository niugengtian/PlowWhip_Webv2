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
                """
                SELECT * FROM provider_configs
                ORDER BY enabled DESC, priority, name
                """
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
        failure_threshold: int = 3,
        recovery_successes: int = 1,
        open_seconds: int = 60,
        failure_class: str | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM provider_configs WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"provider not found: {name}")
            failures = (
                0
                if available
                else int(row["consecutive_failures"]) + 1
            )
            successes = (
                int(row["consecutive_successes"]) + 1
                if available else 0
            )
            circuit_state = str(row["circuit_state"])
            if not row["enabled"]:
                circuit_state = "closed"
            elif available and successes >= max(1, recovery_successes):
                circuit_state = "closed"
                failures = 0
            elif not available and failures >= max(1, failure_threshold):
                circuit_state = "open"
            elif circuit_state == "open":
                circuit_state = "half_open"
            retain_last_known_good = bool(
                not available
                and circuit_state == "closed"
                and failures < max(1, failure_threshold)
                and row["status"] == "available"
            )
            status = (
                "disabled"
                if not row["enabled"]
                else "available"
                if (
                    circuit_state == "closed"
                    and (available or retain_last_known_good)
                )
                else "unavailable"
            )
            config = json.loads(row["config_json"] or "{}")
            if readiness is not None:
                config["readiness"] = readiness
            connection.execute(
                """
                UPDATE provider_configs SET status = ?, reason = ?,
                    config_json = ?, last_probed_at = CURRENT_TIMESTAMP,
                    circuit_state = ?, consecutive_failures = ?,
                    consecutive_successes = ?,
                    circuit_opened_at = CASE
                        WHEN ? = 'open' AND circuit_state != 'open'
                        THEN CURRENT_TIMESTAMP
                        WHEN ? = 'closed' THEN NULL
                        ELSE circuit_opened_at END,
                    next_probe_at = CASE
                        WHEN ? = 'open' THEN datetime('now', ?)
                        WHEN ? = 'closed' THEN NULL
                        ELSE next_probe_at END,
                    last_failure_class = CASE
                        WHEN ? THEN NULL ELSE ? END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE name = ?
                """,
                (
                    status,
                    detail,
                    _dump(config),
                    circuit_state,
                    failures,
                    successes,
                    circuit_state,
                    circuit_state,
                    circuit_state,
                    f"+{max(5, open_seconds)} seconds",
                    circuit_state,
                    int(available),
                    failure_class,
                    name,
                ),
            )
        return self.require(name)

    def probe_allowed(self, name: str) -> bool:
        """Reserve the one half-open probe after a circuit cool-down."""
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT enabled, circuit_state,
                       next_probe_at IS NULL
                       OR next_probe_at <= CURRENT_TIMESTAMP AS due
                FROM provider_configs WHERE name = ?
                """,
                (name,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"provider not found: {name}")
            if not row["enabled"]:
                return False
            state = str(row["circuit_state"])
            if state == "closed":
                return True
            if state == "open" and row["due"]:
                cursor = connection.execute(
                    """
                    UPDATE provider_configs
                    SET circuit_state = 'half_open', updated_at = CURRENT_TIMESTAMP
                    WHERE name = ? AND circuit_state = 'open'
                    """,
                    (name,),
                )
                return cursor.rowcount == 1
            return False

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
                "deepseek-chat"
                if row["name"] in {"simple-worker", "deepseek"}
                else "moonshot-v1"
                if row["name"] == "kimi"
                else "provider-managed"
            ),
            "readiness": readiness,
            "network_zone": row["network_zone"],
            "priority": int(row["priority"]),
            "circuit_state": row["circuit_state"],
            "consecutive_failures": int(row["consecutive_failures"]),
            "consecutive_successes": int(row["consecutive_successes"]),
            "circuit_opened_at": row["circuit_opened_at"],
            "next_probe_at": row["next_probe_at"],
            "last_failure_class": row["last_failure_class"],
        }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
