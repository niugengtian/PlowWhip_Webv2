from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings
from plow_whip_web.runtime.model_call_ledger import (
    UNKNOWN_PROJECT_KEY,
    UNKNOWN_TASK_KEY,
    USAGE_TIMEZONE,
)


SHANGHAI = ZoneInfo(USAGE_TIMEZONE)


def _app(directory: str):
    return create_app(Settings(data_dir=Path(directory) / "runtime"))


def _force_settled_at(app, call_id: str, settled_at: str) -> None:
    with app.state.database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE model_calls
            SET settled_at = ?, created_at = ?, updated_at = ?
            WHERE call_id = ?
            """,
            (settled_at, settled_at, settled_at, call_id),
        )


def _settle(
    ledger,
    *,
    key: str,
    project_id: str | None = None,
    task_id: str | None = None,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    session_id: str | None = None,
    session_generation: int | None = 1,
) -> str:
    receipt = ledger.prepare(
        idempotency_key=key,
        call_kind="executor",
        provider="codex",
        project_id=project_id,
        session_id=session_id,
        session_generation=session_generation,
    )
    if task_id is not None:
        with ledger.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE model_calls SET task_id = ? WHERE call_id = ?",
                (task_id, receipt["call_id"]),
            )
    ledger.settle(
        receipt["call_id"],
        {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        },
        session_id=session_id,
    )
    return receipt["call_id"]


def test_daily_series_fills_zeros_and_uses_asia_shanghai_day_boundary() -> None:
    with TemporaryDirectory() as directory:
        app = _app(directory)
        ledger = app.state.model_calls
        # 2026-07-18 23:30 Shanghai == 2026-07-18 15:30 UTC → day 18
        # 2026-07-19 00:30 Shanghai == 2026-07-18 16:30 UTC → day 19
        before = _settle(
            ledger,
            key="before-midnight",
            input_tokens=40,
            cached_input_tokens=10,
            output_tokens=5,
        )
        after = _settle(
            ledger,
            key="after-midnight",
            input_tokens=70,
            cached_input_tokens=20,
            output_tokens=8,
        )
        _force_settled_at(app, before, "2026-07-18 15:30:00")
        _force_settled_at(app, after, "2026-07-18 16:30:00")

        series = ledger.daily_series(
            start=datetime(2026, 7, 17, tzinfo=SHANGHAI).date(),
            end=datetime(2026, 7, 19, tzinfo=SHANGHAI).date(),
        )
        assert series["timezone"] == "Asia/Shanghai"
        assert [day["date"] for day in series["days"]] == [
            "2026-07-17",
            "2026-07-18",
            "2026-07-19",
        ]
        assert series["days"][0]["total_tokens"] == 0
        assert series["days"][1] == {
            "date": "2026-07-18",
            "input_tokens": 40,
            "cached_input_tokens": 10,
            "uncached_input_tokens": 30,
            "output_tokens": 5,
            "total_tokens": 45,
            "calls": 1,
        }
        assert series["days"][2]["total_tokens"] == 78
        assert series["days"][2]["input_tokens"] == 70
        assert series["days"][2]["cached_input_tokens"] == 20
        assert series["days"][2]["uncached_input_tokens"] == 50
        assert series["days"][2]["output_tokens"] == 8
        assert series["totals"]["total_tokens"] == 123


def test_daily_api_range_and_day_breakdown_with_unknown_attribution() -> None:
    with TemporaryDirectory() as directory:
        app = _app(directory)
        client = TestClient(app)
        project_path = Path(directory) / "project"
        project_path.mkdir()
        project = app.state.project_repository.create(
            name="Alpha", path=str(project_path)
        )
        role_id = app.state.project_repository.resolve_role(
            str(project["id"]), "fullstack"
        )["role_id"]
        task = app.state.task_repository.create(
            title="history-task",
            objective="track tokens",
            project_path=str(project["path"]),
            project_id=str(project["id"]),
            role_id=str(role_id),
            provider="codex",
            command={"argv": ["true"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=2,
            idempotency_key="history-task",
        )
        ledger = app.state.model_calls
        known = _settle(
            ledger,
            key="known-call",
            project_id=str(project["id"]),
            task_id=task.id,
            input_tokens=100,
            cached_input_tokens=40,
            output_tokens=10,
            session_id="sess-a",
        )
        orphan = _settle(
            ledger,
            key="orphan-call",
            project_id=None,
            task_id=None,
            input_tokens=30,
            cached_input_tokens=5,
            output_tokens=7,
            session_id="sess-b",
        )
        _force_settled_at(app, known, "2026-07-15 04:00:00")
        _force_settled_at(app, orphan, "2026-07-15 05:00:00")

        ranged = client.get(
            "/api/usage/daily",
            params={"start": "2026-07-14", "end": "2026-07-15"},
        )
        assert ranged.status_code == 200
        body = ranged.json()
        assert body["from"] == "2026-07-14"
        assert body["to"] == "2026-07-15"
        assert body["days"][0]["total_tokens"] == 0
        assert body["days"][1]["total_tokens"] == 147

        detail = client.get("/api/usage/daily/2026-07-15").json()
        assert detail["total_tokens"] == 147
        assert sum(item["tokens"] for item in detail["projects"]) == 147
        assert sum(item["tokens"] for item in detail["tasks"]) == 147
        project_keys = {item["key"] for item in detail["projects"]}
        task_keys = {item["key"] for item in detail["tasks"]}
        assert str(project["id"]) in project_keys
        assert UNKNOWN_PROJECT_KEY in project_keys
        assert task.id in task_keys
        assert UNKNOWN_TASK_KEY in task_keys
        unknown_project = next(
            item for item in detail["projects"] if item["key"] == UNKNOWN_PROJECT_KEY
        )
        assert unknown_project["label"] == "未知/已删除项目"
        unknown_task = next(
            item for item in detail["tasks"] if item["key"] == UNKNOWN_TASK_KEY
        )
        assert unknown_task["label"] == "未知/已删除任务"


def test_provider_cumulative_snapshots_do_not_double_count_across_days() -> None:
    with TemporaryDirectory() as directory:
        app = _app(directory)
        ledger = app.state.model_calls
        first = _settle(
            ledger,
            key="snap-1",
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=10,
            session_id="physical",
        )
        second = _settle(
            ledger,
            key="snap-2",
            input_tokens=160,
            cached_input_tokens=140,
            output_tokens=15,
            session_id="physical",
        )
        _force_settled_at(app, first, "2026-07-10 02:00:00")
        _force_settled_at(app, second, "2026-07-11 02:00:00")

        series = ledger.daily_series(
            start=datetime(2026, 7, 10, tzinfo=SHANGHAI).date(),
            end=datetime(2026, 7, 11, tzinfo=SHANGHAI).date(),
        )
        assert series["days"][0]["total_tokens"] == 110
        assert series["days"][1]["total_tokens"] == 65
        assert series["totals"]["total_tokens"] == 175
        assert series["totals"]["input_tokens"] == 160
        assert series["totals"]["cached_input_tokens"] == 140
        summary = ledger.summary()
        assert summary["total_tokens"] == 175
        assert summary["raw_snapshot_totals"]["total_tokens"] == 285


def test_daily_api_rejects_oversized_range() -> None:
    with TemporaryDirectory() as directory:
        client = TestClient(_app(directory))
        response = client.get(
            "/api/usage/daily",
            params={"start": "2026-01-01", "end": "2026-07-01"},
        )
        assert response.status_code == 400
        assert "90" in response.json()["detail"]


FIXTURE_TASK_ID = "dac716ed-4f78-4736-be68-7305e079519d"


def test_exact_fixture_task_tokens_preserve_ledger_semantics() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        project_dir = root / "project"
        project_dir.mkdir()
        app = _app(directory)
        project = app.state.project_repository.create(
            name="fixture-project",
            path=str(project_dir),
            host_path=str(project_dir),
        )
        connection = app.state.database.connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO tasks(
                    id, title, objective, project_path, status, revision,
                    command_json, verification_json, max_attempts, token_budget,
                    project_id, provider, quality_profile, sizing_json
                ) VALUES (?, 'exact fixture', 'preserve ledger totals', ?, 'ready', 0,
                          '{}', '[]', 1, 0, ?, 'codex', 'deterministic', '{"status":"legacy_fallback"}')
                """,
                (FIXTURE_TASK_ID, str(project_dir), str(project["id"])),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
        finally:
            connection.close()
        ledger = app.state.model_calls
        call_id = _settle(
            ledger,
            key=f"fixture-{FIXTURE_TASK_ID}",
            project_id=str(project["id"]),
            task_id=FIXTURE_TASK_ID,
            input_tokens=107_946,
            cached_input_tokens=0,
            output_tokens=37_209,
            session_id=f"session-{FIXTURE_TASK_ID}",
            session_generation=1,
        )
        # Replay settle must not double-count.
        ledger.settle(
            call_id,
            {
                "input_tokens": 107_946,
                "cached_input_tokens": 0,
                "output_tokens": 37_209,
            },
            session_id=f"session-{FIXTURE_TASK_ID}",
        )
        summary = ledger.summary()
        task_row = next(
            item for item in summary["tasks"] if item["task_id"] == FIXTURE_TASK_ID
        )
        assert task_row["input_tokens"] == 107_946
        assert task_row["cached_input_tokens"] == 0
        assert task_row["uncached_input_tokens"] == 107_946
        assert task_row["output_tokens"] == 37_209
        assert task_row["tokens"] == 145_155
        assert summary["input_tokens"] == 107_946
        assert summary["cached_input_tokens"] == 0
        assert summary["uncached_input_tokens"] == 107_946
        assert summary["output_tokens"] == 37_209
        assert summary["total_tokens"] == 145_155
        assert summary["scope"] == "all_history"
        assert summary["ratios"]["is_budget_gate"] is False
        assert summary["ratios"]["is_quality_gate"] is False
        assert summary["ratios"]["input_per_output"] == pytest.approx(107_946 / 37_209)
        exact = next(
            item
            for item in summary["usage_quality"]
            if item["usage_semantics"] == "delta"
        )
        assert exact["label"] == "exact_delta"
        assert exact["tokens"] == 145_155
        assert exact["call_share"] == 1.0
        assert exact["token_share"] == 1.0
        assert summary["today"]["timezone"] == USAGE_TIMEZONE
        assert summary["today"]["scope"] == "local_day"
        # Full-history total must never be mirrored as "today" semantics alone.
        assert summary["today"]["total_tokens"] <= summary["total_tokens"]


def test_summary_exposes_legacy_and_exact_shares_without_reclassifying() -> None:
    with TemporaryDirectory() as directory:
        app = _app(directory)
        ledger = app.state.model_calls
        exact_id = _settle(
            ledger,
            key="exact-share",
            input_tokens=100,
            cached_input_tokens=20,
            output_tokens=10,
            session_id="exact-session",
        )
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO model_calls(
                    call_id, idempotency_key, provider, model, call_kind, status,
                    input_tokens, cached_input_tokens, output_tokens,
                    raw_input_tokens, raw_cached_input_tokens, raw_output_tokens,
                    usage_semantics, normalized_usage_json, settled_at
                ) VALUES (?, ?, 'codex', 'codex', 'executor', 'completed',
                          ?, ?, ?, ?, ?, ?, 'legacy_inferred_delta', ?, CURRENT_TIMESTAMP)
                """,
                (
                    "legacy-call",
                    "legacy-share",
                    400,
                    0,
                    50,
                    400,
                    0,
                    50,
                    '{"source":"legacy_inferred_delta"}',
                ),
            )
        summary = ledger.summary()
        assert summary["usage_semantics"] == "mixed_exact_and_legacy_inferred_delta"
        by_sem = {
            item["usage_semantics"]: item for item in summary["usage_quality"]
        }
        assert by_sem["delta"]["label"] == "exact_delta"
        assert by_sem["legacy_inferred_delta"]["label"] == "legacy_inferred_delta"
        assert by_sem["delta"]["tokens"] == 110
        assert by_sem["legacy_inferred_delta"]["tokens"] == 450
        assert by_sem["legacy_inferred_delta"]["token_share"] == pytest.approx(450 / 560)
        assert by_sem["delta"]["call_share"] == pytest.approx(0.5)
        # Exact call remains exact; legacy remains legacy.
        settled = next(item for item in summary["calls"] if item["call_id"] == exact_id)
        legacy = next(item for item in summary["calls"] if item["call_id"] == "legacy-call")
        assert settled["usage_semantics"] == "delta"
        assert legacy["usage_semantics"] == "legacy_inferred_delta"