from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from plow_whip_web.domain.model import DomainError
from plow_whip_web.roles import ROLE_PROMPTS
from plow_whip_web.store.convention_repository import ConventionRepository
from plow_whip_web.store.database import Database
from plow_whip_web.store.settings_repository import SettingsRepository
from plow_whip_web.store.task_repository import TaskRepository


class ContextCompiler:
    def __init__(
        self,
        data_dir: Path,
        database: Database,
        tasks: TaskRepository,
        conventions: ConventionRepository,
        settings: SettingsRepository,
    ) -> None:
        self.data_dir = data_dir
        self.database = database
        self.tasks = tasks
        self.conventions = conventions
        self.settings = settings

    def compile(self, task_id: str) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        role = self._role(task.role_id)
        sections: list[tuple[str, int, int]] = [
            ("## Objective\n" + task.objective, 3, 512),
            ("## Role\n" + ROLE_PROMPTS.get(role, ROLE_PROMPTS["fullstack"]), 2, 384),
        ]
        if task.last_error == "external_execution_interrupted":
            sections.append(
                (
                    "## Continuation\nThe previous host process was externally interrupted. "
                    "Inspect the existing workspace first, preserve completed work, and continue "
                    "the same objective from the retained CLI session without repeating finished steps.",
                    1,
                    0,
                )
            )
        for convention in self.conventions.resolve(project_id=task.project_id, task_id=task.id):
            if convention["content"]:
                priority, floor = {
                    "global": (0, 0),
                    "project": (4, 768),
                    "task": (5, 1024),
                }[convention["scope"]]
                sections.append((
                    f"## Convention: {convention['scope']}\n{convention['content']}",
                    priority,
                    floor,
                ))
        protected = [
            f"## Boundaries\nProject path: {task.project_path}\nTask id: {task.id}\nWorker id: {task.worker_id or 'pending'}",
            "## Completion rule\nOnly verification evidence can move this task to completed.",
        ]
        max_bytes = self.settings.get()["values"]["context_max_bytes"]
        content = _fit_sections(sections, protected, max_bytes)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        relative = Path("contexts") / task.id / f"{digest}.md"
        target = self.data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, target)
        with self.database.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id FROM context_packs WHERE task_id = ? AND content_hash = ?",
                (task.id, digest),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO context_packs(id, task_id, worker_id, content_hash, byte_size, relative_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), task.id, task.worker_id, digest, len(content.encode("utf-8")), str(relative)),
                )
        return {
            "task_id": task.id, "role": role, "content": content, "content_hash": digest,
            "byte_size": len(content.encode("utf-8")), "max_bytes": max_bytes,
            "relative_path": str(relative), "model_invoked": False,
        }

    def _role(self, role_id: str | None) -> str:
        if role_id is None:
            return "fullstack"
        connection = self.database.connect()
        try:
            row = connection.execute("SELECT kind FROM roles WHERE id = ?", (role_id,)).fetchone()
            return row["kind"] if row else "fullstack"
        finally:
            connection.close()


def _fit_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = "\n\n[context truncated deterministically]\n"
    room = max(0, limit - len(marker.encode("utf-8")))
    prefix = encoded[:room]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _fit_sections(
    sections: list[tuple[str, int, int]], protected: list[str], limit: int
) -> str:
    prefix = "# Execution Context"
    full = "\n\n".join([prefix, *(text for text, _, _ in sections), *protected]) + "\n"
    if len(full.encode("utf-8")) <= limit:
        return full

    truncation = "[context sections truncated deterministically by scope priority]"
    fixed = [prefix, truncation, *protected]
    if len(("\n\n".join(fixed) + "\n").encode("utf-8")) > limit:
        raise DomainError("context limit cannot preserve boundaries and completion rule")

    fitted = [text for text, _, _ in sections]

    def render() -> str:
        return "\n\n".join([
            prefix,
            *(text for text in fitted if text),
            truncation,
            *protected,
        ]) + "\n"

    for index in sorted(range(len(sections)), key=lambda item: sections[item][1]):
        over = len(render().encode("utf-8")) - limit
        if over <= 0:
            break
        current_size = len(fitted[index].encode("utf-8"))
        floor = min(current_size, sections[index][2])
        fitted[index] = _fit_utf8(
            fitted[index], max(floor, current_size - over)
        )
    content = render()
    if len(content.encode("utf-8")) > limit:
        raise DomainError(
            "context limit cannot preserve task/project rules, boundaries, and completion rule"
        )
    return content
