from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.store.convention_repository import (
    DEFAULT_GLOBAL_CONVENTION_PATH,
    default_global_convention,
)
from plow_whip_web.store.database import migration_contract


def test_fresh_install_bootstraps_portable_sqlite_catalog() -> None:
    with TemporaryDirectory() as directory:
        runtime = Path(directory) / "runtime"
        app = create_app(Settings(data_dir=runtime))
        with TestClient(app) as client:
            health = client.get("/health").json()["database"]
            convention = client.get("/api/conventions/global/global").json()
            rules = client.get("/api/rules").json()["items"]
            templates = client.get("/api/role-templates").json()["items"]
            providers = client.get("/api/providers").json()

        assert health == {
            "status": "ok",
            "journal_mode": "wal",
            **migration_contract(),
        }
        assert convention["present"] is True
        assert convention["revision"] == 1
        assert convention["content"] == default_global_convention()
        assert "Major-change incident-ledger gate" in convention["content"]
        assert len(rules) >= 7
        assert len(templates) >= 7
        assert {item["name"] for item in providers} >= {
            "codex", "cursor", "deepseek", "kimi", "generic-command",
        }

        with app.state.database.connect() as connection:
            global_butler = connection.execute(
                """
                SELECT role_kind FROM global_butler_identity
                WHERE id = 'global'
                """
            ).fetchone()
        assert global_butler["role_kind"] == "global_butler"
        assert DEFAULT_GLOBAL_CONVENTION_PATH.is_file()


def test_bootstrap_never_overwrites_an_existing_global_convention() -> None:
    with TemporaryDirectory() as directory:
        runtime = Path(directory) / "runtime"
        app = create_app(Settings(data_dir=runtime))
        with TestClient(app) as client:
            saved = client.put(
                "/api/conventions",
                json={
                    "scope": "global",
                    "scope_id": "global",
                    "content": "owner-managed global convention",
                    "expected_revision": 1,
                },
            )
            assert saved.status_code == 200
            assert saved.json()["revision"] == 2

        restarted = create_app(Settings(data_dir=runtime))
        with TestClient(restarted) as client:
            persisted = client.get("/api/conventions/global/global").json()

        assert persisted["content"] == "owner-managed global convention"
        assert persisted["revision"] == 2
