from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_name: str = "plow-whip-web.sqlite3"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
