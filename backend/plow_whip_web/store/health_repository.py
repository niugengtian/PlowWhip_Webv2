from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from plow_whip_web.runtime.connectivity import ConnectivityResult
from plow_whip_web.store.database import Database


class HealthRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, connectivity: ConnectivityResult, *, expected_interval_seconds: int) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM runtime_health WHERE id = 'global'").fetchone()
            resumed = False
            if row["last_tick_at"]:
                previous = datetime.fromisoformat(row["last_tick_at"] + "+00:00")
                resumed = (now - previous).total_seconds() > expected_interval_seconds * 3
            failures = 0 if connectivity.state == "online" else row["consecutive_failures"] + 1
            connection.execute(
                """
                UPDATE runtime_health SET connectivity = ?, domestic_ok = ?, overseas_ok = ?,
                    last_tick_at = ?, last_resume_at = CASE WHEN ? THEN ? ELSE last_resume_at END,
                    consecutive_failures = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 'global'
                """,
                (
                    connectivity.state, int(connectivity.domestic_ok), int(connectivity.overseas_ok),
                    now.strftime("%Y-%m-%d %H:%M:%S"), int(resumed),
                    now.strftime("%Y-%m-%d %H:%M:%S"), failures,
                ),
            )
            return {
                "connectivity": connectivity.state, "domestic_ok": connectivity.domestic_ok,
                "overseas_ok": connectivity.overseas_ok, "sleep_resumed": resumed,
                "consecutive_failures": failures, "model_invoked": False,
            }

    def status(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            return dict(connection.execute("SELECT * FROM runtime_health WHERE id = 'global'").fetchone())
        finally:
            connection.close()
