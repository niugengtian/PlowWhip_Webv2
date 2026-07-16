from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from plow_whip_web.store.database import Database
from plow_whip_web.store.health_repository import HealthRepository
from plow_whip_web.store.provider_repository import ProviderRepository
from plow_whip_web.store.settings_repository import SettingsRepository


class MaintenanceService:
    def __init__(
        self,
        data_dir: Path,
        database: Database,
        settings: SettingsRepository,
        health: HealthRepository,
        providers: ProviderRepository,
    ) -> None:
        self.data_dir = data_dir
        self.database = database
        self.settings = settings
        self.health = health
        self.providers = providers

    def backup(self) -> dict[str, Any]:
        directory = self.data_dir / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"plow-whip-{stamp}.sqlite3"
        source = self.database.connect()
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        integrity = sqlite3.connect(target)
        try:
            result = integrity.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            integrity.close()
        if result != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError(f"backup integrity failed: {result}")
        return {
            "filename": target.name, "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "integrity": result,
        }

    def export_metadata(self) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            projects = [dict(row) for row in connection.execute(
                "SELECT id, name, path, status, created_at, updated_at FROM projects ORDER BY id"
            )]
            tasks = [dict(row) for row in connection.execute(
                """
                SELECT id, title, objective, project_id, role_id, worker_id, status, revision,
                       attempts_used, max_attempts, token_budget, tokens_used, provider,
                       quality_profile, created_at, updated_at
                FROM tasks ORDER BY id
                """
            )]
            migrations = [row["version"] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
        finally:
            connection.close()
        return {
            "format": "plow-whip-web-v2-metadata-v1", "projects": projects,
            "tasks": tasks, "migrations": migrations, "secrets_included": False,
        }

    def diagnostics(self) -> dict[str, Any]:
        directory = self.data_dir / "diagnostics"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = directory / f"diagnostics-{stamp}.zip"
        payloads = {
            "health.json": self.health.status(),
            "settings.json": self.settings.get(),
            "providers.json": self.providers.list(),
            "metadata.json": self.export_metadata(),
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in payloads.items():
                archive.writestr(name, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return {
            "filename": target.name, "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "secrets_included": False,
        }

    def restore_backup(self, filename: str) -> dict[str, Any]:
        if Path(filename).name != filename:
            raise ValueError("backup filename must not contain a path")
        source_path = self.data_dir / "backups" / filename
        if not source_path.is_file():
            raise FileNotFoundError(filename)
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
        try:
            if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup integrity check failed")
            self.backup()
            destination = self.database.connect()
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return {"restored": filename, "integrity": "ok"}
