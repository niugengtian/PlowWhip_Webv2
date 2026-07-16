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
    assert payload["database"]["migration_count"] == 10


def test_capabilities_are_zero_token_and_desktop_free() -> None:
    with TemporaryDirectory() as directory:
        app = create_app(Settings(data_dir=Path(directory)))
        with TestClient(app) as client:
            response = client.get("/api/system/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["web_control_plane"] is True
    assert payload["desktop_required"] is False
    assert payload["model_invoked"] is False
    assert payload["multi_project"] is True
    assert payload["durable_worker_sessions"] is True
    assert payload["zero_token_scheduler"] is True
    assert payload["compiled_context"] is True
    assert payload["three_scope_conventions"] is True
    assert payload["fault_recovery"] is True
    assert payload["anti_loop_guards"] is True
    assert payload["audited_permissions"] is True
    assert payload["loopback_default"] is True
    assert payload["container_runtime"] is True
    assert payload["host_scheduler_required"] is False
    assert payload["embedded_cron"] is True
    assert payload["docker_managed_sqlite"] is True
    assert payload["worker_provider_pool"] is True
    assert payload["restricted_host_bridge"] is True
    assert payload["platform_api_key_required"] is False
    assert payload["sprint"] == 8
