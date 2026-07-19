from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from plow_whip_web.store.settings_repository import SettingsRepository


class SessionJournal:
    def __init__(self, data_dir: Path, settings: SettingsRepository) -> None:
        self.root = data_dir / "sessions"
        self.settings = settings

    def append(
        self, worker_id: str | None, event: dict[str, Any],
        *, maximum_bytes: int | None = None,
    ) -> dict[str, Any] | None:
        if not worker_id:
            return None
        directory = self.root / worker_id
        directory.mkdir(parents=True, exist_ok=True)
        current = directory / "events.current.jsonl"
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        rotated = None
        maximum = maximum_bytes or self.settings.get()["values"]["rotation_max_bytes"]
        if current.exists() and current.stat().st_size + len(line.encode("utf-8")) > maximum:
            rotated = self._rotate(directory, current)
        with current.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return rotated

    def current_bytes(self, worker_id: str | None) -> int:
        """Count only the hot/active journal generation, never archived events.*.jsonl."""
        if not worker_id:
            return 0
        current = self.root / worker_id / "events.current.jsonl"
        try:
            return current.stat().st_size if current.is_file() else 0
        except OSError:
            return 0

    def rotation_meta(self, worker_id: str | None) -> dict[str, Any] | None:
        if not worker_id:
            return None
        carry = self.root / worker_id / "carry-forward.json"
        if not carry.is_file():
            return None
        try:
            return json.loads(carry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def rotate_current(self, worker_id: str | None) -> dict[str, Any] | None:
        if not worker_id:
            return None
        directory = self.root / worker_id
        current = directory / "events.current.jsonl"
        if not current.is_file() or current.stat().st_size == 0:
            return None
        return self._rotate(directory, current)

    @staticmethod
    def _rotate(directory: Path, current: Path) -> dict[str, Any]:
        sequence = len(list(directory.glob("events.[0-9]*.jsonl"))) + 1
        archive = directory / f"events.{sequence:06d}.jsonl"
        os.replace(current, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        carry = {
            "archive": archive.name, "sha256": digest, "bytes": archive.stat().st_size,
            "reason": "size_limit",
        }
        temporary = directory / "carry-forward.tmp"
        temporary.write_text(json.dumps(carry, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, directory / "carry-forward.json")
        return carry
