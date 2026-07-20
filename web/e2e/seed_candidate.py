from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plow_whip_web.api.app import create_app
from plow_whip_web.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    app = create_app(Settings(data_dir=args.data_dir))
    seeded: dict[str, dict[str, object]] = {}
    for index, (name, yesterday_tokens, today_tokens) in enumerate(
        (("E2E Alpha", (40, 4), (100, 11)), ("E2E Beta", (80, 8), (200, 22))),
        start=1,
    ):
        project_dir = args.data_dir / f"project-{index}"
        project_dir.mkdir(parents=True)
        project = app.state.project_repository.create(name=name, path=str(project_dir))
        role_id = app.state.project_repository.resolve_role(
            str(project["id"]), "frontend"
        )["role_id"]
        task = app.state.task_repository.create(
            title=f"{name} terminal task",
            objective=f"Prove terminal task detail for {name}",
            project_path=str(project["path"]),
            project_id=str(project["id"]),
            role_id=str(role_id),
            provider="codex",
            command={"argv": ["true"]},
            verification=[{"kind": "exit_code", "expected": 0}],
            max_attempts=1,
            idempotency_key=f"e2e-task-{index}",
            acceptance=[f"{name} terminal state is visible"],
        )
        task = app.state.task_repository.control(
            task.id,
            action="cancel",
            reason="e2e_terminal_fixture",
            expected_revision=task.revision,
            idempotency_key=f"e2e-cancel-{index}",
        )

        project_title = f"{name} project history"
        app.state.butler_repository.start_project_conversation(
            project_id=str(project["id"]),
            source_type="human",
            source_id="e2e-owner",
            instruction=project_title,
            draft={
                "title": project_title,
                "objective": f"Keep {project_title} scoped to {name}",
                "boundaries": [f"Only {name}"],
                "acceptance": [f"{project_title} remains visible"],
                "provider": "codex",
            },
            idempotency_key=f"e2e-project-butler-{index}",
        )
        app.state.butler_repository.start_global_conversation(
            source_type="human",
            source_id="e2e-owner",
            instruction=f"{name} global history",
            provider="codex",
            idempotency_key=f"e2e-global-butler-{index}",
        )

        call_ids: list[str] = []
        for period, (input_tokens, output_tokens) in (
            ("yesterday", yesterday_tokens),
            ("today", today_tokens),
        ):
            receipt = app.state.model_calls.prepare(
                idempotency_key=f"e2e-usage-{index}-{period}",
                call_kind="executor",
                provider="codex",
                project_id=str(project["id"]),
                session_id=f"e2e-session-{index}-{period}",
                session_generation=1,
            )
            with app.state.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE model_calls SET task_id = ? WHERE call_id = ?",
                    (task.id, receipt["call_id"]),
                )
            app.state.model_calls.settle(
                str(receipt["call_id"]),
                {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": input_tokens // 2,
                    "output_tokens": output_tokens,
                },
                session_id=f"e2e-session-{index}-{period}",
            )
            call_ids.append(str(receipt["call_id"]))
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(hour=4, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        with app.state.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE model_calls
                SET settled_at = ?, created_at = ?, updated_at = ?
                WHERE call_id = ?
                """,
                (yesterday, yesterday, yesterday, call_ids[0]),
            )

        seeded[name] = {
            "project_id": project["id"],
            "task_id": task.id,
            "task_title": task.title,
            "status": task.status.value,
            "history_total": sum(yesterday_tokens) + sum(today_tokens),
            "today_total": sum(today_tokens),
        }

    args.output.write_text(
        json.dumps({"projects": seeded}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
