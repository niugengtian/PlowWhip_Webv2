from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def test_health_reports_wal_and_migration() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory)))
        with TestClient(app) as client:
            response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"]["journal_mode"] == "wal"
    assert payload["database"]["migration_count"] == 2


def test_capabilities_are_zero_token_and_desktop_free() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory)))
        with TestClient(app) as client:
            response = client.get("/api/system/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "web_control_plane": True,
        "desktop_required": False,
        "model_invoked": False,
        "sprint": 1,
    }
